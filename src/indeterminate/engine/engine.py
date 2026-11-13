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

Three kinds of edge are handled apart from that:

* a protocol that does not trigger (``triggers = False``) keeps only its newest envelope and never makes a
  node ready; whenever the node runs for another reason it finds that value on the port, and it stays there;
* a ``STREAM`` protocol delivers one envelope per sending activation, holding a ``Stream``: everything that
  activation emits on the port is a chunk of it, and it closes when the activation ends. With two or more
  workers the receiver reads while the sender is still writing; with fewer it reads once the sender is done;
* a ``CALL`` protocol is not scheduled at all. The sending node calls ``ctx.call(port, payload)``; the
  receiving node runs at once, in the caller's thread, and what it returns is the answer. A node that is
  called takes nothing else: it only ever runs when called, one call at a time.

``workers=0`` runs everything in the calling thread, in a fixed order. With more workers, nodes run in a pool
of threads and only the bookkeeping is serialised.

``stopping`` is a function the engine asks before each activation; once it returns true the run stops as if
``stop`` had been called. A graph that runs inside a node of another graph is stopped this way.

``journal`` makes the run durable: every finished activation is written to it in one piece, and ``resume``
on a new engine with the same journal takes the run up where it stopped. What a finished activation did is
never done again; an activation that was cut off runs again from its inputs, so a node may see the same
input twice, and what it had already sent on is not delivered a second time to a node that has dealt with
it. Everything that travels must then be plain data, a ``Ref`` or a stream of those. ``resume(change=True)``
continues under a different graph: nodes and edges that are still there keep their state and their waiting
envelopes, the rest is dropped.

``time_limit`` is the longest one activation may take, in seconds. A node's own ``time_limit`` attribute wins over
it (``math.inf`` for a node that may take as long as it likes, a person for instance). A thread cannot be
killed, so when an activation overruns, the run fails with that node named, running nodes see
``ctx.stopping()``, and the engine returns without waiting for the one that is stuck; whatever it does
afterwards is thrown away. Nothing it did was journaled, so a resumed run starts that activation again.

``observe`` is a function the engine tells what happens, as it happens: one mapping per event, with
``event`` one of ``started``, ``emitted``, ``finished``, ``failed`` (each with ``node`` and ``round``) and,
last, ``ended``. It is called from the threads that run the nodes and must not raise; it is how a screen
or a log follows a run without reading the journal.

What a node sends is checked as it is sent: by the port's schema, by the protocol of each edge it leaves
on, and by the port's own checks. When a check finds a problem the port decides (see ``Port``):

* while the port has repairs left in this activation, the node is told: ``emit`` raises ``Invalid`` with
  ``again`` true. A node that can put things right (an agent) does, and sends again. A node that lets the
  exception go is run again from the same inputs, with ``ctx.problems`` saying what was wrong, provided it
  had not yet delivered anything;
* then, a port that warns sends the payload on as it is and the problem is recorded;
* otherwise the node fails and the run stops, to be resumed once the cause is dealt with.

Every problem, whatever became of it, is in ``Result.problems``, in the journal, and told to ``observe``
(``rejected`` when it was given back to the node, ``warned`` when it was let through). The same applies on
arrival, when a protocol finds that what arrived is not what was sent: the receiving port fails or warns.

A node may run a whole graph inside one activation: ``ctx.engine(graph, protocols)`` gives it an engine
that belongs to the outer run. It stops with it, works under the same time limit, and tells the outer
observer its events with ``within`` naming the node (or nodes) it runs inside. When the outer run has a
journal, the inner one keeps its own next to it (``<journal>.d/<node>-<round>.jsonl``), so that after a
restart the outer activation runs again and the inner run goes on from where it was instead of starting
over. Problems found inside are recorded outside as well, under ``outer/inner``.

