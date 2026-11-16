"""A change to a running graph: the one way a graph changes while it runs.

A node asks for one with ``ctx.change(Change().add(node).connect("a.y", node.id + ".x"))``. The engine applies
it to the graph as it stands, checks the result by every rule a graph is built by, and either refuses it (the
graph stays as it was and the node is told why) or makes it real at once. With a journal the change is kept
with the activation that asked for it, as plain data, and is made again when the run is taken up.
"""
from __future__ import annotations

from typing import Callable, Iterable

from .edge import Edge, EdgeError, Endpoint
from .graph import GraphBuilder, GraphError
from .node import Node


class Change:
    """What to do to the graph, in order. Each method returns the change, so calls can be chained."""

    __slots__ = ("ops",)

    def __init__(self):
        self.ops: list[tuple] = []

    def add(self, *nodes: Node) -> "Change":
        for node in nodes:
            if not isinstance(node, Node):
                raise GraphError(f"not a node: {node!r}")
            self.ops.append(("add", node))
        return self

    def remove(self, *nodes: str) -> "Change":
        """Take nodes out, with every edge and graph output that touches them."""
        self.ops.extend(("remove", str(node)) for node in nodes)
        return self

    def connect(self, source, target, protocol: str = "value", *, id: str | None = None, back: bool = False,
                max_rounds: int | None = None) -> "Change":
        try:
            source, target = Endpoint.of(source), Endpoint.of(target)
            self.ops.append(("connect", Edge(id or f"{source}>{target}", source, target, protocol, back, max_rounds)))
        except EdgeError as exc:
            raise GraphError(str(exc)) from None
        return self

    def disconnect(self, *edges: str) -> "Change":
        self.ops.extend(("disconnect", str(edge)) for edge in edges)
        return self

    def output(self, name: str, source) -> "Change":
        """Name an output port as a new output of the graph itself."""
        try:
            self.ops.append(("output", str(name), Endpoint.of(source)))
        except EdgeError as exc:
            raise GraphError(str(exc)) from None
        return self

    def __bool__(self) -> bool:
        return bool(self.ops)

    @property
    def added(self) -> list[Node]:
        return [op[1] for op in self.ops if op[0] == "add"]

    @property
    def removed(self) -> list[str]:
        return [op[1] for op in self.ops if op[0] == "remove"]

    def apply(self, builder: GraphBuilder) -> GraphBuilder:
        """Make the change on ``builder``; ``GraphError`` says what cannot be done."""
        for op in self.ops:
            kind = op[0]
            if kind == "add":
                builder.add(op[1])
            elif kind == "remove":
                builder.remove(op[1])
            elif kind == "connect":
                edge = op[1]
                builder.connect(edge.source, edge.target, edge.protocol, id=edge.id, back=edge.back,
                                max_rounds=edge.max_rounds)
            elif kind == "disconnect":
                builder.disconnect(op[1])
            else:
                builder.output(op[1], op[2])
        return builder

    def describe(self) -> list:
        """The change as plain data: an added node is what its ``describe`` gives."""
        out: list = []
        for op in self.ops:
            kind = op[0]
            if kind == "add":
                out.append(["add", op[1].describe()])
            elif kind == "connect":
                out.append(["connect", op[1].describe()])
            elif kind == "output":
                out.append(["output", op[1], str(op[2])])
            else:
                out.append([kind, op[1]])
        return out

    @classmethod
    def of(cls, described: Iterable, rebuild: Callable[[dict], Node] | None) -> "Change":
        """The change ``describe`` gave, with each added node made again by ``rebuild(description)``."""
        change = cls()
        for op in described:
            kind = op[0]
            if kind == "add":
                if rebuild is None:
                    raise GraphError(f"node {op[1].get('id')!r} was added while the run went on, and nothing was "
                                     f"given that can make it again (rebuild)")
                change.add(rebuild(op[1]))
            elif kind == "connect":
                e = op[1]
                change.connect(e["from"], e["to"], e["protocol"], id=e["id"], back=e.get("back", False),
                               max_rounds=e.get("max_rounds"))
            elif kind == "output":
                change.output(op[1], op[2])
            elif kind == "remove":
                change.remove(op[1])
            elif kind == "disconnect":
                change.disconnect(op[1])
            else:
                raise GraphError(f"unknown change: {kind!r}")
        return change


