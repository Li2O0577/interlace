"""The run's accounts: what the nodes report using, against the budget, and what ``ctx.once`` has kept.

Nothing here takes a lock or writes a journal; the engine does that around it.
"""
from __future__ import annotations

from typing import Any, Mapping


def amounts(given: Mapping[str, float], what: str) -> dict[str, float]:
    """``given`` as a dict, after making sure every amount is a finite number that is not negative."""
    for name, amount in given.items():
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not amount >= 0 or amount == float("inf"):
            raise ValueError(f"{what}: {name} must be a number that is not negative, not {amount!r}")
    return dict(given)


class Ledger:
    __slots__ = ("budget", "meter", "kept")

    def __init__(self, budget: Mapping[str, float] | None = None):
        self.budget = amounts(budget or {}, "budget")
        self.meter: dict[str, float] = {}   # what the nodes have reported using
        self.kept: dict[tuple, Any] = {}    # what ctx.once has done, by (node, round, name)

    def add(self, used: Mapping[str, float]) -> str | None:
        """Count ``used`` in. Returns why the run is now over its budget, or None."""
        meter, over = self.meter, None
        for name, amount in used.items():
            meter[name] = meter.get(name, 0) + amount
            limit = self.budget.get(name)
            if over is None and limit is not None and meter[name] > limit:
                over = f"the run has used {meter[name]:g} {name}, and its budget is {limit:g}"
        return over

    def over(self) -> str | None:
        """Why the run is already over its budget, or None: a run in that state goes on only under a larger one."""
        for name, limit in self.budget.items():
            if self.meter.get(name, 0) > limit:
                return f"the run has used {self.meter[name]:g} {name}, and its budget is {limit:g}"
        return None

    def keep(self, node: str, round: int, name: str, value: Any) -> None:
        self.kept[(node, round, name)] = value

    def has(self, node: str, round: int, name: str) -> bool:
        return (node, round, name) in self.kept

    def get(self, node: str, round: int, name: str) -> Any:
        return self.kept[(node, round, name)]