``transport`` stands between sending and receiving: every envelope body passes through its ``carry``. Without
one, bodies are handed over as they are. Streams and calls do not pass through it yet.
"""
from __future__ import annotations

import os
import secrets
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..base import Context, Delivery, Envelope, Firing, Graph, Invalid, Node, NodeError, Protocols, Ref, digest
from .journal import Journal, pack, pack_body, unpack_body
from .stream import Stream
from .transport import Transport

_INPUT = "input:"   # prefix of the pseudo edge id of a graph input


class OverBudget(NodeError):
    """Raised in a node when what it reports using takes the run over its budget. The run has failed by
    then; the node should return."""


def _discard(journal) -> None:
    """Remove the journal of a run that happened inside an activation, and those of the runs inside that."""
    shutil.rmtree(journal.with_name(journal.name + ".d"), ignore_errors=True)
    try:
        journal.unlink()
    except OSError:
        pass


def _amounts(given: Mapping[str, float], what: str) -> dict[str, float]:
    for name, amount in given.items():
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not amount >= 0 or amount == float("inf"):
            raise ValueError(f"{what}: {name} must be a number that is not negative, not {amount!r}")
    return dict(given)


class RunError(RuntimeError):
    """A node failed, or the run was given something the graph does not take. ``node`` names the node."""

    def __init__(self, message: str, node: str | None = None):
        super().__init__(message)
        self.node = node
        self.problems: list[dict] = []     # what had failed a check before the run failed
        self.used: dict[str, float] = {}   # what the run had used up when it failed


@dataclass
class Result:
    outputs: dict[str, list] = field(default_factory=dict)   # everything that left each graph output, in order
    activations: int = 0
    envelopes: int = 0
    rounds: dict[str, int] = field(default_factory=dict)     # how many times each node ran
    limited: tuple[str, ...] = ()                            # back edges that reached max_rounds and dropped an envelope
    complete: bool = True                                    # False when the run was stopped with work left
    problems: list[dict] = field(default_factory=list)       # what failed a check and was repaired or let through
    used: dict[str, float] = field(default_factory=dict)     # what the nodes reported using, added up

    def last(self, name: str, default: Any = None) -> Any:
        values = self.outputs.get(name)
        return values[-1] if values else default


class _Feed:
    __slots__ = ("id", "port", "back", "latest", "stream", "passive", "reply", "protocol", "box", "sent", "limit")

    def __init__(self, id, port, back, protocol, limit):
        self.id, self.port, self.back, self.protocol, self.limit = id, port, back, protocol, limit
        delivery = protocol.delivery if protocol is not None else Delivery.QUEUE
        self.latest = delivery is Delivery.LATEST
        self.stream = delivery is Delivery.STREAM
        self.passive = protocol is not None and not protocol.triggers
        self.reply: str | None = None     # CALL only: the output port of the called node that holds the answer
        self.box: deque = deque()
        self.sent = 0


class _Port:
    __slots__ = ("port", "state", "forward", "back", "passive", "forward_full", "back_full", "ready")

    def __init__(self, port, state):
        self.port, self.state = port, state
        self.forward: list[_Feed] = []
        self.back: list[_Feed] = []
        self.passive: list[_Feed] = []    # feeds that do not trigger: read, never taken
        self.forward_full = 0     # forward feeds holding an envelope
        self.back_full = 0
        self.ready = False


class _State:
    __slots__ = ("node", "ports", "any", "missing", "optional_ready", "arrivals", "running", "queued", "started",
                 "opened", "round", "outs", "calls", "passive", "streams", "called", "since", "time_limit", "rejected", "delivered",
                 "inner")

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
        self.calls: dict[str, _Feed] = {}         # output port -> its call edge
        self.passive: list[_Port] = []            # input ports fed by edges that do not trigger
        self.streams: dict[str, Any] = {}         # feed id -> the stream this activation is writing
        self.called: threading.Lock | None = None  # set when the node is the target of a call: one call at a time
        self.since = 0.0                          # when the running activation began
        self.time_limit: float | None = None      # how long an activation may take, in seconds
        self.rejected: dict[str, int] = {}        # this activation: output port -> times a payload was given back
        self.delivered = False                    # this activation has sent something on
        self.inner: list = []                     # this activation: the journals of the runs it made inside itself


class _Act:
    """What one activation did, for the journal."""

    __slots__ = ("node", "round", "took", "sent", "out", "limited", "calls", "snaps", "problems")

    def __init__(self, node, round, took):
        self.node, self.round, self.took = node, round, took
        self.sent: list[Envelope] = []
        self.out: list[tuple] = []
        self.limited: list[str] = []
        self.calls: list[str] = []
        self.snaps: dict[str, Any] = {}
        self.problems: list[dict] = []


class _Context(Context):
    __slots__ = ("node", "round", "problems", "_engine", "_state", "_cause", "_act", "_cut")

    def __init__(self, engine, state, cause, act=None):
        self.node, self.round, self.problems = state.node.id, state.round, []
        self._cut = False       # the node has been told the run is stopping: what it did may not be all of it
        self._engine, self._state, self._cause, self._act = engine, state, cause, act

    def emit(self, port: str, payload: Any) -> None:
        self._engine._emit(self._state, port, payload, self.round, self._cause, self._act)

    def call(self, port: str, payload: Any) -> Any:
        return self._engine._call(self._state, port, payload, self._act)

    def stopping(self) -> bool:
        engine = self._engine
        if engine._stop or (engine._outer is not None and bool(engine._outer())):
            self._cut = True
            return True
        return False

    def engine(self, graph: Graph, protocols: Protocols, *, workers: int = 0) -> "Engine":
        return self._engine._inner(self, graph, protocols, workers)

    def use(self, **amounts: float) -> None:
        self._engine._use(self._state, self.round, amounts)

    @property
    def key(self) -> str:
        return f"{self._engine._id}/{self.node}#{self.round}"

    @property
    def within(self) -> tuple:
        return self._engine._within


class Engine:
    """One run of one graph. Build a new Engine to run again."""

    def __init__(self, graph: Graph, protocols: Protocols, *, workers: int = 0,
                 stopping: Callable[[], bool] | None = None, transport: Transport | None = None,
                 journal: Journal | None = None, time_limit: float | None = None,
                 observe: Callable[[Mapping], None] | None = None, budget: Mapping[str, float] | None = None):
        if workers < 0:
            raise ValueError("workers cannot be negative")
        if time_limit is not None and not time_limit > 0:
            raise ValueError("time_limit must be a positive number of seconds")
        self.graph, self.workers = graph, workers
        self._observe = observe
        self._budget = _amounts(budget or {}, "budget")
        self._meter: dict[str, float] = {}  # what the nodes have reported using
        self._id: str | None = None         # names this run: the first part of every activation's key
        self._limit = time_limit
        self._parent: tuple | None = None   # (outer engine, the outer node's state, its activation's record)
        self._within: tuple = ()            # (node id, round) of each node this run is inside, outermost first
        self._pending: deque = deque()    # problems recorded under the lock, waiting to be told to the observer
        self._over = False                # set once the engine has returned: a node still running is on its own
        self._alive = 0                   # worker threads that have not returned
        self._outer = stopping
        self._carry = transport.carry if transport is not None else None
        self._journal = journal
        self._orphans: set[str] = set()   # after a resume: envelopes already dealt with whose sender must run again
        self._lock = threading.Condition()
        self._ready: deque[_State] = deque()
        self._active = 0
        self._stop = False
        self._error: RunError | None = None
        self._result = Result(outputs={name: [] for name in graph.outputs})
        self._limited: dict[str, None] = {}
        self._used = False
        self._states = {node.id: _State(node) for node in graph.nodes}
        for state in self._states.values():
            own = getattr(state.node, "time_limit", None)
            state.time_limit = own if own is not None else time_limit
            if state.time_limit is not None and not state.time_limit > 0:
                raise RunError(f"{state.node.id}: time_limit must be a positive number of seconds", state.node.id)
        self._timed = any(state.time_limit is not None for state in self._states.values())
        self._inputs: dict[str, _Feed] = {}
        for state in self._states.values():
            state.ports = {name: _Port(port, state) for name, port in state.node.inputs.items()}
        feeds: dict[str, _Feed] = {}
        for edge in graph.edges:
            protocol = protocols.get(edge.protocol)
            if protocol is None:
                raise RunError(f"edge {edge.id}: unknown protocol {edge.protocol!r}")
            target = self._states[edge.target.node]
            port = target.ports[edge.target.port]
            feed = feeds[edge.id] = _Feed(edge.id, port, edge.back, protocol, edge.max_rounds)
            if protocol.delivery is Delivery.CALL:
                if edge.back:
                    raise RunError(f"edge {edge.id}: a call cannot be a back edge")
                outputs = [name for name, out in target.node.outputs.items() if not out.call]
                feed.reply = outputs[0] if len(outputs) == 1 else "reply" if "reply" in outputs else None
                target.called = target.called or threading.Lock()
            elif feed.passive:
                if not feed.latest or edge.back:
                    raise RunError(f"edge {edge.id}: a protocol that does not trigger must deliver the latest value, forward")
                port.passive.append(feed)
            else:
                (port.back if edge.back else port.forward).append(feed)
        for name, end in graph.inputs.items():
            port = self._states[end.node].ports[end.port]
            feed = self._inputs[name] = _Feed(_INPUT + name, port, False, None, None)
            port.forward.append(feed)
        self._feeds = {**feeds, **{feed.id: feed for feed in self._inputs.values()}}
        exposed: dict[tuple, list[str]] = {}
        for name, end in graph.outputs.items():
            exposed.setdefault(tuple(end), []).append(name)
        for state in self._states.values():
            node = state.node
            state.missing = sum(1 for p in state.ports.values() if p.port.required)
            for name, port in state.ports.items():
                if port.passive:
                    if port.forward or port.back or port.port.required:
                        raise RunError(f"{node.id}.{name} is fed by an edge that does not trigger: it must be optional "
                                       f"and take nothing else")
                    state.passive.append(port)
                if state.called and (port.forward or port.back or port.passive):
                    raise RunError(f"{node.id} is called, so nothing else may feed it ({name} is fed)")
            for name in node.outputs:
                out = tuple((feeds[e.id], node.outputs[name]) for e in graph.outgoing(node.id, name))
                calls = [feed for feed, _ in out if feed.protocol.delivery is Delivery.CALL]
                if calls:
                    if len(out) != 1:
                        raise RunError(f"{node.id}.{name} makes a call, so exactly one edge may leave it")
                    state.calls[name] = calls[0]
                    out = ()
                state.outs[name] = (out, tuple(exposed.get((node.id, name), ())))

    # ---- running ---------------------------------------------------------------------------------------

    def run(self, inputs: Mapping[str, Any] | None = None) -> Result:
        """Feed the graph's inputs, run until nothing is left to do, and return what reached its outputs."""
        self._begin()
        journal = self._journal
        if journal is not None and journal.read():
            raise RunError("the journal already holds a run: resume it, or use a new journal")
        inputs = dict(inputs or {})
        unknown = inputs.keys() - self._inputs.keys()
        if unknown:
            raise RunError(f"the graph has no input {sorted(unknown)}")
        with self._lock:
            sent = []
            for name, payload in inputs.items():
                feed = self._inputs[name]
                try:
                    feed.port.port.schema.check(payload)
                except ValueError as exc:
                    raise RunError(f"graph input {name}: {exc}") from None
                sent.append(self._deliver(feed, payload, 0, ()))
            self._id = self._id or secrets.token_hex(8)
            if journal is not None:
                try:
                    journal.write({"kind": "run", "id": self._id, "graph": self.graph.digest, "sent": [pack(e) for e in sent]})
                except (TypeError, ValueError) as exc:
                    raise RunError(f"the graph's inputs cannot be journaled: {exc}") from None
            for state in self._states.values():
                self._consider(state)
        return self._finish()

    def go(self, inputs: Mapping[str, Any] | None = None, *, fresh: bool = False) -> Result:
        """Run, or, when the journal already holds a run of this graph, take that run up. ``fresh`` throws
        an earlier run away."""
        journal = self._journal
        if journal is not None:
            records = journal.read()
            if records:
                under = next((r["graph"] for r in reversed(records) if r["kind"] in ("run", "change")), None)
                if not fresh and under == self.graph.digest:
                    return self.resume()
                journal.close()
                journal.path.unlink()
        return self.run(inputs)

    def _inner(self, ctx: "_Context", graph: Graph, protocols: Protocols, workers: int) -> "Engine":
        """An engine for a graph that runs inside one activation of one of this engine's nodes."""
        state = ctx._state
        journal = None
        if self._journal is not None:
            outer = self._journal.path
            journal = Journal(outer.with_name(outer.name + ".d") / f"{state.node.id}-{ctx.round}.jsonl", sync=self._journal.sync)
            with self._lock:
                if journal.path not in state.inner:
                    state.inner.append(journal.path)
        told, id = self._observe, state.node.id
        observe = None if told is None else lambda event: told({**event, "within": [id, *event.get("within", ())]})
        inner = Engine(graph, protocols, workers=workers, stopping=ctx.stopping, journal=journal,
                       time_limit=self._limit, observe=observe)
        inner._parent = (self, state, ctx._act)
        inner._within = (*self._within, (state.node.id, ctx.round))
        inner._id = ctx.key
        return inner

    def _use(self, state: _State, round: int, amounts: Mapping[str, float]) -> None:
        """Add what a node reports using to the run's count; over the budget, the run fails."""
        amounts = _amounts(amounts, "use")
        if not amounts:
            return
        over = None
        with self._lock:
            if self._over:
                return
            meter = self._meter
            for name, amount in amounts.items():
                meter[name] = meter.get(name, 0) + amount
                limit = self._budget.get(name)
                if over is None and limit is not None and meter[name] > limit:
                    over = f"the run has used {meter[name]:g} {name}, and its budget is {limit:g}"
            if self._journal is not None:
                self._journal.write({"kind": "use", "node": state.node.id, "round": round, "amounts": amounts})
            if over and self._error is None:        # the run ends even if the node goes on regardless
                self._error = RunError(over, state.node.id)
                self._stop = True
                self._lock.notify_all()
            total = dict(meter)
        if self._observe is not None:
            self._tell("used", node=state.node.id, round=round, amounts=amounts, total=total)
        if self._parent is not None:
            outer, outer_state, _ = self._parent
            outer._use(outer_state, self._within[-1][1], amounts)
        if over:
            raise OverBudget(over)

    def _adopt(self, state: _State, act: _Act | None, problems: list[dict]) -> None:
        """Take on the problems of a run that happened inside one of this engine's nodes."""
        with self._lock:
            for problem in problems:
                record = {**problem, "node": f"{state.node.id}/{problem['node']}"}
                self._result.problems.append(record)
                if act is not None:
                    act.problems.append(record)

    def resume(self, *, change: bool = False) -> Result:
        """Take up the run in the journal where it stopped. ``change=True`` allows this engine's graph to
        differ from the one the journal was written under."""
        self._begin()
        journal = self._journal
        if journal is None:
            raise RunError("resume needs a journal")
        records = journal.read()
        if not records:
            raise RunError("the journal is empty: there is nothing to resume")
        under = next(r["graph"] for r in reversed(records) if r["kind"] in ("run", "change"))
        changed = under != self.graph.digest
        if changed and not change:
            raise RunError(f"the journal was written under another graph ({under[:12]}); "
                           f"pass change=True to continue under this one ({self.graph.digest[:12]})")
        ended = records[-1]["kind"] == "end" and not changed
        with self._lock:
            dropped = self._restore(records)
            if not ended:
                for name, limit in self._budget.items():
                    if self._meter.get(name, 0) > limit:
                        raise RunError(f"the run has used {self._meter[name]:g} {name}, and its budget is {limit:g}: "
                                       f"it goes on only under a larger budget")
            if changed:
                journal.write({"kind": "change", "from": under, "graph": self.graph.digest, "dropped": dropped})
            if not ended:
                for state in self._states.values():
                    self._consider(state)
        return self._finish(ended)

    def _begin(self) -> None:
        if self._used:
            raise RunError("this engine has already run; build a new one")
        self._used = True

    def _restore(self, records: list[dict]) -> int:
        """Rebuild counters, node state and waiting envelopes from the journal. Returns how many waiting
        envelopes were dropped because their edge is not in this graph."""
        result, states = self._result, self._states
        sent: dict[str, dict[int, list]] = {}
        took: set[str] = set()
        snaps: dict[str, Any] = {}
        for record in records:
            kind = record["kind"]
            if kind == "run":
                self._id = self._id or record.get("id") or record["graph"][:16]
            elif kind == "use":
                for name, amount in record["amounts"].items():
                    self._meter[name] = self._meter.get(name, 0) + amount
            if kind == "act":
                took.update(record["took"])
                for name, body in record["out"]:
                    if name in result.outputs:
                        result.outputs[name].append(unpack_body(body))
                for id in record["limited"]:
                    if id in self._feeds:
                        self._limited[id] = None
                result.problems.extend(record.get("problems", ()))
                for id in (record["node"], *record["calls"]):
                    state = states.get(id)
                    if state is not None:
                        state.round += 1
                        state.started = True
                        result.activations += 1
                result.envelopes += len(record["calls"])
                snaps.update(record["snaps"])
            if kind in ("run", "act"):
                for packed in record["sent"]:
                    sent.setdefault(packed[0], {})[packed[1]] = packed
        dropped = 0
        self._orphans = took - {f"{feed}#{seq}" for feed, by_seq in sent.items() for seq in by_seq}
        for id, by_seq in sent.items():
            seqs = sorted(by_seq)
            waiting = [seq for seq in seqs if f"{id}#{seq}" not in took]
            feed = self._feeds.get(id)
            if feed is None:
                dropped += len(waiting)
                continue
            feed.sent = seqs[-1]
            result.envelopes += len(seqs)
            if feed.passive:
                waiting = seqs[-1:]
            elif feed.latest:
                waiting = waiting[-1:] if waiting and waiting[-1] == seqs[-1] else []
            for seq in waiting:
                _, _, round, body, cause = by_seq[seq]
                body = unpack_body(body, feed.protocol, feed.port.port)
                self._place(feed, Envelope(f"{id}#{seq}", id, seq, round, body, "", tuple(cause)))
        for id, snapshot in snaps.items():
            if id in states:
                states[id].node.restore(snapshot)
        return dropped

    def _finish(self, ended: bool = False) -> Result:
        try:
            if ended:
                pass
            elif self._timed:
                self._watch()
            elif self.workers:
                threads = [threading.Thread(target=self._work, name=f"engine-{i}", daemon=True) for i in range(self.workers)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            else:
                self._work()
        finally:
            with self._lock:
                self._over = True
            self._close()
            if self._journal is not None:
                if not ended and not self._stop and self._error is None:
                    self._journal.write({"kind": "end"})
                    try:
                        os.rmdir(self._journal.path.with_name(self._journal.path.name + ".d"))
                    except OSError:
                        pass                # never made, or something is still in it
                self._journal.close()
        if self._parent is not None and self._result.problems:
            outer, state, act = self._parent
            outer._adopt(state, act, self._result.problems)
        if self._error:
            self._flush()
            self._error.problems = self._result.problems
            self._error.used = dict(self._meter)
            self._tell("ended", complete=False, error=str(self._error))
            cause = self._error.__cause__
            if cause is not None and not isinstance(cause, Exception):
                raise cause                 # an interrupt or an exit request passes through as itself
            raise self._error
        result = self._result
        result.rounds = {id: state.round for id, state in self._states.items() if state.round}
        result.limited = tuple(self._limited)
        result.complete = not self._stop
        result.used = dict(self._meter)
        self._flush()
        self._tell("ended", complete=result.complete)
        if self._error:                     # the observer failed on the last thing it was told
            raise self._error
        return result

    def _watch(self) -> None:
        """Run the workers in threads of their own and watch the clock for them. The order of activations is
        the same as without a limit: with no workers asked for, one thread does everything."""
        lock = self._lock

        def work():
            try:
                self._work()
            finally:
                with lock:
                    self._alive -= 1
                    lock.notify_all()

        threads = [threading.Thread(target=work, name=f"engine-{i}", daemon=True) for i in range(self.workers or 1)]
        self._alive = len(threads)
        for thread in threads:
            thread.start()
        with lock:
            while self._alive:
                now, wait = time.monotonic(), None
                for state in self._states.values():
                    if state.running and state.time_limit is not None and state.time_limit != float("inf"):
                        left = state.since + state.time_limit - now
                        if left <= 0:
                            if self._error is None:
                                self._error = RunError(f"node {state.node.id} ran longer than {state.time_limit:g} seconds "
                                                       f"in round {state.round}", state.node.id)
                            self._stop = True
                            lock.notify_all()
                            deadline = now + 1.0             # idle workers leave at once; a node still running is left
                            while (self._alive > sum(1 for s in self._states.values() if s.running)
                                   and time.monotonic() < deadline):
                                lock.wait(0.01)
                            return
                        wait = left if wait is None else min(wait, left)
                lock.wait(wait)

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
                if self._timed:
                    state.since = time.monotonic()
                if state.rejected:
                    state.rejected = {}
                state.delivered = False
                if state.inner:
                    state.inner = []
                self._active += 1
                try:
                    taken, cause, took = self._take(state)
                except Exception as exc:   # noqa: BLE001 - a protocol's decode failed
                    self._fail(state, exc)
                    continue
                self._result.activations += 1
            act = _Act(state.node.id, state.round, took) if self._journal is not None else None
            node, ctx = state.node, _Context(self, state, cause, act)
            if self._observe is not None:
                self._flush()
                self._tell("started", node=node.id, round=state.round, took=list(took))
            try:
                if not state.opened:
                    state.opened = True
                    node.open(ctx)
                while True:
                    try:
                        returned = node.run(ctx, taken)
                        if returned is not None:
                            if not isinstance(returned, Mapping):
                                raise NodeError(f"run must return a mapping of output port to payload, or None; got {type(returned).__name__}")
                            for port, payload in returned.items():
                                self._emit(state, port, payload, state.round, cause, act)
                        break
                    except Invalid as exc:
                        # the node did not put it right itself: run it again, if that repeats nothing
                        if not exc.again or state.delivered or any(isinstance(v, Stream) for v in taken.values()):
                            raise
                        ctx.problems.append(str(exc))
                if act is not None:
                    snapshot = node.snapshot()
                    if snapshot:
                        act.snaps[node.id] = dict(snapshot)
            except BaseException as exc:   # noqa: BLE001 - whatever a node raises ends the run, an interrupt included
                with lock:
                    if self._over:
                        return
                    self._fail(state, exc)
                if self._observe is not None:
                    self._tell("failed", node=node.id, round=state.round, error=str(exc))
                continue
            if ctx._cut:
                # it was told to stop and may have left its work half done: it is not on record as having run,
                # so it runs again when the run is taken up
                with lock:
                    if self._over:
                        return
                    self._end_streams(state)
                    state.running = False
                    self._active -= 1
                    self._stop = True
                    lock.notify_all()
                if self._observe is not None:
                    self._tell("stopped", node=node.id, round=state.round)
                continue
            if self._observe is not None:
                self._tell("finished", node=node.id, round=state.round)
            with lock:
                if self._over:             # it overran and the engine has gone: what it did is thrown away
                    return
                self._end_streams(state)
                if act is not None:
                    try:
                        self._commit(act)
                    except Exception as exc:   # noqa: BLE001 - something it sent is not plain data
                        self._fail(state, RunError(f"cannot be journaled: {exc}"))
                        continue
                    for path in state.inner:       # the activation is on record: what ran inside it is not needed again
                        _discard(path)
                state.running = False
                self._active -= 1
                self._consider(state)
                if not self._ready and not self._active:
                    lock.notify_all()
                elif self._ready:
                    lock.notify()

    def _commit(self, act: _Act) -> None:
        self._journal.write({"kind": "act", "node": act.node, "round": act.round, "took": list(act.took),
                             "sent": [pack(e) for e in act.sent],
                             "out": [[name, pack_body(payload)] for name, payload in act.out],
                             "limited": act.limited, "calls": act.calls, "snaps": act.snaps,
                             **({"problems": act.problems} if act.problems else {})})

    def _tell(self, event: str, **about) -> None:
        observe = self._observe
        if observe is None:
            return
        try:
            observe({"event": event, **about})
        except Exception as exc:   # noqa: BLE001 - an observer that fails must not go unnoticed
            with self._lock:
                if self._error is None:
                    self._error = RunError(f"the observer failed on {event}: {exc}")
                self._stop = True
                self._lock.notify_all()

    def _end_streams(self, state: _State) -> None:
        if state.streams:
            for stream in state.streams.values():
                if stream is not None:
                    stream._close()
            state.streams.clear()

    def _fail(self, state: _State, exc: BaseException) -> None:
        self._end_streams(state)
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
        if state.running or state.queued or self._stop or state.called:
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

    def _deliver(self, feed: _Feed, body: Any, round: int, cause: tuple) -> Envelope:
        envelope = self._envelope(feed, body, round, cause)
        self._place(feed, envelope)
        return envelope

    def _envelope(self, feed: _Feed, body: Any, round: int, cause: tuple) -> Envelope:
        feed.sent += 1
        if isinstance(body, Ref):
            body_digest = body.digest
        else:
            try:
                body_digest = digest(body)
            except (TypeError, ValueError):
                body_digest = ""    # not plain data: it can travel inside the process, but it has no content hash
        self._result.envelopes += 1
        return Envelope(f"{feed.id}#{feed.sent}", feed.id, feed.sent, round, body, body_digest, cause)

    def _place(self, feed: _Feed, envelope: Envelope) -> None:
        """Put an envelope in its feed's mailbox and see whether the receiving node can now run."""
        box, port = feed.box, feed.port
        if feed.passive:
            box.clear()
            box.append(envelope)
            return
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
        """The inputs of one activation, the ids of the envelopes they came in, and which of those were used up."""
        inputs: dict[str, Any] = {}
        cause: list[str] = []

        def read(feed):
            envelope = self._pop(feed)
            cause.append(envelope.id)
            if feed.protocol is None:
                return envelope.body
            try:
                return feed.protocol.decode(envelope.body, feed.port.port)
            except ValueError as exc:
                return self._arrived_wrong(state, feed, envelope.body, exc)

        if state.any:
            while state.arrivals:
                feed = state.arrivals.popleft()
                if feed.box:    # a latest feed may have been overwritten and already read
                    value = read(feed)
                    inputs[feed.port.port.name] = [value] if feed.port.port.many else value
                    break
        else:
            for name, port in state.ports.items():
                if not port.ready:
                    continue
                if port.forward and port.forward_full == len(port.forward):
                    values = [read(feed) for feed in port.forward]
                else:
                    values = [read(next(feed for feed in port.back if feed.box))]
                inputs[name] = values if port.port.many else values[0]
        took = tuple(cause)
        for port in state.passive:      # the newest value of each edge that does not trigger: read, and left there
            values = []
            for feed in port.passive:
                if feed.box:
                    cause.append(feed.box[0].id)
                    try:
                        values.append(feed.protocol.decode(feed.box[0].body, port.port))
                    except ValueError as exc:
                        values.append(self._arrived_wrong(state, feed, feed.box[0].body, exc))
            if values:
                inputs[port.port.name] = values if port.port.many else values[0]
        return inputs, tuple(cause), took

    def _arrived_wrong(self, state: _State, feed: _Feed, body: Any, exc: ValueError) -> Any:
        """What arrived did not pass its protocol: the receiving port fails (the exception goes on) or warns."""
        port = feed.port.port
        if port.invalid != "warn":
            raise Invalid(port.name, str(exc)) from None
        value = feed.protocol.decode_anyway(body, port)
        self._problem(state, port.name, str(exc), "warned", None, state.round)
        return value

    def _problem(self, state: _State, port: str, problem: str, handled: str, act: _Act | None, round: int) -> None:
        """Record a problem and what became of it. Called under the lock."""
        record = {"node": state.node.id, "round": round, "port": port, "problem": problem, "handled": handled}
        self._result.problems.append(record)
        if act is not None:
            act.problems.append(record)
        if self._observe is not None:
            self._pending.append(record)

    def _invalid(self, state: _State, port: str, payload: Any, feeds, exc: ValueError, act: _Act | None, round: int) -> list:
        """A payload failed a check on its way out. Give it back to the node, let it through with a warning
        (returning the bodies to send), or fail the node."""
        source = state.node.outputs[port]
        problem = exc.problem if isinstance(exc, Invalid) else str(exc)
        repairs = source.repairs if source.repairs is not None else getattr(state.node, "repairs", 0)
        with self._lock:
            given_back = state.rejected.get(port, 0)
            if given_back < repairs:
                state.rejected[port] = given_back + 1
                self._problem(state, port, problem, "rejected", act, round)
            elif source.invalid == "warn":
                try:
                    bodies = [feed.protocol.encode_anyway(payload, port_) for feed, port_ in feeds]
                except ValueError as cannot:
                    raise Invalid(port, f"{problem}; and it cannot be sent on as it is: {cannot}") from None
                self._problem(state, port, problem, "warned", act, round)
                return bodies
            else:
                raise Invalid(port, problem if not given_back else f"{problem} (after {given_back} corrections)") from None
        self._flush()
        raise Invalid(port, problem, again=True) from None

    def _flush(self) -> None:
        """Tell the observer of the problems recorded under the lock."""
        if self._observe is not None:
            while self._pending:
                record = self._pending.popleft()
                self._tell(record["handled"], **{k: v for k, v in record.items() if k != "handled"})

    def _emit(self, state: _State, port: str, payload: Any, round: int, cause: tuple, act: _Act | None = None) -> None:
        try:
            feeds, names = state.outs[port]
        except KeyError:
            raise NodeError(f"{state.node.id} has no output port {port!r}") from None
        if port in state.calls:
            raise NodeError(f"{state.node.id}.{port} makes a call: use ctx.call, not emit")
        try:
            out = state.node.outputs[port]
            bodies = [feed.protocol.encode(payload, source) for feed, source in feeds]   # validate before anything is sent
            if not feeds:
                out.schema.check(payload)
            if out.check:                     # the port's own checks come last: they may rely on the shape
                found = out.problem(payload)
                if found:
                    raise Invalid(port, found)
        except ValueError as exc:
            bodies = self._invalid(state, port, payload, feeds, exc, act, round)
            self._flush()
        carry = self._carry
        if carry is not None:
            bodies = [body if feed.stream else carry(feed.id, body) for (feed, _), body in zip(feeds, bodies)]
        if self._observe is not None:
            self._tell("emitted", node=state.node.id, round=round, port=port, payload=payload)
        with self._lock:
            if self._over:
                raise NodeError("the run has ended")
            state.delivered = True
            for name in names:
                self._result.outputs[name].append(payload)
                if act is not None:
                    act.out.append((name, payload))
            for (feed, _), body in zip(feeds, bodies):
                if feed.stream and feed.id in state.streams:
                    if state.streams[feed.id] is not None:
                        state.streams[feed.id]._put(body)
                    continue
                if feed.limit is not None and feed.sent >= feed.limit:
                    self._limited[feed.id] = None
                    if act is not None and feed.id not in act.limited:
                        act.limited.append(feed.id)
                    if feed.stream:
                        state.streams[feed.id] = None
                    continue
                if feed.stream:                          # the first chunk of this activation opens the stream
                    stream = state.streams[feed.id] = Stream(feed.protocol, feed.port.port, keep=act is not None)
                    stream._put(body)
                    body = stream
                envelope = self._envelope(feed, body, round, cause)
                if act is not None:
                    act.sent.append(envelope)
                if envelope.id in self._orphans:         # its receiver dealt with it before the restart
                    self._orphans.discard(envelope.id)
                else:
                    self._place(feed, envelope)
            if self._ready:
                self._lock.notify()

    def _call(self, state: _State, port: str, payload: Any, act: _Act | None = None) -> Any:
        """Run the node at the other end of a call edge now, in this thread, and return its answer."""
        feed = state.calls.get(port)
        if feed is None:
            raise NodeError(f"{state.node.id} has no call port {port!r}")
        body = feed.protocol.encode(payload, state.node.outputs[port])
        target = feed.port.state
        node = target.node
        with target.called:
            with self._lock:
                if self._stop:
                    raise NodeError("the run is stopping")
                feed.sent += 1
                target.round += 1
                self._result.envelopes += 1
                self._result.activations += 1
                ctx = _Context(self, target, (f"{feed.id}#{feed.sent}",), act)
                if act is not None:
                    act.calls.append(node.id)
                target.rejected = {}
            if self._observe is not None:
                self._tell("started", node=node.id, round=ctx.round, took=list(ctx._cause), by=state.node.id)
            failed = True
            try:
                if not target.opened:
                    target.opened = True
                    node.open(ctx)
                value = feed.protocol.decode(body, feed.port.port)
                returned = node.run(ctx, {feed.port.port.name: [value] if feed.port.port.many else value})
                if act is not None:
                    snapshot = node.snapshot()
                    if snapshot:
                        act.snaps[node.id] = dict(snapshot)
                if returned is None:
                    failed = False
                    return None
                if not isinstance(returned, Mapping):
                    raise NodeError(f"run must return a mapping of output port to payload, or None; got {type(returned).__name__}")
                for name, item in returned.items():
                    self._emit(target, name, item, ctx.round, ctx._cause, act)
                failed = False
                return returned.get(feed.reply) if feed.reply is not None else None
            except Invalid as exc:      # the called node did not put its own output right: that is its failure,
                raise NodeError(f"{node.id} could not answer: {exc}") from None      # not something for the caller to repair
            finally:
                if self._observe is not None:
                    self._tell("failed" if failed else "finished", node=node.id, round=ctx.round, by=state.node.id)
                with self._lock:
                    self._end_streams(target)
