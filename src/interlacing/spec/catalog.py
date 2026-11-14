"""The catalog: what a graph written as data may refer to, and how each reference is turned into the real thing.

A description names functions, models and kinds of node; it cannot contain them. Loading one therefore runs
code that the description chose. The catalog is where the host says how far that may go: which modules a
function may come from, whether external programs may be started, where models come from, who answers for
the people in the graph. By default nothing outside this package is reachable.
"""
from __future__ import annotations

import copy
import importlib
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..base import Node, NodeError, Protocols
from ..nodes import Area, Tool, ToolPool, Workspace
from ..protocols import DEFAULT, with_files


class SpecError(ValueError):
    """A description that cannot be turned into a graph. The message says where."""


class Catalog:
    """
    ``allow``       module names a function may be imported from (``"mypkg"`` covers ``mypkg`` and all below it)
    ``processes``   whether ``process`` nodes may be built: they start programs on this machine
    ``models``      ``models(name)`` returns the model for an agent; without it, agents cannot be built
    ``ask``         ``ask(node_id, round, request)`` answers for every ``human`` node that names no function of its own
    ``protocols``   the protocols the graph is compiled with
    ``files``       the directory a description may take text from with ``{"file": "prompts/lead.md"}``; files
                    outside it are refused, and without it no file is read
    ``workspace``   the directory the graph's nodes work in: the ``file`` protocol carries paths relative to it,
                    agents are shown the pictures in it, programs are started in it
    ``tools``       a tool pool: the directory an agent's tools may be taken from by path (``"tools": ["plot.py"]``);
                    without it no tool file is read or run
    ``max_workers`` the most threads a block written in a description may ask for (``None``: as many as it asks)
    ``defaults``    settings for every node of a kind, ``{"agent": {"model": "...", "max_steps": 20}}``; a
                    description's own ``defaults`` come on top of these, and a node's own settings on top of both
    """

    def __init__(self, *, allow: Iterable[str] = (), processes: bool = False,
                 models: Callable[[str], Any] | None = None, ask: Callable[[str, int, Any], Any] | None = None,
                 protocols: Protocols = DEFAULT, files=None, defaults: Mapping[str, Mapping] | None = None,
                 workspace=None, max_workers: int | None = None, tools=None):
        self.allow, self.processes, self.max_workers = tuple(allow), bool(processes), max_workers
        self.files = Path(files).resolve() if files is not None else None
        self.defaults = {kind: dict(settings) for kind, settings in (defaults or {}).items()}
        self.models, self.ask = models, ask
        self.workspace = Path(workspace).resolve() if workspace is not None else None
        self.protocols = with_files(protocols, self.workspace) if self.workspace is not None else protocols
        self.space = Workspace(self.workspace) if self.workspace is not None else None
        self.pool = ToolPool(tools) if tools is not None else None
        self.layer: tuple = ()      # the blocks the nodes being built are inside, outermost first
        self._kinds: dict[str, Callable[[Mapping, "Catalog", Mapping], Node]] = {}
        from .kinds import BUILT_IN
        from .spawn import loader as spawn
        for kind, loader in {**BUILT_IN, "spawn": spawn}.items():
            self.register(kind, loader)

    def register(self, kind: str, loader: Callable[[Mapping, "Catalog", Mapping], Node]) -> None:
        """``loader(description, catalog, graphs)`` builds a node of this kind from its description."""
        self._kinds[kind] = loader

    def loader(self, kind: str):
        try:
            return self._kinds[kind]
        except KeyError:
            raise SpecError(f"unknown kind of node: {kind!r} (known: {', '.join(sorted(self._kinds))})") from None

    def function(self, name: str) -> Callable:
        """The function a dotted name stands for, if it lives in an allowed module."""
        if not isinstance(name, str) or not name or "<" in name:
            raise SpecError(f"{name!r} cannot be found by name: it must be a function defined at the top of a module")
        parts = name.split(".")
        if not any(name == prefix or name.startswith(prefix + ".") for prefix in self.allow):
            raise SpecError(f"{name} is not in a module this catalog allows ({', '.join(self.allow) or 'none'})")
        for cut in range(len(parts) - 1, 0, -1):
            try:
                found: Any = importlib.import_module(".".join(parts[:cut]))
            except ImportError:
                continue
            try:
                for part in parts[cut:]:
                    found = getattr(found, part)
            except AttributeError:
                break
            if not callable(found):
                raise SpecError(f"{name} is not callable")
            return found
        raise SpecError(f"{name} cannot be found")

    def with_defaults(self, defaults: Mapping[str, Mapping]) -> "Catalog":
        """This catalog with more defaults on top of its own."""
        if not isinstance(defaults, Mapping) or not all(isinstance(v, Mapping) for v in defaults.values()):
            raise SpecError("defaults is a mapping of kind of node to settings")
        both = copy.copy(self)
        both.defaults = {kind: {**self.defaults.get(kind, {}), **defaults.get(kind, {})}
                         for kind in self.defaults.keys() | defaults.keys()}
        return both

    def inside(self, block: str) -> "Catalog":
        """This catalog for the nodes of a graph that is a node, ``block``, of the one being built: their
        directories in the workspace are one layer further in."""
        inner = copy.copy(self)
        inner.layer = (*self.layer, block)
        return inner

    def area(self, node: str, config: Mapping) -> Area | None:
        """The part of the workspace that is the node's: where the catalog's layer puts it, with the bounds
        its description gives (``reads``, ``writes``) or else the usual ones. None without a workspace."""
        reads, writes = config.get("reads"), config.get("writes")
        if self.space is None:
            if reads is not None or writes is not None:
                raise SpecError("reads and writes are bounds in a workspace, and this catalog has none")
            return None
        for names in (reads, writes):
            if names is not None and (not isinstance(names, list) or not all(isinstance(name, str) for name in names)):
                raise SpecError("reads and writes are lists of paths relative to the top of the workspace")
        try:
            return self.space.area(node, layer=self.layer, reads=reads, writes=writes)
        except NodeError as exc:
            raise SpecError(str(exc)) from None

    def tools_of(self, described, area: Area | None) -> list[Tool]:
        """The tools an agent's description lists. A mapping with a name is a tool the graph answers through
        a call edge; a path (``"plot.py"``, ``"charts"``, ``{"file": "plot.py"}``) is taken from the tool
        pool, a directory standing for every tool in it."""
        tools: list[Tool] = []
        for tool in described or ():
            path = tool if isinstance(tool, str) else tool.get("file") if isinstance(tool, Mapping) else None
            if path is not None:
                if self.pool is None:
                    raise SpecError(f"the tool {path!r} is a file, and this catalog has no tool pool")
                cwd, top = (area.home, area.root) if area is not None else (None, None)
                try:
                    if isinstance(tool, Mapping) and "name" in tool:
                        tools.append(self.pool.tool(path, cwd=cwd, top=top, spec=tool))
                    else:
                        tools.extend(self.pool.tools(path, cwd=cwd, top=top))
                except NodeError as exc:
                    raise SpecError(str(exc)) from None
            elif isinstance(tool, Mapping) and tool.get("own"):
                raise SpecError(f"the tool {tool.get('name')!r} was the node's own function, which a description cannot hold")
            elif isinstance(tool, Mapping) and "name" in tool:
                tools.append(Tool(tool["name"], tool.get("description", ""), tool.get("parameters") or {"type": "object", "properties": {}}))
            else:
                raise SpecError(f"not a tool: {tool!r}")
        return tools

    def text(self, name: str) -> str:
        """The text of a file a description refers to."""
        if self.files is None:
            raise SpecError(f"the description takes text from the file {name!r}, and this catalog allows no files")
        path = (self.files / name).resolve()
        if path != self.files and self.files not in path.parents:
            raise SpecError(f"the file {name!r} is outside {self.files}")
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SpecError(f"the file {name!r} cannot be read: {exc.strerror or exc}") from None

    def model(self, name: str) -> Any:
        if self.models is None:
            raise SpecError(f"this catalog has no models, so the agent's model {name!r} cannot be built")
        return self.models(name)
