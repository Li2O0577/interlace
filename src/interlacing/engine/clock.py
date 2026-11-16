"""How long each activation may take, and which running activation has taken too long."""
from __future__ import annotations

from typing import Iterable

from .result import RunError
from .wiring import State

INF = float("inf")


def set_limits(states: Iterable[State], time_limit: float | None, firm: bool) -> bool:
    """Give each node its time limit: its own ``time_limit`` attribute, else the run's. With ``firm`` the run's
    limit is a ceiling, except for a kind of node that waits by nature. Returns whether any node is timed."""
    timed = False
    for state in states:
        own = getattr(state.node, "time_limit", None)
        if own is not None and firm and time_limit is not None:
            waits = own == INF and getattr(type(state.node), "time_limit", None) == INF
            own = own if waits else min(own, time_limit)     # a kind of node that waits by nature keeps doing so
        state.time_limit = own if own is not None else time_limit
        if state.time_limit is not None and not state.time_limit > 0:
            raise RunError(f"{state.node.id}: time_limit must be a positive number of seconds", state.node.id)
        timed = timed or state.time_limit is not None
    return timed


def overrun(states: Iterable[State], now: float) -> tuple[State | None, float | None]:
    """The first running activation past its limit, if there is one; otherwise how long until the next limit
    falls due (None when no running activation has a limit)."""
    wait = None
    for state in states:
        if state.running and state.time_limit is not None and state.time_limit != INF and state.status != "paused":
            left = state.since + state.time_limit - now
            if left <= 0:
                return state, None
            wait = left if wait is None else min(wait, left)
    return None, wait
