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

from ..base import Node, Protocols
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
    ``defaults``    settings for every node of a kind, ``{"agent": {"model": "...", "max_steps": 20}}``; a
                    description's own ``defaults`` come on top of these, and a node's own settings on top of both
    """

    def __init__(self, *, allow: Iterable[str] = (), processes: bool = False,
                 models: Callable[[str], Any] | None = None, ask: Callable[[str, int, Any], Any] | None = None,
                 protocols: Protocols = DEFAULT, files=None, defaults: Mapping[str, Mapping] | None = None,
                 workspace=None):
        self.allow, self.processes = tuple(allow), bool(processes)
        self.files = Path(files).resolve() if files is not None else None
        self.defaults = {kind: dict(settings) for kind, settings in (defaults or {}).items()}
        self.models, self.ask = models, ask
        self.workspace = Path(workspace).resolve() if workspace is not None else None
        self.protocols = with_files(protocols, self.workspace) if self.workspace is not None else protocols
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
