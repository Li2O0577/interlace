"""The abstractions everything else is built on. No behaviour of any particular node, edge or method lives here.

    Node, Port, Schema      what takes information in and gives it out, and what its ports carry
    Edge, Protocol          a flow between two ports, and the rules of that flow
    Envelope, Ref           the form in which information travels
    Graph, GraphBuilder     nodes joined by edges, checked once, then read-only
    Change                  the one way a graph changes while it runs
    Method                  a reusable way of joining nodes

This package imports nothing else from ``interlacing``.
"""
from .canonical import canonical, digest
from .change import Change
from .edge import Edge, EdgeError, Endpoint
from .envelope import Envelope, Ref
from .graph import Graph, GraphBuilder, GraphError
from .method import Method
from .node import Context, Firing, Invalid, Node, NodeError, waited
from .port import Port, PortError
from .protocol import Delivery, Protocol, ProtocolError, Protocols, resolve, set_standard
from .schema import ANY, Is, ListOf, Record, Schema, SchemaError, schema

__all__ = [
    "ANY", "Change", "Context", "Delivery", "Edge", "EdgeError", "Endpoint", "Envelope", "Firing", "Graph", "GraphBuilder",
    "GraphError", "Invalid", "Is", "ListOf", "Method", "Node", "NodeError", "Port", "PortError", "Protocol", "ProtocolError",
    "Protocols", "Record", "Ref", "Schema", "SchemaError", "canonical", "digest", "resolve", "schema", "set_standard", "waited",
]
