"""The node: anything that takes information in and gives information out.

A node knows its ports and how to run once. It does not know which graph it is in, who is upstream or
downstream, or how its outputs travel: everything it touches outside itself goes through the ``Context`` the
engine hands it. An agent, a function, an external program, a device, a person and a whole sub-graph are all
nodes, with no difference in standing.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Iterable, Mapping

from .port import Port

ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class NodeError(ValueError):
    pass


class Firing(Enum):
    """When a node is activated."""

    ALL = "all"   # once every required input has arrived
    ANY = "any"   # each time any input arrives


class Context(ABC):
    """What the engine gives a node for one activation."""

    node: str     # the node's id
    round: int    # which activation of this node this is, from 1

    @abstractmethod
    def emit(self, port: str, payload: Any) -> None:
        """Send ``payload`` out of an output port now (a node may emit many times during one activation)."""

    @abstractmethod
    def call(self, port: str, payload: Any) -> Any:
        """Send ``payload`` out of an output port whose edge is a call, wait, and return the other node's answer."""

    @abstractmethod
    def stopping(self) -> bool:
        """True once the run is being stopped: a long activation should return soon."""


def _ports(given: Iterable[Port] | Mapping[str, Port] | None, declared: Iterable[Port]) -> Mapping[str, Port]:
    ports = list(given.values() if isinstance(given, Mapping) else given) if given is not None else list(declared)
    out: dict[str, Port] = {}
    for port in ports:
        if not isinstance(port, Port):
            raise NodeError(f"not a port: {port!r}")
        if port.name in out:
            raise NodeError(f"duplicate port: {port.name}")
        out[port.name] = port
    return MappingProxyType(out)


class Node(ABC):
    """Subclass, set ``kind``, declare ports (on the class or per instance) and implement ``run``.

    ``run`` receives the inputs that have arrived, by port name (a list for a ``many`` port). It may return a
    mapping of output port to payload, call ``ctx.emit`` any number of times, or both. Returning ends the
    activation.
    """

    kind: ClassVar[str] = ""                 # the name this kind of node is registered and stored under
    inputs: Iterable[Port] | Mapping[str, Port] = ()    # declared on the class as Ports; a mapping on an instance
    outputs: Iterable[Port] | Mapping[str, Port] = ()
    firing: Firing = Firing.ALL
    time_limit: float | None = None          # the longest one activation may take, in seconds; None: the engine's

    def __init__(self, id: str, *, inputs: Iterable[Port] | None = None, outputs: Iterable[Port] | None = None,
                 firing: Firing | None = None):
        if not isinstance(id, str) or not ID.fullmatch(id):
            raise NodeError(f"invalid node id: {id!r}")
        if not type(self).kind:
            raise NodeError(f"{type(self).__name__} has no kind")
        self.id = id
        self.inputs = _ports(inputs, type(self).inputs)
        self.outputs = _ports(outputs, type(self).outputs)
        if firing is not None:
            self.firing = Firing(firing)

    def config(self) -> Mapping:
        """Plain data that sets this node apart from another of the same kind (it goes into the graph digest)."""
        return {}

    def describe(self) -> dict:
        own = self.__dict__.get("time_limit")
        return {"id": self.id, "kind": type(self).kind, "firing": self.firing.value,
                "inputs": [p.describe() for p in self.inputs.values()],
                "outputs": [p.describe() for p in self.outputs.values()], "config": dict(self.config()),
                **({"time_limit": own} if own is not None else {})}

    # ---- lifecycle: only run is required --------------------------------------------------------------

    def open(self, ctx: Context) -> None:
        """Acquire what the node needs, once, before its first activation."""

    @abstractmethod
    def run(self, ctx: Context, inputs: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """One activation."""

    def close(self, ctx: Context) -> None:
        """Release what ``open`` acquired."""

    def snapshot(self) -> Mapping:
        """Plain data from which ``restore`` can rebuild the node's own state after a restart."""
        return {}

    def restore(self, state: Mapping) -> None:
        pass

    def __repr__(self):
        return f"<{type(self).__name__} {self.id}>"
