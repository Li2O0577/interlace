"""The edge: one flow of information from an output port to an input port, under a protocol."""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple


class EdgeError(ValueError):
    pass


class Endpoint(NamedTuple):
    node: str
    port: str

    def __str__(self):
        return f"{self.node}.{self.port}"

    @classmethod
    def of(cls, value) -> "Endpoint":
        """From an Endpoint, a ``(node, port)`` pair or the text ``"node.port"``."""
        if isinstance(value, str):
            node, dot, port = value.partition(".")
            if not (node and dot and port):
                raise EdgeError(f"expected 'node.port': {value!r}")
            return cls(node, port)
        try:
            node, port = value
        except (TypeError, ValueError):
            raise EdgeError(f"expected (node, port): {value!r}") from None
        return cls(str(node), str(port))


@dataclass(frozen=True)
class Edge:
    """``back`` marks an edge that closes a loop: it never counts toward a node's first start, and it must say
    how many rounds it may open at most (``max_rounds``), so no loop can run for ever by construction."""

    id: str
    source: Endpoint
    target: Endpoint
    protocol: str
    back: bool = False
    max_rounds: int | None = None
    #: The receiver may reject what comes on this edge (``ctx.reject``) this many times in all; None: never.
    challenges: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "source", Endpoint.of(self.source))
        object.__setattr__(self, "target", Endpoint.of(self.target))
        if not isinstance(self.id, str) or not self.id.strip():
            raise EdgeError("an edge needs an id")
        if not isinstance(self.protocol, str) or not self.protocol:
            raise EdgeError(f"edge {self.id}: no protocol")
        if self.back:
            if type(self.max_rounds) is not int or self.max_rounds < 1:
                raise EdgeError(f"edge {self.id}: a back edge needs max_rounds of at least 1")
        elif self.max_rounds is not None:
            raise EdgeError(f"edge {self.id}: only a back edge has max_rounds")
        if self.challenges is not None and (type(self.challenges) is not int or self.challenges < 1):
            raise EdgeError(f"edge {self.id}: challenges is how many rejections it takes, a whole number of at least 1")

    def describe(self) -> dict:
        return {"id": self.id, "from": str(self.source), "to": str(self.target), "protocol": self.protocol,
                "back": self.back, "max_rounds": self.max_rounds,
                **({"challenges": self.challenges} if self.challenges is not None else {})}
