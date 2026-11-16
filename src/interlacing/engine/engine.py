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

The engine itself only drives the run; each part of the work is done by a piece of its own:

* ``wiring``: the mailboxes, ports and node states a graph becomes;
* ``scheduler``: putting envelopes in, taking them out, and which nodes are ready;
* ``checks``: what is checked on the way out and in, and what follows a problem;
* ``ledger``: the budget and what ``ctx.once`` keeps;
* ``recorder``: what goes into the journal, and rebuilding a run from it;
* ``clock``: time limits, and which activation has overrun;
* ``context``: the ``ctx`` a node is given.
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Callable, Mapping

from ..base import Graph, Invalid, NodeError, Protocols
from .checks import Checks
from .clock import overrun, set_limits
from .context import RunContext
from .journal import Journal
from .ledger import Ledger, amounts
from .recorder import Act, Recorder, discard, restore, under
from .result import OverBudget, Result, RunError
from .scheduler import Scheduler
from .stream import Stream
from .transport import Transport
from .wiring import Feed, State, wire

__all__ = ["Engine", "OverBudget", "Result", "RunError"]


class Engine:
    """One run of one graph. Build a new Engine to run again."""

    def __init__(self, graph: Graph, protocols: Protocols | None = None, *, workers: int = 0,
                 stopping: Callable[[], bool] | None = None, transport: Transport | None = None,
                 journal: Journal | None = None, time_limit: float | None = None,
                 observe: Callable[[Mapping], None] | None = None, budget: Mapping[str, float] | None = None,
                 firm: bool = False):
        if workers < 0:
            raise ValueError("workers cannot be negative")
        if time_limit is not None and not time_limit > 0:
            raise ValueError("time_limit must be a positive number of seconds")
        if protocols is None:
            protocols = graph.protocols
            if protocols is None:
                raise RunError("the graph was not compiled with protocols: give the engine the ones to run it with")
        self.graph, self.protocols, self.workers = graph, protocols, workers
        self._observe = observe
        self._ledger = Ledger(budget)
        self._id: str | None = None         # names this run: the first part of every activation's key
        self._limit = time_limit
        self._firm = bool(firm)             # no node may give itself longer than time_limit
        self._parent: tuple | None = None   # (outer engine, the outer node's state, its activation's record)
        self._within: tuple = ()            # (node id, round) of each node this run is inside, outermost first
        self._over = False                # set once the engine has returned: a node still running is on its own
        self._alive = 0                   # worker threads that have not returned
        self._outer = stopping
        self._carry = transport.carry if transport is not None else None
        self._recorder = Recorder(journal) if journal is not None else None
        self._orphans: set[str] = set()   # after a resume: envelopes already dealt with whose sender must run again
        self._lock = threading.Condition()
        self._sched = Scheduler()
        self._active = 0
        self._error: RunError | None = None
        self._result = Result(outputs={name: [] for name in graph.outputs})
        self._checks = Checks(self._result.problems, observe is not None)
        self._limited: dict[str, None] = {}
        self._used = False
        wiring = self._wiring = wire(graph, protocols)
        self._states, self._feeds, self._inputs = wiring.states, wiring.feeds, wiring.inputs
        self._timed = set_limits(self._states.values(), time_limit, self._firm)

    @property
    def used(self) -> dict[str, float]:
        """What the nodes have reported using so far, added up."""
        with self._lock:
            return dict(self._ledger.meter)

    # ---- running ---------------------------------------------------------------------------------------

    def run(self, inputs: Mapping[str, Any] | None = None) -> Result:
        """Feed the graph's inputs, run until nothing is left to do, and return what reached its outputs."""
        self._begin()
        recorder = self._recorder
        if recorder is not None and recorder.read():
            raise RunError("the journal already holds a run: resume it, or use a new journal")
        inputs = dict(inputs or {})
        unknown = inputs.keys() - self._inputs.keys()
        if unknown:
            raise RunError(f"the graph has no input {sorted(unknown)}")
        sched = self._sched
        with self._lock:
            sent = []
            for name, payload in inputs.items():
                feed = self._inputs[name]
                try:
                    feed.port.port.schema.check(payload)
                except ValueError as exc:
                    raise RunError(f"graph input {name}: {exc}") from None
                envelope = sched.envelope(feed, payload, 0, ())
                sched.place(feed, envelope)
                sent.append(envelope)
            self._id = self._id or secrets.token_hex(8)
            if recorder is not None:
                try:
                    recorder.run(self._id, self.graph.digest, sent)
                except (TypeError, ValueError) as exc:
                    raise RunError(f"the graph's inputs cannot be journaled: {exc}") from None
            for state in self._states.values():
                sched.consider(state)
        return self._finish()

    def go(self, inputs: Mapping[str, Any] | None = None, *, fresh: bool = False) -> Result:
        """Run, or, when the journal already holds a run of this graph, take that run up. ``fresh`` throws
        an earlier run away."""
        recorder = self._recorder
        if recorder is not None:
            records = recorder.read()
            if records:
                if not fresh and under(records) == self.graph.digest:
                    return self.resume()
                recorder.close()
                recorder.journal.path.unlink()
        return self.run(inputs)

    def resume(self, *, change: bool = False) -> Result:
        """Take up the run in the journal where it stopped. ``change=True`` allows this engine's graph to
        differ from the one the journal was written under."""
        self._begin()
        recorder = self._recorder
        if recorder is None:
            raise RunError("resume needs a journal")
        records = recorder.read()
        if not records:
            raise RunError("the journal is empty: there is nothing to resume")
        was = under(records)
        assert was is not None
        changed = was != self.graph.digest
        if changed and not change:
            raise RunError(f"the journal was written under another graph ({was[:12]}); "
                           f"pass change=True to continue under this one ({self.graph.digest[:12]})")
        ended = records[-1]["kind"] == "end" and not changed
        with self._lock:
            run_id, self._orphans, dropped = restore(records, self._wiring, self._sched, self._ledger, self._result,
                                                     self._limited)
            self._id = self._id or run_id
            if not ended:
                over = self._ledger.over()
                if over:
                    raise RunError(f"{over}: it goes on only under a larger budget")
            if changed:
                recorder.change(was, self.graph.digest, dropped)
            if not ended:
                for state in self._states.values():
                    self._sched.consider(state)
        return self._finish(ended)

    def stop(self) -> None:
        """Ask the run to end: nothing new starts; running nodes see ``ctx.stopping()``."""
        with self._lock:
            self._sched.stopped = True
            self._lock.notify_all()

    # ---- the same, awaited ------------------------------------------------------------------------------

    async def arun(self, inputs: Mapping[str, Any] | None = None) -> Result:
        """``run``, for a caller that is a coroutine: the run goes on in a thread and the event loop stays
        free. Cancelling the awaiting task asks the run to stop."""
        return await self._awaited(self.run, inputs)

    async def aresume(self, *, change: bool = False) -> Result:
        """``resume``, awaited."""
        return await self._awaited(lambda: self.resume(change=change))

    async def ago(self, inputs: Mapping[str, Any] | None = None, *, fresh: bool = False) -> Result:
        """``go``, awaited."""
        return await self._awaited(lambda: self.go(inputs, fresh=fresh))

    async def _awaited(self, do: Callable, *args) -> Result:
        import asyncio
        try:
            return await asyncio.to_thread(do, *args)
        except asyncio.CancelledError:
            self.stop()
            raise

    # ---- the life of a run -----------------------------------------------------------------------------

    def _begin(self) -> None:
        if self._used:
            raise RunError("this engine has already run; build a new one")
        self._used = True

    def _stopping(self) -> bool:
        return self._sched.stopped or (self._outer is not None and bool(self._outer()))

    def _finish(self, ended: bool = False) -> Result:
        recorder = self._recorder
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
            if recorder is not None:
                if not ended and not self._sched.stopped and self._error is None:
                    recorder.end()
                recorder.close()
        result = self._result
        if self._parent is not None and result.problems:
            outer, state, act = self._parent
            outer._adopt(state, act, result.problems)
        if self._error:
            self._flush()
            self._error.problems = result.problems
            self._error.used = dict(self._ledger.meter)
            self._tell("ended", complete=False, error=str(self._error))
            cause = self._error.__cause__
            if cause is not None and not isinstance(cause, Exception):
                raise cause                 # an interrupt or an exit request passes through as itself
            raise self._error
        result.envelopes = self._sched.envelopes
        result.rounds = {id: state.round for id, state in self._states.items() if state.round}
        result.limited = tuple(self._limited)
        result.complete = not self._sched.stopped
        result.used = dict(self._ledger.meter)
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
        states = self._states.values()
        with lock:
            while self._alive:
                late, wait = overrun(states, time.monotonic())
                if late is not None:
                    if self._error is None:
                        self._error = RunError(f"node {late.node.id} ran longer than {late.time_limit:g} seconds "
                                               f"in round {late.round}", late.node.id)
                    self._sched.stopped = True
                    lock.notify_all()
                    deadline = time.monotonic() + 1.0    # idle workers leave at once; a node still running is left
                    while (self._alive > sum(1 for s in states if s.running)
                           and time.monotonic() < deadline):
                        lock.wait(0.01)
                    return
                lock.wait(wait)

    def _work(self) -> None:
        lock, sched, checks = self._lock, self._sched, self._checks
        while True:
            with lock:
                if self._outer is not None and not sched.stopped and self._outer():
                    sched.stopped = True
                while not sched.ready and self._active and not sched.stopped:
                    lock.wait()
                if sched.stopped or not sched.ready:
                    lock.notify_all()
                    return
                state = sched.ready.popleft()
                state.begin()
                if self._timed:
                    state.since = time.monotonic()
                    lock.notify_all()          # the watcher must learn that a clock has started
                self._active += 1
                try:
                    taken, cause, took = sched.take(state, lambda feed, body: checks.arrived(state, feed, body))
                except Exception as exc:   # noqa: BLE001 - a protocol's decode failed
                    self._fail(state, exc)
                    continue
                self._result.activations += 1
            act = Act(state.node.id, state.round, took) if self._recorder is not None else None
            node, ctx = state.node, RunContext(self, state, cause, act)
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
                    sched.stopped = True
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
                    assert self._recorder is not None
                    try:
                        self._recorder.act(act)
                    except Exception as exc:   # noqa: BLE001 - something it sent is not plain data
                        self._fail(state, RunError(f"cannot be journaled: {exc}"))
                        continue
                    for path in state.inner:       # the activation is on record: what ran inside it is not needed again
                        discard(path)
                state.running = False
                self._active -= 1
                sched.consider(state)
                if not sched.ready and not self._active:
                    lock.notify_all()
                elif sched.ready:
                    lock.notify()

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
                self._sched.stopped = True
                self._lock.notify_all()

    def _flush(self) -> None:
        """Tell the observer of the problems recorded under the lock."""
        self._checks.flush(self._tell)

    @staticmethod
    def _end_streams(state: State) -> None:
        if state.streams:
            for stream in state.streams.values():
                if stream is not None:
                    stream._close()
            state.streams.clear()

    def _fail(self, state: State, exc: BaseException) -> None:
        """A node failed: the run stops. Called under the lock."""
        self._end_streams(state)
        state.running = False
        self._active -= 1
        if self._error is None:
            self._error = RunError(f"node {state.node.id} failed in round {state.round}: {exc}", state.node.id)
            self._error.__cause__ = exc
        self._sched.stopped = True
        self._lock.notify_all()

    def _close(self) -> None:
        for state in self._states.values():
            if state.opened:
                try:
                    state.node.close(RunContext(self, state, ()))
                except Exception as exc:   # noqa: BLE001
                    if self._error is None:
                        self._error = RunError(f"node {state.node.id} failed to close: {exc}", state.node.id)

    # ---- what a node asks of the run through ctx ---------------------------------------------------------

    def _emit(self, state: State, port: str, payload: Any, round: int, cause: tuple, act: Act | None = None) -> None:
        try:
            feeds, names = state.outs[port]
        except KeyError:
            raise NodeError(f"{state.node.id} has no output port {port!r}") from None
        if port in state.calls:
            raise NodeError(f"{state.node.id}.{port} makes a call: use ctx.call, not emit")
        try:
            bodies = Checks.outgoing(state, port, payload, feeds)
        except ValueError as exc:
            try:
                bodies = self._checks.refused(state, port, payload, feeds, exc, act, round, self._lock)
            finally:
                self._flush()
        carry = self._carry
        if carry is not None:
            bodies = [body if feed.stream else carry(feed.id, body) for (feed, _), body in zip(feeds, bodies)]
        if self._observe is not None:
            self._tell("emitted", node=state.node.id, round=round, port=port, payload=payload)
        sched = self._sched
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
                envelope = sched.envelope(feed, body, round, cause)
                if act is not None:
                    act.sent.append(envelope)
                if envelope.id in self._orphans:         # its receiver dealt with it before the restart
                    self._orphans.discard(envelope.id)
                else:
                    sched.place(feed, envelope)
            if sched.ready:
                self._lock.notify()

    def _call(self, state: State, port: str, payload: Any, act: Act | None = None) -> Any:
        """Run the node at the other end of a call edge now, in this thread, and return its answer."""
        feed: Feed | None = state.calls.get(port)
        if feed is None:
            raise NodeError(f"{state.node.id} has no call port {port!r}")
        body = feed.protocol.encode(payload, state.node.outputs[port])
        target = feed.port.state
        node = target.node
        assert target.called is not None
        with target.called:
            with self._lock:
                if self._sched.stopped:
                    raise NodeError("the run is stopping")
                feed.sent += 1
                target.round += 1
                self._sched.envelopes += 1
                self._result.activations += 1
                ctx = RunContext(self, target, (f"{feed.id}#{feed.sent}",), act)
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
                if feed.guard is not None:
                    feed.guard.check(value)
                returned = node.run(ctx, {feed.port.port.name: [value] if feed.port.port.many else value})
                if ctx._cut:            # it was told to stop: the caller must not take what it left for an answer
                    raise NodeError("the run is stopping")
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

    def _inner(self, ctx: RunContext, graph: Graph, protocols: Protocols | None, workers: int, firm: bool = False) -> "Engine":
        """An engine for a graph that runs inside one activation of one of this engine's nodes."""
        state = ctx._state
        journal = None
        if self._recorder is not None:
            journal = self._recorder.inner(state.node.id, ctx.round)
            with self._lock:
                if journal.path not in state.inner:
                    state.inner.append(journal.path)
        told, id = self._observe, state.node.id
        observe = None if told is None else lambda event: told({**event, "within": [id, *event.get("within", ())]})
        inner = Engine(graph, protocols, workers=workers, stopping=ctx.stopping, journal=journal,
                       time_limit=self._limit, observe=observe, firm=firm or self._firm)
        inner._parent = (self, state, ctx._act)
        inner._within = (*self._within, (state.node.id, ctx.round))
        inner._id = ctx.key
        return inner

    def _use(self, state: State, round: int, used: Mapping[str, float]) -> None:
        """Add what a node reports using to the run's count; over the budget, the run fails."""
        used = amounts(used, "use")
        if not used:
            return
        with self._lock:
            if self._over:
                return
            over = self._ledger.add(used)
            if self._recorder is not None:
                self._recorder.use(state.node.id, round, used)
            if over and self._error is None:        # the run ends even if the node goes on regardless
                self._error = RunError(over, state.node.id)
                self._sched.stopped = True
                self._lock.notify_all()
            total = dict(self._ledger.meter)
        if self._observe is not None:
            self._tell("used", node=state.node.id, round=round, amounts=used, total=total)
        if self._parent is not None:
            outer, outer_state, _ = self._parent
            outer._use(outer_state, self._within[-1][1], used)
        if over:
            raise OverBudget(over)

    def _once(self, state: State, round: int, name: str, do: Callable[[], Any]) -> Any:
        """What ``do`` returned the first time this activation asked under this name; ``do`` is called only then."""
        if not isinstance(name, str) or not name:
            raise NodeError("once needs a name for what is done")
        ledger, id = self._ledger, state.node.id
        with self._lock:
            if ledger.has(id, round, name):
                return ledger.get(id, round, name)
        value = do()
        with self._lock:
            if self._recorder is not None:
                try:
                    self._recorder.once(id, round, name, value)
                except (TypeError, ValueError) as exc:
                    raise NodeError(f"what once keeps under {name!r} must be plain data: {exc}") from None
            ledger.keep(id, round, name, value)
        return value

    def _adopt(self, state: State, act: Act | None, problems: list[dict]) -> None:
        """Take on the problems of a run that happened inside one of this engine's nodes."""
        with self._lock:
            for problem in problems:
                record = {**problem, "node": f"{state.node.id}/{problem['node']}"}
                self._result.problems.append(record)
                if act is not None:
                    act.problems.append(record)

