"""Fan out and in: the same input goes to several branches side by side, and their outputs are joined."""
from __future__ import annotations

from ..base import Graph, GraphBuilder, GraphError, Method, Node, Protocols
from ..nodes import Collect, Pass
from .chain import sole_input, sole_output


def _named(ports, branches) -> list[str] | None:
    """The port that belongs to each branch, when there is one for every branch: it has the branch's id as its
    name, with ``_`` where the id has ``-``."""
    names = [branch.id.replace("-", "_") for branch in branches]
    return names if all(name in ports for name in names) else None


class Fan(Method):
    """``Fan(a, b, c)``: the input reaches a, b and c; ``out`` is the list of their outputs, in that order.

    ``split`` replaces the node that hands the input to the branches (by default it passes it on unchanged);
    ``join`` replaces the node that takes the branches' outputs (by default it makes a list).

    A split with one output gives the same to every branch. A split with an output named after each branch
    gives each branch its own: that is a ``Desk`` (or an agent) sharing out the work. In the same way a join
    has either one input declared ``many``, and gets a list, or an input named after each branch, and knows
    what came from whom. A join runs once, when every branch has delivered.

    The graph's input is ``in`` and its output ``out``. A split that takes several things, or a join that
    gives several, shows each of them under its own name instead, so a fan can take from several places and
    give to several.
    """

    name = "fan"

    def __init__(self, *branches: Node, split: Node | None = None, join: Node | None = None, protocol: str = "value"):
        if not branches:
            raise GraphError("a fan needs at least one branch")
        self.branches, self.split, self.join, self.protocol = branches, split, join, protocol

    def build(self, protocols: Protocols) -> Graph:
        split = self.split if self.split is not None else Pass("split")
        join = self.join if self.join is not None else Collect("join")
        gives = [name for name, port in split.outputs.items() if not port.call]
        sources = _named(gives, self.branches) or [sole_output(split)] * len(self.branches)
        many = [name for name, port in join.inputs.items() if port.many]
        targets = _named(join.inputs, self.branches) or many * len(self.branches)
        if len(targets) != len(self.branches):
            raise GraphError(f"{join.id}: the join of a fan needs one input port declared many, or an input named "
                             f"after each branch")
        builder = GraphBuilder().add(split, *self.branches, join)
        for branch, source, into in zip(self.branches, sources, targets):
            target = sole_input(branch)
            if target is None:
                raise GraphError(f"{branch.id} takes no input and cannot be a branch of a fan")
            builder.connect((split.id, source), (branch.id, target), self.protocol)
            builder.connect((branch.id, sole_output(branch)), (join.id, into), self.protocol)
        required = [name for name, port in split.inputs.items() if port.required]
        if len(split.inputs) > 1 and len(required) != 1:
            for name in split.inputs:
                builder.input(name, (split.id, name))
        elif split.inputs:
            builder.input("in", (split.id, sole_input(split)))
        sends = [name for name, port in join.outputs.items() if not port.call]
        if len(sends) > 1:
            for name in sends:
                builder.output(name, (join.id, name))
        else:
            builder.output("out", (join.id, sole_output(join)))
        return builder.compile(protocols)
