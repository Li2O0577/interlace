"""The protocol of an edge: what may travel on it, how it is delivered, and whether its arrival starts work.

A protocol says nothing about how an envelope physically reaches the other end; that is the engine's transport.
"""
from __future__ import annotations

import re
from abc import ABC
from enum import Enum
from typing import Any, ClassVar, Iterable, Iterator

from .port import Port

NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


class ProtocolError(ValueError):
    pass


class Delivery(Enum):
    """What the receiving port keeps."""

    LATEST = "latest"   # only the newest envelope
    QUEUE = "queue"     # every envelope, in order
    STREAM = "stream"   # a sequence that is still being written while it is read


class Protocol(ABC):
    name: ClassVar[str] = ""
    delivery: ClassVar[Delivery] = Delivery.QUEUE
    triggers: ClassVar[bool] = True    # an arrival counts toward starting the receiving node

    def check(self, source: Port, target: Port) -> None:
        """When a graph is built: may an edge with this protocol join these two ports?"""
        if not target.schema.accepts(source.schema):
            raise ProtocolError(f"{target.name} does not accept what {source.name} produces")

    def encode(self, payload: Any, source: Port) -> Any:
        """When a node emits: validate the payload and return the envelope body."""
        source.schema.check(payload)
        return payload

    def decode(self, body: Any, target: Port) -> Any:
        """When a node receives: turn the envelope body back into what the node reads."""
        return body


class Protocols:
    """The protocols one engine knows, by name."""

    def __init__(self, protocols: Iterable[Protocol] = ()):
        self._by_name: dict[str, Protocol] = {}
        for protocol in protocols:
            self.register(protocol)

    def register(self, protocol: Protocol) -> Protocol:
        name = protocol.name
        if not isinstance(protocol, Protocol) or not isinstance(name, str) or not NAME.fullmatch(name):
            raise ProtocolError(f"invalid protocol: {protocol!r}")
        if name in self._by_name:
            raise ProtocolError(f"protocol already registered: {name}")
        self._by_name[name] = protocol
        return protocol

    def get(self, name: str) -> Protocol | None:
        return self._by_name.get(name)

    def __contains__(self, name) -> bool:
        return name in self._by_name

    def __iter__(self) -> Iterator[Protocol]:
        return iter(self._by_name.values())

    def copy(self) -> "Protocols":
        return Protocols(self._by_name.values())
