"""What is sent is checked as it leaves and again as it arrives; this module decides what follows a problem.

On the way out a payload passes the port's schema, the protocol of each edge it leaves on, and the port's own
checks. When one finds a problem the port decides: while it has repairs left in this activation the node is
told (``Invalid`` with ``again``), then a port that warns lets the payload through and the problem is
recorded, otherwise the node fails. On the way in, what a protocol cannot decode fails the receiving port or,
when it warns, is let through.

Every problem, whatever became of it, is kept in one list (the run's ``Result.problems``) and waits to be
told to the observer, which happens outside the lock (``flush``).
"""
from __future__ import annotations

from collections import deque
from typing import Any, Callable

from ..base import Invalid
from .recorder import Act
from .wiring import Feed, State


class Checks:
    __slots__ = ("problems", "pending", "telling")

    def __init__(self, problems: list[dict], telling: bool):
        self.problems = problems        # every problem of the run
        self.pending: deque = deque()   # recorded under the lock, not yet told to the observer
        self.telling = telling

    def record(self, state: State, port: str, problem: str, handled: str, act: Act | None, round: int) -> None:
        """Record a problem and what became of it. Called under the lock."""
        record = {"node": state.node.id, "round": round, "port": port, "problem": problem, "handled": handled}
        self.problems.append(record)
        if act is not None:
            act.problems.append(record)
        if self.telling:
            self.pending.append(record)

    def flush(self, tell: Callable[..., None]) -> None:
        """Tell the observer of the problems recorded under the lock."""
        if self.telling:
            pending = self.pending
            while pending:
                record = pending.popleft()
                tell(record["handled"], **{k: v for k, v in record.items() if k != "handled"})

    @staticmethod
    def outgoing(state: State, port: str, payload: Any, feeds) -> list:
        """The bodies ``payload`` becomes on each edge it leaves on, once it has passed every check; a
        ``ValueError`` says what it failed."""
        out = state.node.outputs[port]
        bodies = [feed.protocol.encode(payload, source) for feed, source in feeds]   # validate before anything is sent
        if not feeds:
            out.schema.check(payload)
        if out.check:                     # the port's own checks come last: they may rely on the shape
            found = out.problem(payload)
            if found:
                raise Invalid(port, found)
        return bodies

    def refused(self, state: State, port: str, payload: Any, feeds, exc: ValueError, act: Act | None, round: int,
                lock) -> list:
        """A payload failed a check on its way out. Give it back to the node, let it through with a warning
        (returning the bodies to send), or fail the node."""
        source = state.node.outputs[port]
        problem = exc.problem if isinstance(exc, Invalid) else str(exc)
        repairs = source.repairs if source.repairs is not None else getattr(state.node, "repairs", 0)
        with lock:
            given_back = state.rejected.get(port, 0)
            if given_back < repairs:
                state.rejected[port] = given_back + 1
                self.record(state, port, problem, "rejected", act, round)
            elif source.invalid == "warn":
                try:
                    bodies = [feed.protocol.encode_anyway(payload, port_) for feed, port_ in feeds]
                except ValueError as cannot:
                    raise Invalid(port, f"{problem}; and it cannot be sent on as it is: {cannot}") from None
                self.record(state, port, problem, "warned", act, round)
                return bodies
            else:
                raise Invalid(port, problem if not given_back else f"{problem} (after {given_back} corrections)") from None
        raise Invalid(port, problem, again=True) from None

    def arrived(self, state: State, feed: Feed, body: Any) -> Any:
        """What a node finds for ``body`` arriving on ``feed``. What does not pass its protocol fails the
        receiving port (the exception goes on) or, when the port warns, is let through and recorded."""
        if feed.protocol is None:
            return body
        try:
            value = feed.protocol.decode(body, feed.port.port)
            if feed.guard is not None:
                feed.guard.check(value)
            return value
        except ValueError as exc:
            port = feed.port.port
            if port.invalid != "warn":
                raise Invalid(port.name, str(exc)) from None
            value = feed.protocol.decode_anyway(body, port)
            self.record(state, port.name, str(exc), "warned", None, state.round)
            return value
