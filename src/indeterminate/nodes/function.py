"""A node that is one Python function."""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from ..base import Firing, Node, NodeError, Port


class FunctionNode(Node):
    """Calls ``function`` with the arrived inputs as keyword arguments, by port name.

    With one output port, what the function returns is that port's payload (``None`` sends nothing). With
    several, it returns a mapping of port name to payload; ports it leaves out send nothing. With
    ``context=True`` the function also receives the engine's context as its first argument, to emit several
    times or to see that the run is stopping.
    """

    kind = "function"

    def __init__(self, id: str, function: Callable, *, inputs: Iterable[Port] = (), outputs: Iterable[Port] = (),
                 firing: Firing | None = None, context: bool = False):
        if not callable(function):
            raise NodeError(f"{id}: not callable: {function!r}")
        super().__init__(id, inputs=inputs, outputs=outputs, firing=firing)
        self.function, self.context = function, context

    def config(self) -> Mapping:
        function = self.function
        name = f"{getattr(function, '__module__', '')}.{getattr(function, '__qualname__', type(function).__qualname__)}"
        return {"function": name, "context": self.context}

    def run(self, ctx, inputs: Mapping[str, Any]):
        result = self.function(ctx, **inputs) if self.context else self.function(**inputs)
        if result is None:
            return None
        if len(self.outputs) == 1:
            return {next(iter(self.outputs)): result}
        if not isinstance(result, Mapping):
            raise NodeError(f"{self.id} has {len(self.outputs)} output ports: its function must return a mapping of port to payload")
        return result
