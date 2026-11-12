"""Event-driven execution of a graph.

Every edge (and every graph input) is a *feed* with its own mailbox. An input port is ready when all its
forward feeds hold an envelope, or when one of its back feeds does. A node keeps a count of its required ports
that are not ready; an arriving envelope changes that count for one node and nothing else, so scheduling costs
the same per message whatever the size of the graph.

A node is activated when

* it fires on ``ALL``: every required port is ready (a node that requires nothing starts once, by itself, and
  again whenever an optional port becomes ready);
* it fires on ``ANY``: any envelope arrives; each activation takes exactly one.

An activation takes one envelope from each feed of each ready port (a ``many`` port yields a list, in the
order of its edges), runs the node, and delivers what it emits. A node never runs twice at the same time. A
back edge stops carrying envelopes once it has carried ``max_rounds`` of them. The run ends when nothing is
running and nothing is ready.

``workers=0`` runs everything in the calling thread, in a fixed order. With more workers, nodes run in a pool
of threads and only the bookkeeping is serialised.

``stopping`` is a function the engine asks before each activation; once it returns true the run stops as if
``stop`` had been called. A graph that runs inside a node of another graph is stopped this way.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..base import Context, Delivery, Envelope, Firing, Graph, Node, NodeError, Protocols, digest

_INPUT = "input:"   # prefix of the pseudo edge id of a graph input


class RunError(RuntimeError):
    """A node failed, or the run was given something the graph does not take. ``node`` names the node."""

    def __init__(self, message: str, node: str | None = None):
        super().__init__(message)
        self.node = node


@dataclass
class Result:
    outputs: dict[str, list] = field(default_factory=dict)   # everything that left each graph output, in order
    activations: int = 0
    envelopes: int = 0
    rounds: dict[str, int] = field(default_factory=dict)     # how many times each node ran
    limited: tuple[str, ...] = ()                            # back edges that reached max_rounds and dropped an envelope

    def last(self, name: str, default: Any = None) -> Any:
        values = self.outputs.get(name)
        return values[-1] if values else default


class _Feed:
    __slots__ = ("id", "port", "back", "latest", "protocol", "box", "sent", "limit")

    def __init__(self, id, port, back, protocol, limit):
        self.id, self.port, self.back, self.protocol, self.limit = id, port, back, protocol, limit
        self.latest = protocol is not None and protocol.delivery is Delivery.LATEST
        self.box: deque = deque()
        self.sent = 0


class _Port:
    __slots__ = ("port", "state", "forward", "back", "forward_full", "back_full", "ready")

    def __init__(self, port, state):
        self.port, self.state = port, state
        self.forward: list[_Feed] = []
        self.back: list[_Feed] = []
        self.forward_full = 0     # forward feeds holding an envelope
        self.back_full = 0
        self.ready = False


class _State:
    __slots__ = ("node", "ports", "any", "missing", "optional_ready", "arrivals", "running", "queued", "started",
                 "opened", "round", "outs")

    def __init__(self, node: Node):
        self.node = node
        self.ports: dict[str, _Port] = {}
        self.any = node.firing is Firing.ANY
        self.missing = 0            # required ports not ready
        self.optional_ready = 0
        self.arrivals: deque = deque()    # ANY only: feeds in the order their envelopes arrived
        self.running = self.queued = self.started = self.opened = False
        self.round = 0
        self.outs: dict[str, tuple] = {}  # output port -> (feeds, graph output names)


class _Context(Context):
    __slots__ = ("node", "round", "_engine", "_state", "_cause")

    def __init__(self, engine, state, cause):
        self.node, self.round = state.node.id, state.round
        self._engine, self._state, self._cause = engine, state, cause

    def emit(self, port: str, payload: Any) -> None:
        self._engine._emit(self._state, port, payload, self.round, self._cause)

    def stopping(self) -> bool:
        engine = self._engine
        return engine._stop or (engine._outer is not None and bool(engine._outer()))


class Engine:
    """One run of one graph. Build a new Engine to run again."""

    def __init__(self, graph: Graph, protocols: Protocols, *, workers: int = 0,
                 stopping: Callable[[], bool] | None = None):
        if workers < 0:
            raise ValueError("workers cannot be negative")
        self.graph, self.workers = graph, workers
        self._outer = stopping
        self._lock = threading.Condition()
        self._ready: deque[_State] = deque()
        self._active = 0
        self._stop = False
        self._error: RunError | None = None
        self._result = Result(outputs={name: [] for name in graph.outputs})
        self._limited: dict[str, None] = {}
        self._used = False
        self._states = {node.id: _State(node) for node in graph.nodes}
        self._inputs: dict[str, _Feed] = {}
        for state in self._states.values():
            state.ports = {name: _Port(port, state) for name, port in state.node.inputs.items()}
        feeds: dict[str, _Feed] = {}
        for edge in graph.edges:
            protocol = protocols.get(edge.protocol)
            if protocol is None:
                raise RunError(f"edge {edge.id}: unknown protocol {edge.protocol!r}")
            if protocol.delivery is Delivery.STREAM or not protocol.triggers:
                raise RunError(f"edge {edge.id}: protocol {edge.protocol!r} needs a delivery this engine does not have yet")
            port = self._states[edge.target.node].ports[edge.target.port]
            feed = feeds[edge.id] = _Feed(edge.id, port, edge.back, protocol, edge.max_rounds)
            (port.back if edge.back else port.forward).append(feed)
        for name, end in graph.inputs.items():
            port = self._states[end.node].ports[end.port]
            feed = self._inputs[name] = _Feed(_INPUT + name, port, False, None, None)
            port.forward.append(feed)
        exposed: dict[tuple, list[str]] = {}
        for name, end in graph.outputs.items():
            exposed.setdefault(tuple(end), []).append(name)
        for state in self._states.values():
            node = state.node
            state.missing = sum(1 for p in state.ports.values() if p.port.required)
            state.outs = {name: (tuple((feeds[e.id], node.outputs[name]) for e in graph.outgoing(node.id, name)),
                                 tuple(exposed.get((node.id, name), ()))) for name in node.outputs}

    # ---- running ---------------------------------------------------------------------------------------

    def run(self, inputs: Mapping[str, Any] | None = None) -> Result:
        """Feed the graph's inputs, run until nothing is left to do, and return what reached its outputs."""
        if self._used:
            raise RunError("this engine has already run; build a new one")
        self._used = True
        inputs = dict(inputs or {})
        unknown = inputs.keys() - self._inputs.keys()
        if unknown:
            raise RunError(f"the graph has no input {sorted(unknown)}")
        with self._lock:
            for name, payload in inputs.items():
                feed = self._inputs[name]
                try:
                    feed.port.port.schema.check(payload)
                except ValueError as exc:
                    raise RunError(f"graph input {name}: {exc}") from None
                self._deliver(feed, payload, 0, ())
            for state in self._states.values():
                self._consider(state)
        try:
            if self.workers:
                threads = [threading.Thread(target=self._work, name=f"engine-{i}", daemon=True) for i in range(self.workers)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            else:
                self._work()
        finally:
            self._close()
        if self._error:
            raise self._error
        result = self._result
        result.rounds = {id: state.round for id, state in self._states.items() if state.round}
        result.limited = tuple(self._limited)
        return result

    def stop(self) -> None:
        """Ask the run to end: nothing new starts; running nodes see ``ctx.stopping()``."""
        with self._lock:
            self._stop = True
            self._lock.notify_all()

    def _work(self) -> None:
        lock = self._lock
        while True:
            with lock:
                if self._outer is not None and not self._stop and self._outer():
                    self._stop = True
                while not self._ready and self._active and not self._stop:
                    lock.wait()
                if self._stop or not self._ready:
                    lock.notify_all()
                    return
                state = self._ready.popleft()
                state.queued, state.running, state.started = False, True, True
                state.round += 1
                self._active += 1
                try:
                    taken, cause = self._take(state)
                except Exception as exc:   # noqa: BLE001 - a protocol's decode failed
                    self._fail(state, exc)
                    continue
                self._result.activations += 1
            node, ctx = state.node, _Context(self, state, cause)
            try:
                if not state.opened:
                    state.opened = True
                    node.open(ctx)
                returned = node.run(ctx, taken)
                if returned is not None:
                    if not isinstance(returned, Mapping):
                        raise NodeError(f"run must return a mapping of output port to payload, or None; got {type(returned).__name__}")
                    for port, payload in returned.items():
                        self._emit(state, port, payload, state.round, cause)
            except Exception as exc:   # noqa: BLE001 - whatever a node raises ends the run
                with lock:
                    self._fail(state, exc)
                continue
            with lock:
                state.running = False
                self._active -= 1
                self._consider(state)
                if not self._ready and not self._active:
                    lock.notify_all()
                elif self._ready:
                    lock.notify()

    def _fail(self, state: _State, exc: Exception) -> None:
        state.running = False
        self._active -= 1
        if self._error is None:
            self._error = RunError(f"node {state.node.id} failed in round {state.round}: {exc}", state.node.id)
            self._error.__cause__ = exc
        self._stop = True
        self._lock.notify_all()

    def _close(self) -> None:
        for state in self._states.values():
            if state.opened:
                try:
                    state.node.close(_Context(self, state, ()))
                except Exception as exc:   # noqa: BLE001
                    if self._error is None:
                        self._error = RunError(f"node {state.node.id} failed to close: {exc}", state.node.id)

    # ---- bookkeeping (always under the lock) ------------------------------------------------------------

    def _consider(self, state: _State) -> None:
        """Queue the node if it can be activated now."""
        if state.running or state.queued or self._stop:
            return
        if state.any:
            arrivals = state.arrivals
            while arrivals and not arrivals[0].box:   # a latest feed that was overwritten and already read
                arrivals.popleft()
            can = bool(arrivals)
        elif state.missing:
            can = False
        else:
            can = bool(state.optional_ready) or not state.started or any(p.port.required for p in state.ports.values())
        if can:
            state.queued = True
            self._ready.append(state)

    def _refresh(self, port: _Port) -> None:
        ready = (bool(port.forward) and port.forward_full == len(port.forward)) or port.back_full > 0
        if ready != port.ready:
            port.ready = ready
            state = port.state
            if port.port.required:
                state.missing += -1 if ready else 1
            else:
                state.optional_ready += 1 if ready else -1

    def _deliver(self, feed: _Feed, body: Any, round: int, cause: tuple) -> None:
        feed.sent += 1
        try:
            body_digest = digest(body)
        except (TypeError, ValueError):
            body_digest = ""    # not plain data: it can travel inside the process, but it has no content hash
        envelope = Envelope(f"{feed.id}#{feed.sent}", feed.id, feed.sent, round, body, body_digest, cause)
        box, port = feed.box, feed.port
        if box and feed.latest:
            box[0] = envelope
        else:
            box.append(envelope)
            if len(box) == 1:
                if feed.back:
                    port.back_full += 1
                else:
                    port.forward_full += 1
                self._refresh(port)
        self._result.envelopes += 1
        state = port.state
        if state.any:
            state.arrivals.append(feed)
        self._consider(state)

    def _pop(self, feed: _Feed) -> Envelope:
        envelope = feed.box.popleft()
        if not feed.box:
            port = feed.port
            if feed.back:
                port.back_full -= 1
            else:
                port.forward_full -= 1
            self._refresh(port)
        return envelope

    def _take(self, state: _State):
        """The inputs of one activation, and the ids of the envelopes they came in."""
        inputs: dict[str, Any] = {}
        cause: list[str] = []

        def read(feed):
            envelope = self._pop(feed)
            cause.append(envelope.id)
            return envelope.body if feed.protocol is None else feed.protocol.decode(envelope.body, feed.port.port)

        if state.any:
            while state.arrivals:
                feed = state.arrivals.popleft()
                if feed.box:    # a latest feed may have been overwritten and already read
                    value = read(feed)
                    inputs[feed.port.port.name] = [value] if feed.port.port.many else value
                    break
            return inputs, tuple(cause)
        for name, port in state.ports.items():
            if not port.ready:
                continue
            if port.forward and port.forward_full == len(port.forward):
                values = [read(feed) for feed in port.forward]
            else:
                values = [read(next(feed for feed in port.back if feed.box))]
            inputs[name] = values if port.port.many else values[0]
        return inputs, tuple(cause)

    def _emit(self, state: _State, port: str, payload: Any, round: int, cause: tuple) -> None:
        try:
            feeds, names = state.outs[port]
        except KeyError:
            raise NodeError(f"{state.node.id} has no output port {port!r}") from None
        bodies = [feed.protocol.encode(payload, source) for feed, source in feeds]   # validate before anything is sent
        if not feeds:
            state.node.outputs[port].schema.check(payload)
        with self._lock:
            for name in names:
                self._result.outputs[name].append(payload)
            for (feed, _), body in zip(feeds, bodies):
                if feed.limit is not None and feed.sent >= feed.limit:
                    self._limited[feed.id] = None
                    continue
                self._deliver(feed, body, round, cause)
            if self._ready:
                self._lock.notify()
