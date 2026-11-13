"""Fan out and in: the same input goes to several branches side by side, and their outputs are joined."""
from __future__ import annotations

from ..base import Graph, GraphBuilder, GraphError, Method, Node, Protocols
from ..nodes import Collect, Pass
from .chain import sole_input, sole_output


class Fan(Method):
    """``Fan(a, b, c)``: the input reaches a, b and c; ``out`` is the list of their outputs, in that order.

    ``split`` replaces the node that hands the input to the branches (by default it passes it on unchanged);
    ``join`` replaces the node that takes the branches' outputs (by default it makes a list). A ``join`` needs
    one input port declared ``many``; it runs once, when every branch has delivered.
    """

    name = "fan"

    def __init__(self, *branches: Node, split: Node | None = None, join: Node | None = None, protocol: str = "value"):
        if not branches:
            raise GraphError("a fan needs at least one branch")
        self.branches, self.split, self.join, self.protocol = branches, split, join, protocol

    def build(self, protocols: Protocols) -> Graph:
        split = self.split if self.split is not None else Pass("split")
        join = self.join if self.join is not None else Collect("join")
        many = [name for name, port in join.inputs.items() if port.many]
        if len(many) != 1:
            raise GraphError(f"{join.id}: the join of a fan needs exactly one input port declared many")
        builder = GraphBuilder().add(split, *self.branches, join)
        source = (split.id, sole_output(split))
        for branch in self.branches:
            target = sole_input(branch)
            if target is None:
                raise GraphError(f"{branch.id} takes no input and cannot be a branch of a fan")
            builder.connect(source, (branch.id, target), self.protocol)
            builder.connect((branch.id, sole_output(branch)), (join.id, many[0]), self.protocol)
        first = sole_input(split)
        if first is not None:
            builder.input("in", (split.id, first))
        return builder.output("out", (join.id, sole_output(join))).compile(protocols)
