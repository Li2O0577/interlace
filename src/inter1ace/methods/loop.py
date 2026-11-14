"""A loop: a chain whose end can send the work back to its beginning, a limited number of times."""
from __future__ import annotations

from typing import Any, Callable

from ..base import Graph, GraphBuilder, GraphError, Method, Node, Protocols
from ..nodes import Until
from .chain import sole_input, sole_output


class Loop(Method):
    """``Loop(a, b, until=good_enough, max_rounds=5)``: a → b, and back to a until ``good_enough(b's output)``.

    The body runs at most ``max_rounds`` times. Two ways to decide whether it goes round again:

    * give ``until``: a function of the body's output. When the rounds run out the last output leaves anyway;
      with ``strict=True`` the run fails instead.
    * leave ``until`` out: the last node decides. It must have an output port called ``again`` (what it sends
      there goes back to the first node) and one other output (the result). When the rounds run out, what it
      sends back is dropped and the run's ``limited`` names the back edge.

    The graph's input ``in`` is the first node's input, its output ``out`` is the result.
    """

    name = "loop"

    def __init__(self, *nodes: Node, max_rounds: int, until: Callable[[Any], bool] | None = None,
                 strict: bool = False, again: str = "again", protocol: str = "value"):
        if not nodes:
            raise GraphError("a loop needs at least one node")
        if type(max_rounds) is not int or max_rounds < 1:
            raise GraphError("a loop needs max_rounds of at least 1")
        if strict and until is None:
            raise GraphError("strict only applies to a loop with until")
        self.nodes, self.max_rounds, self.until, self.strict = nodes, max_rounds, until, strict
        self.again, self.protocol = again, protocol

    def build(self, protocols: Protocols) -> Graph:
        nodes, protocol = self.nodes, self.protocol
        builder = GraphBuilder().add(*nodes)
        first = sole_input(nodes[0])
        if first is None:
            raise GraphError(f"{nodes[0].id} takes no input, so nothing can be sent back to it")
        for upstream, downstream in zip(nodes, nodes[1:]):
            target = sole_input(downstream)
            if target is None:
                raise GraphError(f"{downstream.id} takes no input and cannot follow {upstream.id} in a loop")
            builder.connect((upstream.id, sole_output(upstream)), (downstream.id, target), protocol)
        last = nodes[-1]
        if self.until is not None:
            carried = last.outputs[sole_output(last)].schema     # the gate passes on what the body produces
            gate = Until("until", self.until, limit=self.max_rounds, strict=self.strict, schema=carried)
            builder.add(gate).connect((last.id, sole_output(last)), (gate.id, "x"), protocol)
            back, result = (gate.id, "again"), (gate.id, "done")
        else:
            others = [name for name in last.outputs if name != self.again]
            if self.again not in last.outputs or len(others) != 1:
                raise GraphError(f"{last.id}: without until, the last node of a loop needs an output {self.again!r} "
                                 f"and one other output, and it has {sorted(last.outputs)}")
            back, result = (last.id, self.again), (last.id, others[0])
        if self.max_rounds > 1:
            builder.connect(back, (nodes[0].id, first), protocol, id="again", back=True, max_rounds=self.max_rounds - 1)
        return builder.input("in", (nodes[0].id, first)).output("out", result).compile(protocols)
