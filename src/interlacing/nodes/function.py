"""A node that is one Python function."""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Iterable, Mapping

from ..base import Firing, Node, NodeError, Port


def named(function: Callable) -> str:
    """A function's dotted name, for a node's ``config``."""
    return f"{getattr(function, '__module__', '')}.{getattr(function, '__qualname__', type(function).__qualname__)}"


class FunctionNode(Node):
    """Calls ``function`` with the arrived inputs as keyword arguments, by port name.

    With one output port (not counting call ports), what the function returns is that port's payload (``None`` sends nothing). With
    several, it returns a mapping of port name to payload; ports it leaves out send nothing. With
    ``context=True`` the function also receives the engine's context as its first argument, to emit several
    times or to see that the run is stopping.

    The function may be ``async def``: it is run to its end in an event loop of its own, in the thread of the
    activation, so it can await what it likes without holding up any other node.
    """

    kind = "function"

    def __init__(self, id: str, function: Callable, *, inputs: Iterable[Port] = (), outputs: Iterable[Port] = (),
                 firing: Firing | None = None, context: bool = False):
        if not callable(function):
            raise NodeError(f"{id}: not callable: {function!r}")
        super().__init__(id, inputs=inputs, outputs=outputs, firing=firing)
        self.function, self.context = function, context
        self._sends = [name for name, port in self.outputs.items() if not port.call]

    def config(self) -> Mapping:
        return {"function": named(self.function), "context": self.context}

    def run(self, ctx, inputs: Mapping[str, Any]):
        result = self.function(ctx, **inputs) if self.context else self.function(**inputs)
        if inspect.iscoroutine(result):
            result = asyncio.run(result)
        if result is None:
            return None
        if len(self._sends) == 1:
            return {self._sends[0]: result}
        if not isinstance(result, Mapping):
            raise NodeError(f"{self.id} has {len(self._sends)} output ports: its function must return a mapping of port to payload")
        return result
