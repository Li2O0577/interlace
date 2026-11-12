"""Small nodes that only direct the flow: pass on, collect, choose a way, decide whether a loop goes round again."""
from __future__ import annotations

from typing import Any, Callable, Mapping

from ..base import ANY, Node, NodeError, Port
from .function import named


class Pass(Node):
    """Sends on what it receives, unchanged: ``x`` to ``y``. One graph input can reach several nodes through it."""

    kind = "pass"

    def __init__(self, id: str, *, schema=ANY):
        super().__init__(id, inputs=[Port("x", schema)], outputs=[Port("y", schema)])

    def run(self, ctx, inputs: Mapping[str, Any]):
        return {"y": inputs["x"]}


class Collect(Node):
    """Waits for every edge that feeds ``parts`` and sends them on as one list, in the order of the edges."""

    kind = "collect"

    def __init__(self, id: str, *, schema=ANY):
        super().__init__(id, inputs=[Port("parts", schema, many=True)], outputs=[Port("y")])

    def run(self, ctx, inputs: Mapping[str, Any]):
        return {"y": list(inputs["parts"])}


def _condition(id: str, condition: Callable) -> Callable:
    if not callable(condition):
        raise NodeError(f"{id}: not callable: {condition!r}")
    return condition


class Branch(Node):
    """Sends ``x`` out of ``yes`` when ``condition(x)`` holds, out of ``no`` otherwise."""

    kind = "branch"

    def __init__(self, id: str, condition: Callable[[Any], bool], *, schema=ANY):
        super().__init__(id, inputs=[Port("x", schema)], outputs=[Port("yes", schema), Port("no", schema)])
        self.condition = _condition(id, condition)

    def config(self) -> Mapping:
        return {"condition": named(self.condition)}

    def run(self, ctx, inputs: Mapping[str, Any]):
        value = inputs["x"]
        return {"yes" if self.condition(value) else "no": value}


class Until(Node):
    """The end of a loop's body. Sends ``x`` out of ``done`` once ``condition(x)`` holds, out of ``again``
    until then. After ``limit`` activations it stops asking: the value leaves through ``done`` anyway, or, when
    ``strict``, the node fails."""

    kind = "until"

    def __init__(self, id: str, condition: Callable[[Any], bool], *, limit: int | None = None, strict: bool = False,
                 schema=ANY):
        if limit is not None and (type(limit) is not int or limit < 1):
            raise NodeError(f"{id}: limit must be at least 1")
        super().__init__(id, inputs=[Port("x", schema)], outputs=[Port("done", schema), Port("again", schema)])
        self.condition, self.limit, self.strict = _condition(id, condition), limit, bool(strict)

    def config(self) -> Mapping:
        return {"condition": named(self.condition), "limit": self.limit, "strict": self.strict}

    def run(self, ctx, inputs: Mapping[str, Any]):
        value = inputs["x"]
        if self.condition(value):
            return {"done": value}
        if self.limit is not None and ctx.round >= self.limit:
            if self.strict:
                raise NodeError(f"the condition did not hold after {self.limit} rounds")
            return {"done": value}
        return {"again": value}
