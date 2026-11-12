"""A port: one named place where a node takes information in or gives it out."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .schema import ANY, Schema, schema as as_schema

NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


class PortError(ValueError):
    pass


@dataclass(frozen=True)
class Port:
    """``required``: an input the node cannot start without. ``many``: an input several edges may feed.

    Both only matter on inputs; an output can always feed any number of edges.
    """

    name: str
    schema: Schema = ANY
    required: bool = True
    many: bool = False

    def __post_init__(self):
        if not isinstance(self.name, str) or not NAME.fullmatch(self.name):
            raise PortError(f"invalid port name: {self.name!r}")
        object.__setattr__(self, "schema", as_schema(self.schema))

    def describe(self) -> dict:
        return {"name": self.name, "schema": self.schema.describe(), "required": self.required, "many": self.many}
