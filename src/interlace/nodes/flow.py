"""Small nodes that only direct the flow: pass on, collect, choose a way, decide whether a loop goes round again."""
from __future__ import annotations

from typing import Any, Callable, Mapping

from ..base import ANY, Firing, Node, NodeError, Port
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


class Roster(Node):
    """Keeps a fixed number of places and gives them out by name, as they are asked for.

    What arrives on ``task`` is passed to a place: ``{"to": name, "task": ...}`` goes to the place that
    name holds, and a name not seen before takes the next free place; anything else takes a new place under
    a name of its own. Whatever else the mapping carries (a role, say) goes along with the task. What comes
    back from place *i* (on ``r<i>``) leaves through ``reply`` as ``{"from": name, "reply": ...}``. When
    every place is taken, a new name gets ``{"from": name, "error": ...}`` instead.

    A place that was never given out never receives anything, so the node standing there never runs.
    """

    kind = "roster"

    def __init__(self, id: str, size: int):
        if type(size) is not int or size < 1:
            raise NodeError(f"{id}: size must be at least 1")
        super().__init__(id, inputs=[Port("task"), *(Port(f"r{i}", required=False) for i in range(1, size + 1))],
                         outputs=[Port("reply"), *(Port(f"m{i}") for i in range(1, size + 1))], firing=Firing.ANY)
        self.size = size
        self.names: list[str] = []          # names[i - 1] holds place i

    def config(self) -> Mapping:
        return {"size": self.size}

    def snapshot(self) -> Mapping:
        return {"names": self.names}

    def restore(self, state: Mapping) -> None:
        self.names = list(state.get("names", []))

    def run(self, ctx, inputs: Mapping[str, Any]):
        (port, value), = inputs.items()
        if port != "task":
            return {"reply": {"from": self.names[int(port[1:]) - 1], "reply": value}}
        addressed = isinstance(value, Mapping) and "task" in value
        name = str(value["to"]) if addressed and value.get("to") else f"member-{len(self.names) + 1}"
        if name not in self.names:
            if len(self.names) >= self.size:
                return {"reply": {"from": name, "error": f"all {self.size} places are taken ({', '.join(self.names)}); "
                                                         f"give the work to one of them"}}
            self.names.append(name)
        if addressed:
            rest = {key: item for key, item in value.items() if key != "to"}
            value = rest["task"] if len(rest) == 1 else rest
        return {f"m{self.names.index(name) + 1}": value}
