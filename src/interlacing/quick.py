"""``run``: build and run in one step, for the times when there is nothing to set but the graph."""
from __future__ import annotations

from typing import Any, Mapping

from .base import Graph, GraphBuilder, Method, Protocols
from .engine import Engine, Result
from .nodes import as_node


def graph_of(thing: Any, protocols: Protocols | None = None) -> Graph:
    """``thing`` as a compiled graph: a graph as it is, a method built, and a single node (or a plain
    function) as a graph of one node whose inputs and outputs are the graph's, under the same names."""
    if isinstance(thing, Graph):
        return thing
    if isinstance(thing, Method):
        return thing.build(protocols)
    node = as_node(thing)
    builder = GraphBuilder().add(node)
    for name, port in node.inputs.items():
        builder.input(name, (node.id, name))
    for name, port in node.outputs.items():
        if not port.call:
            builder.output(name, (node.id, name))
    return builder.compile(protocols)


def run(thing: Any, inputs: Mapping[str, Any] | None = None, *, protocols: Protocols | None = None,
        **options: Any) -> Result:
    """Build ``thing`` (a graph, a method, a node or a plain function) and run it with ``inputs``.
    ``options`` go to the ``Engine``: ``workers``, ``journal``, ``time_limit``, ``budget``, ``observe``...

        run(Chain(double, show), {"in": 21}).last("out")
        run(double, {"x": 21}).last("out")
    """
    graph = graph_of(thing, protocols)
    return Engine(graph, protocols, **options).run(inputs)
