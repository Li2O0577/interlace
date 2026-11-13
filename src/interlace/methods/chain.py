"""A chain: each node's output feeds the next node's input."""
from __future__ import annotations

from ..base import Graph, GraphBuilder, GraphError, Method, Node, Protocols


def sole_input(node: Node) -> str | None:
    """The input a chain feeds: the only required one, or the only one. None when the node takes nothing."""
    required = [name for name, port in node.inputs.items() if port.required]
    if len(required) == 1:
        return required[0]
    if not required and len(node.inputs) == 1:
        return next(iter(node.inputs))
    if not node.inputs:
        return None
    raise GraphError(f"{node.id}: a chain needs one input to feed, and it has {sorted(node.inputs)}")


def sole_output(node: Node) -> str:
    """The output a chain passes on: the only one, not counting the ports the node makes calls through."""
    outputs = [name for name, port in node.outputs.items() if not port.call]
    if len(outputs) != 1:
        raise GraphError(f"{node.id}: a chain needs exactly one output, and it has {sorted(outputs)}")
    return outputs[0]


class Chain(Method):
    """``Chain(a, b, c)``: a → b → c. The graph's input ``in`` is a's input (when it has one), its output
    ``out`` is c's output."""

    name = "chain"

    def __init__(self, *nodes: Node, protocol: str = "value"):
        if not nodes:
            raise GraphError("a chain needs at least one node")
        self.nodes, self.protocol = nodes, protocol

    def build(self, protocols: Protocols) -> Graph:
        builder = GraphBuilder().add(*self.nodes)
        for upstream, downstream in zip(self.nodes, self.nodes[1:]):
            target = sole_input(downstream)
            if target is None:
                raise GraphError(f"{downstream.id} takes no input and cannot follow {upstream.id} in a chain")
            builder.connect((upstream.id, sole_output(upstream)), (downstream.id, target), self.protocol)
        first = sole_input(self.nodes[0])
        if first is not None:
            builder.input("in", (self.nodes[0].id, first))
        builder.output("out", (self.nodes[-1].id, sole_output(self.nodes[-1])))
        return builder.compile(protocols)
