"""A whole graph as one node, so that graphs, and the methods that build them, nest."""
from __future__ import annotations

from typing import Any, Mapping

from ..base import Firing, Graph, Method, Node, NodeError, Port, PortError, Protocols
from ..engine import Result


class GraphNode(Node):
    """The inner graph's inputs and outputs become this node's ports, with the same names and schemas.

    Each activation is one complete run of the inner graph, with its own round counts and round limits. What
    reached the inner outputs is sent on when that run has ended, output by output. A failure inside fails
    this node; stopping the outer run stops the inner one. ``last`` holds the result of the latest inner run.

    The inner run belongs to the outer one: it is journaled beside the outer journal, so a restart goes on
    inside it instead of starting it over; the outer observer is told what happens in it; and the time limit
    applies to each node inside, not to the graph as a whole.
    """

    kind = "graph"
    time_limit = float("inf")      # the nodes inside are timed one by one

    def __init__(self, id: str, graph: Graph | Method, protocols: Protocols, *, workers: int = 0,
                 firing: Firing | None = None):
        if isinstance(graph, Method):
            graph = graph.build(protocols)
        if not isinstance(graph, Graph):
            raise NodeError(f"{id}: not a graph or a method: {graph!r}")
        try:
            inputs = [Port(name, (port := graph.node(end.node).inputs[end.port]).schema, port.required)
                      for name, end in graph.inputs.items()]
            outputs = [Port(name, graph.node(end.node).outputs[end.port].schema) for name, end in graph.outputs.items()]
        except PortError as exc:
            raise NodeError(f"{id}: a graph's input and output names become port names: {exc}") from None
        super().__init__(id, inputs=inputs, outputs=outputs, firing=firing)
        self.graph, self.protocols, self.workers = graph, protocols, workers
        self.last: Result | None = None

    def config(self) -> Mapping:
        return {"graph": self.graph.digest}

    def run(self, ctx, inputs: Mapping[str, Any]):
        # run again after a rejected output: start the inner run over; after a restart: take it up
        result = self.last = ctx.engine(self.graph, self.protocols, workers=self.workers).go(inputs, fresh=bool(ctx.problems))
        for name, values in result.outputs.items():
            for value in values:
                ctx.emit(name, value)
