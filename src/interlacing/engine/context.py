"""What a node sees of the run while one of its activations runs: ``ctx``."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..base import Context, Graph, Protocols

if TYPE_CHECKING:
    from .engine import Engine
    from .recorder import Act
    from .wiring import State


class RunContext(Context):
    __slots__ = ("node", "round", "problems", "_engine", "_state", "_cause", "_act", "_cut")

    def __init__(self, engine: "Engine", state: "State", cause: tuple, act: "Act | None" = None):
        self.node, self.round, self.problems = state.node.id, state.round, []
        self._cut = False       # the node has been told the run is stopping: what it did may not be all of it
        self._engine, self._state, self._cause, self._act = engine, state, cause, act

    def emit(self, port: str, payload: Any) -> None:
        self._engine._emit(self._state, port, payload, self.round, self._cause, self._act)

    def call(self, port: str, payload: Any) -> Any:
        return self._engine._call(self._state, port, payload, self._act)

    def stopping(self) -> bool:
        if self._engine._stopping():
            self._cut = True
            return True
        return False

    def engine(self, graph: Graph, protocols: Protocols | None = None, *, workers: int = 0, firm: bool = False) -> "Engine":
        return self._engine._inner(self, graph, protocols, workers, firm)

    def use(self, **amounts: float) -> None:
        self._engine._use(self._state, self.round, amounts)

    def once(self, name: str, do: Callable[[], Any]) -> Any:
        return self._engine._once(self._state, self.round, name, do)

    @property
    def key(self) -> str:
        return f"{self._engine._id}/{self.node}#{self.round}"

    @property
    def within(self) -> tuple:
        return self._engine._within
