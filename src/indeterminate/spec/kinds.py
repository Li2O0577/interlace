"""How each kind of node that comes with the package is built from its description."""
from __future__ import annotations

from typing import Mapping

from ..base import ANY, Is, ListOf, Port, Record, Schema
from ..nodes import (AgentNode, Branch, Collect, FunctionNode, GraphNode, HumanNode, Pass, ProcessNode, Roster, SinkNode,
                     SourceNode, Tool, Until)

_TYPES = {t.__name__: t for t in (str, int, float, bool, list, dict, bytes, type(None))}


def schema_of(described) -> Schema:
    """The inverse of ``Schema.describe``."""
    from .catalog import SpecError
    if described is None or described == "any":
        return ANY
    if isinstance(described, str):
        try:
            return Is(*(_TYPES[name] for name in described.split("|")))
        except KeyError as exc:
            raise SpecError(f"unknown type in a schema: {exc.args[0]!r}") from None
    if isinstance(described, Mapping) and "list" in described:
        return ListOf(schema_of(described["list"]))
    if isinstance(described, Mapping) and "record" in described:
        return Record({name: schema_of(field) for name, field in described["record"].items()},
                      described.get("optional", ()), described.get("open", False))
    raise SpecError(f"not a schema: {described!r}")


def port_of(described) -> Port:
    """A port from its description, or from a bare name."""
    if isinstance(described, str):
        return Port(described)
    return Port(described["name"], schema_of(described.get("schema")), described.get("required", True),
                described.get("many", False), described.get("call", False))


def ports(description: Mapping, side: str) -> list[Port] | None:
    given = description.get(side)
    return None if given is None else [port_of(port) for port in given]


def _first_schema(description: Mapping, side: str) -> Schema:
    given = ports(description, side)
    return given[0].schema if given else ANY


def _optional(catalog, name):
    return catalog.function(name) if name else None


def function(d, catalog, graphs):
    return FunctionNode(d["id"], catalog.function(d["config"]["function"]), inputs=ports(d, "inputs") or (),
                        outputs=ports(d, "outputs") or (), firing=d.get("firing"), context=d["config"].get("context", False))


def agent(d, catalog, graphs):
    c = d["config"]
    outputs = ports(d, "outputs")
    memory = c.get("memory", True)
    return AgentNode(d["id"], catalog.model(c.get("model", "")), system=c.get("system", ""),
                     tools=[Tool(t["name"], t.get("description", ""), t.get("parameters") or {"type": "object", "properties": {}})
                            for t in c.get("tools", ())],
                     inputs=ports(d, "inputs"), outputs=[p for p in outputs if not p.call] if outputs is not None else None,
                     firing=d.get("firing"), memory=catalog.function(memory) if isinstance(memory, str) else memory, json=c.get("json", False),
                     max_steps=c.get("max_steps", 50), max_tokens=c.get("max_tokens", 4096))


def process(d, catalog, graphs):
    from .catalog import SpecError
    if not catalog.processes:
        raise SpecError(f"{d['id']} starts a program, and this catalog does not allow processes")
    c = d["config"]
    inputs = d.get("inputs")
    return ProcessNode(d["id"], c["command"], format=c.get("format", "text"), lines=c.get("lines", False),
                       timeout=c.get("timeout"), takes_input=inputs is None or bool(inputs), cwd=c.get("cwd"))


def source(d, catalog, graphs):
    c = d["config"]
    return SourceNode(d["id"], catalog.function(c["read"]), schema=_first_schema(d, "outputs"),
                      connect=_optional(catalog, c.get("connect")), disconnect=_optional(catalog, c.get("disconnect")))


def sink(d, catalog, graphs):
    c = d["config"]
    return SinkNode(d["id"], catalog.function(c["write"]), schema=_first_schema(d, "inputs"), answers=c.get("answers", False),
                    connect=_optional(catalog, c.get("connect")), disconnect=_optional(catalog, c.get("disconnect")))


def human(d, catalog, graphs):
    name = d["config"].get("ask")
    node = HumanNode(d["id"], catalog.function(name) if name else None,
                     request=_first_schema(d, "inputs"), answer=_first_schema(d, "outputs"))
    if not name:
        node.host = catalog.ask
    return node


def graph(d, catalog, graphs):
    from .catalog import SpecError
    from .load import load
    inner = d["config"].get("graph")
    if isinstance(inner, str):
        if inner not in graphs:
            raise SpecError(f"{d['id']}: the description has no graph {inner[:12]} under \"graphs\"")
        inner = graphs[inner]
    return GraphNode(d["id"], load({**inner, "graphs": {**graphs, **inner.get("graphs", {})}}, catalog), catalog.protocols,
                     firing=d.get("firing"))


BUILT_IN = {
    "function": function, "agent": agent, "process": process, "source": source, "sink": sink, "human": human,
    "graph": graph,
    "roster": lambda d, catalog, graphs: Roster(d["id"], d["config"]["size"]),
    "pass": lambda d, catalog, graphs: Pass(d["id"], schema=_first_schema(d, "inputs")),
    "collect": lambda d, catalog, graphs: Collect(d["id"], schema=_first_schema(d, "inputs")),
    "branch": lambda d, catalog, graphs: Branch(d["id"], catalog.function(d["config"]["condition"]), schema=_first_schema(d, "inputs")),
    "until": lambda d, catalog, graphs: Until(d["id"], catalog.function(d["config"]["condition"]), limit=d["config"].get("limit"),
                                              strict=d["config"].get("strict", False), schema=_first_schema(d, "inputs")),
}
