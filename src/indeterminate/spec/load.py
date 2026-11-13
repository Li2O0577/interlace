"""A graph as data: write a graph out, and build a graph from what was written.

``dump(graph)`` is the graph's own description (the one its digest is computed from) plus the descriptions of
the graphs nested inside it. ``load(description, catalog)`` builds the graph back; when every function it
names can be found, the result has the same digest.

A description may also be written by hand, and then it can be shorter:

* a node may put its settings beside ``id`` and ``kind`` instead of under ``config``, and name its ports with
  bare strings;
* an edge needs only ``from``, ``to`` and (unless it is ``value``) ``protocol``;
* instead of ``nodes`` and ``edges``, a description may name a ``method`` and give it its parts::

      {"method": "loop", "nodes": [{...}, {...}], "max_rounds": 4}
      {"method": "tree", "root": {...}, "children": [{...}, {"method": "tree", ...}], "max_rounds": 2}
      {"method": "pool", "root": {...}, "member": {"kind": "agent"}, "size": 10, "max_rounds": 3}

* ``defaults`` gives settings to every node of a kind, so that a node says only what is different about it::

      {"defaults": {"agent": {"model": "anthropic:some-model", "max_steps": 20}}, "method": "chain", "nodes": [...]}

* any setting may be taken from a file beside the description instead of being written in it::

      {"id": "lead", "kind": "agent", "system": {"file": "prompts/lead.md"}}

* a design kept in a file of its own is used as a block, as often as needed, each use a node::

      {"id": "review", "kind": "block", "file": "blocks/review.json", "defaults": {"agent": {"model": "..."}}}

* ``times`` makes several of a node at once, named ``<id>-1`` .. ``<id>-n``::

      {"method": "fan", "nodes": [{"id": "review", "kind": "block", "file": "blocks/review.json", "times": 4}]}

What ``dump`` writes has none of these left in it: the defaults, the files' text and the blocks' graphs are
in the nodes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ..base import Graph, GraphBuilder, GraphError, Node, NodeError, PortError, SchemaError
from ..blocks import Block
from ..methods import Chain, Fan, Governed, Loop, Pool, Star, Tree
from ..nodes import GraphNode
from .catalog import Catalog, SpecError

_NODE_KEYS = {"id", "kind", "firing", "inputs", "outputs", "config", "time_limit"}


def dump(graph: Graph) -> dict:
    """The graph as plain data."""
    described = graph.describe()
    nested: dict[str, Any] = {}

    def collect(inner: Graph) -> None:
        for node in inner.nodes:
            if isinstance(node, GraphNode) and node.graph.digest not in nested:
                nested[node.graph.digest] = node.graph.describe()
                collect(node.graph)
    collect(graph)
    return {"digest": graph.digest, **described, **({"graphs": nested} if nested else {})}


def write(path, graph: Graph) -> None:
    Path(path).write_text(json.dumps(dump(graph), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read(path, catalog: Catalog | None = None) -> Graph:
    """Build the graph written in a file. Without a catalog, the description may take text from files in
    its own directory and can reach nothing else."""
    if catalog is None:
        catalog = Catalog(files=Path(path).resolve().parent)
    try:
        description = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SpecError(f"{path}: not JSON: {exc}") from None
    return load(description, catalog)


def load(description: Mapping, catalog: Catalog | None = None) -> Graph:
    """Build the graph a description stands for."""
    catalog = catalog if catalog is not None else Catalog()
    if not isinstance(description, Mapping):
        raise SpecError("a description is a mapping")
    graphs = description.get("graphs", {})
    if description.get("defaults"):
        catalog = catalog.with_defaults(description["defaults"])
    try:
        if "method" in description:
            return _method(description, catalog, graphs).build(catalog.protocols)
        builder = GraphBuilder()
        for node in _several(description.get("nodes", ())):
            builder.add(_node(node, catalog, graphs))
        for edge in description.get("edges", ()):
            builder.connect(edge["from"], edge["to"], edge.get("protocol", "value"), id=edge.get("id"),
                            back=edge.get("back", False), max_rounds=edge.get("max_rounds"))
        for name, end in description.get("inputs", {}).items():
            builder.input(name, end)
        for name, end in description.get("outputs", {}).items():
            builder.output(name, end)
        return builder.compile(catalog.protocols)
    except KeyError as exc:
        raise SpecError(f"the description lacks {exc.args[0]!r}") from None
    except (GraphError, NodeError, PortError, SchemaError) as exc:
        raise SpecError(str(exc)) from None


def _several(parts) -> list:
    """The parts as written, with each that says ``times`` written out that many times."""
    out = []
    for part in parts:
        times = part.get("times") if isinstance(part, Mapping) else None
        if times is None:
            out.append(part)
            continue
        if type(times) is not int or times < 1 or "id" not in part:
            raise SpecError(f"times is a whole number of at least 1, on a part that has an id: {part!r}")
        rest = {key: value for key, value in part.items() if key != "times"}
        out.extend({**rest, "id": f"{part['id']}-{i}"} for i in range(1, times + 1))
    return out


def block(source, catalog: Catalog | None = None, *, name: str | None = None, workers: int = 0) -> Block:
    """A block from a description, or from the file one is written in: every use builds the graph anew."""
    if isinstance(source, Mapping):
        description, catalog = source, catalog if catalog is not None else Catalog()
    else:
        path = Path(source)
        catalog = catalog if catalog is not None else Catalog(files=path.resolve().parent)
        try:
            description = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise SpecError(f"{path}: not JSON: {exc}") from None
        name = name or path.stem
    load(description, catalog)                 # a design that cannot be built is refused here, not at its first use
    return Block(lambda: load(description, catalog), name=name or "block", protocols=catalog.protocols, workers=workers)


def _node(given: Mapping, catalog: Catalog, graphs: Mapping) -> Node:
    if not isinstance(given, Mapping) or "id" not in given or "kind" not in given:
        raise SpecError(f"a node needs an id and a kind: {given!r}")
    config = {**catalog.defaults.get(given["kind"], {}),
              **{key: value for key, value in given.items() if key not in _NODE_KEYS}, **given.get("config", {})}
    time_limit = given.get("time_limit", config.pop("time_limit", None))
    try:
        config = {key: catalog.text(value["file"]) if isinstance(value, Mapping) and value.keys() == {"file"} else value
                  for key, value in config.items()}
        node = catalog.loader(given["kind"])({**{k: given[k] for k in _NODE_KEYS & given.keys()}, "config": config}, catalog, graphs)
        if time_limit is not None:
            if isinstance(time_limit, bool) or not isinstance(time_limit, (int, float)) or not time_limit > 0:
                raise SpecError("time_limit must be a positive number of seconds")
            node.time_limit = time_limit
        return node
    except KeyError as exc:
        raise SpecError(f"{given['id']}: a {given['kind']} node needs {exc.args[0]!r}") from None
    except SpecError as exc:
        raise SpecError(str(exc) if str(exc).startswith(f"{given['id']}") else f"{given['id']}: {exc}") from None


def _part(given: Mapping, catalog: Catalog, graphs: Mapping):
    """A part of a method: a node, or (in a tree) another tree."""
    return _method(given, catalog, graphs) if "method" in given else _node(given, catalog, graphs)


def _method(d: Mapping, catalog: Catalog, graphs: Mapping):
    name = d["method"]
    options = {"protocol": d["protocol"]} if "protocol" in d else {}
    nodes = [_node(node, catalog, graphs) for node in _several(d.get("nodes", ()))]
    if name == "chain":
        return Chain(*nodes, **options)
    if name == "loop":
        return Loop(*nodes, max_rounds=d["max_rounds"], until=catalog.function(d["until"]) if d.get("until") else None,
                    strict=d.get("strict", False), **options)
    if name == "fan":
        return Fan(*nodes, split=_node(d["split"], catalog, graphs) if d.get("split") else None,
                   join=_node(d["join"], catalog, graphs) if d.get("join") else None, **options)
    if name in ("tree", "star", "governed"):
        root = _node(d["root"], catalog, graphs)
        children = [_part(child, catalog, graphs) for child in _several(d.get("children", ()))]
        if name == "governed":
            return Governed(root, *children, operator=_node(d["operator"], catalog, graphs), max_rounds=d["max_rounds"], **options)
        return (Tree if name == "tree" else Star)(root, *children, max_rounds=d["max_rounds"], **options)
    if name == "pool":
        member = d["member"]

        def make(id: str) -> Node:
            if "method" in member:
                return GraphNode(id, _method(member, catalog, graphs), catalog.protocols)
            return _node({**member, "id": id}, catalog, graphs)
        return Pool(_node(d["root"], catalog, graphs), make, size=d["size"], max_rounds=d["max_rounds"],
                    places=d.get("places", "member"), **options)
    raise SpecError(f"unknown method: {name!r} (known: chain, loop, fan, tree, star, governed, pool)")
