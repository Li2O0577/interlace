"""Small nodes that only direct the flow: pass on, collect, take in and hand out, choose a way, decide whether a
loop goes round again."""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

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


class Desk(Node):
    """Takes in what arrives from several places, sorts it, and hands it out to several places.

    ``takes`` names the inputs, one for each place something comes from; ``gives`` names the outputs, one for
    each place something goes to. Either may hold ports instead of names, for a schema, a check, or an input
    that several edges feed (``many``).

    ``sort(received)`` is given a mapping of input name to what arrived there and returns a mapping of output
    name to what leaves there; an output it leaves out sends nothing. Without ``sort``, every output gets
    what arrived: the value itself when the desk has one input, the mapping when it has several.

    With ``wait`` (the default) the desk runs once everything it takes has arrived. Without it, the desk runs
    each time anything arrives, and ``received`` holds only that.

    A desk at the way into a block shares the block's input among its members; a desk at the way out makes
    one answer of what the members gave. ``Fan`` joins both to the members by name. Where the sorting needs
    judgement, an ``AgentNode`` with the same inputs and outputs stands in the same place.
    """

    kind = "desk"

    def __init__(self, id: str, *, takes: Iterable[str | Port] = ("x",), gives: Iterable[str | Port] = ("y",),
                 sort: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None, wait: bool = True):
        inputs = [port if isinstance(port, Port) else Port(port) for port in takes]
        outputs = [port if isinstance(port, Port) else Port(port) for port in gives]
        if not inputs or not outputs:
            raise NodeError(f"{id}: a desk takes at least one thing and gives at least one")
        if any(port.call for port in outputs):
            raise NodeError(f"{id}: a desk hands out, it does not call")
        if sort is not None and not callable(sort):
            raise NodeError(f"{id}: not callable: {sort!r}")
        if not wait:    # each arrival is handled by itself, so none of them can be waited for
            inputs = [Port(p.name, p.schema, False, p.many, check=p.check, invalid=p.invalid) for p in inputs]
        super().__init__(id, inputs=inputs, outputs=outputs, firing=Firing.ALL if wait else Firing.ANY)
        self.sort, self.wait = sort, bool(wait)

    def config(self) -> Mapping:
        return {"sort": named(self.sort) if self.sort is not None else None}

    def run(self, ctx, inputs: Mapping[str, Any]):
        if self.sort is None:
            value = next(iter(inputs.values())) if len(self.inputs) == 1 else dict(inputs)
            return {name: value for name in self.outputs}
        sorted_ = self.sort(dict(inputs))
        if sorted_ is None:
            return None
        if not isinstance(sorted_, Mapping):
            raise NodeError(f"sort must return a mapping of output to payload, not {type(sorted_).__name__}")
        unknown = sorted(set(sorted_) - set(self.outputs))
        if unknown:
            raise NodeError(f"sort gave to {', '.join(map(str, unknown))}, and the desk gives to {', '.join(self.outputs)}")
        return dict(sorted_)


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
