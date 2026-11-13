"""The catalog: what a graph written as data may refer to, and how each reference is turned into the real thing.

A description names functions, models and kinds of node; it cannot contain them. Loading one therefore runs
code that the description chose. The catalog is where the host says how far that may go: which modules a
function may come from, whether external programs may be started, where models come from, who answers for
the people in the graph. By default nothing outside this package is reachable.
"""
from __future__ import annotations

import importlib
from typing import Any, Callable, Iterable, Mapping

from ..base import Node, Protocols
from ..protocols import DEFAULT


class SpecError(ValueError):
    """A description that cannot be turned into a graph. The message says where."""


class Catalog:
    """
    ``allow``       module names a function may be imported from (``"mypkg"`` covers ``mypkg`` and all below it)
    ``processes``   whether ``process`` nodes may be built: they start programs on this machine
    ``models``      ``models(name)`` returns the model for an agent; without it, agents cannot be built
    ``ask``         ``ask(node_id, request)`` answers for every ``human`` node that names no function of its own
    ``protocols``   the protocols the graph is compiled with
    """

    def __init__(self, *, allow: Iterable[str] = (), processes: bool = False,
                 models: Callable[[str], Any] | None = None, ask: Callable[[str, Any], Any] | None = None,
                 protocols: Protocols = DEFAULT):
        self.allow, self.processes = tuple(allow), bool(processes)
        self.models, self.ask, self.protocols = models, ask, protocols
        self._kinds: dict[str, Callable[[Mapping, "Catalog", Mapping], Node]] = {}
        from .kinds import BUILT_IN
        for kind, loader in BUILT_IN.items():
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

    def model(self, name: str) -> Any:
        if self.models is None:
            raise SpecError(f"this catalog has no models, so the agent's model {name!r} cannot be built")
        return self.models(name)
