"""The envelope: the one form in which information travels along an edge."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Ref:
    """A body kept elsewhere (a file, a large value): the envelope and the log carry only this reference."""

    digest: str        # content hash, which is also how the content store finds it
    size: int = 0
    media: str = ""    # what it is, when known: "text/markdown", "application/json", ...


@dataclass(frozen=True)
class Envelope:
    id: str
    edge: str                       # the edge it travels on
    seq: int                        # its number on that edge, from 1
    round: int                      # the round of the activation that sent it
    body: Any                       # small data inline, anything large as a Ref
    digest: str                     # hash of the body
    cause: tuple[str, ...] = ()     # the envelopes the sending activation had received: where this came from
    replaces: str = ""              # the id of an envelope already read that this one corrects
