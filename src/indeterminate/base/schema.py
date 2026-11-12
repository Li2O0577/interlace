"""What a port carries. A schema checks a value, and says whether it can take what another schema produces.

Schemas are small immutable values. They describe the shape of plain data; they are not a type system.
``schema()`` turns the short forms into schemas: ``None`` is anything, a type is ``Is(type)``, a dict is a
``Record``, a one-item list is ``ListOf``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


class SchemaError(ValueError):
    pass


class Schema(ABC):
    @abstractmethod
    def check(self, value) -> None:
        """Raise SchemaError when ``value`` does not fit."""

    @abstractmethod
    def accepts(self, other: "Schema") -> bool:
        """True when every value that fits ``other`` also fits this schema (decided when a graph is built)."""

    @abstractmethod
    def describe(self):
        """Plain data that identifies this schema, for the graph digest."""


@dataclass(frozen=True)
class Anything(Schema):
    def check(self, value) -> None:
        pass

    def accepts(self, other: Schema) -> bool:
        return True

    def describe(self):
        return "any"


ANY = Anything()


@dataclass(frozen=True)
class Is(Schema):
    """A value of one of the given Python types. ``bool`` is not accepted where only ``int`` is named."""

    types: tuple

    def __init__(self, *types: type):
        if not types or not all(isinstance(t, type) for t in types):
            raise SchemaError("Is() needs at least one type")
        object.__setattr__(self, "types", tuple(types))

    def _fits(self, kind: type) -> bool:
        if kind is bool and bool not in self.types:
            return False
        return issubclass(kind, self.types)

    def check(self, value) -> None:
        if not self._fits(type(value)):
            raise SchemaError(f"expected {self.describe()}, got {type(value).__name__}")

    def accepts(self, other: Schema) -> bool:
        return isinstance(other, Is) and all(self._fits(t) for t in other.types)

    def describe(self):
        return "|".join(sorted(t.__name__ for t in self.types))


@dataclass(frozen=True)
class ListOf(Schema):
    item: Schema = ANY

    def check(self, value) -> None:
        if not isinstance(value, (list, tuple)):
            raise SchemaError(f"expected a list, got {type(value).__name__}")
        for position, entry in enumerate(value):
            try:
                self.item.check(entry)
            except SchemaError as exc:
                raise SchemaError(f"[{position}]: {exc}") from None

    def accepts(self, other: Schema) -> bool:
        return isinstance(other, ListOf) and self.item.accepts(other.item)

    def describe(self):
        return {"list": self.item.describe()}


class Record(Schema):
    """A mapping with named fields. Fields in ``optional`` may be absent; other keys are refused unless ``open``."""

    __slots__ = ("fields", "optional", "open", "_key")

    def __init__(self, fields: Mapping, optional=(), open: bool = False):
        built = {str(name): schema(value) for name, value in fields.items()}
        unknown = set(optional) - built.keys()
        if unknown:
            raise SchemaError(f"optional names no field: {sorted(unknown)}")
        set_ = object.__setattr__
        set_(self, "fields", MappingProxyType(built))
        set_(self, "optional", frozenset(optional))
        set_(self, "open", bool(open))
        set_(self, "_key", (tuple(sorted(built.items(), key=lambda kv: kv[0])), self.optional, self.open))

    def __setattr__(self, name, value):
        raise AttributeError("a schema is immutable")

    def __eq__(self, other):
        return isinstance(other, Record) and self._key == other._key

    def __hash__(self):
        return hash(self._key)

    def __repr__(self):
        return f"Record({dict(self.fields)!r})"

    def check(self, value) -> None:
        if not isinstance(value, Mapping):
            raise SchemaError(f"expected a record, got {type(value).__name__}")
        missing = [name for name in self.fields if name not in value and name not in self.optional]
        if missing:
            raise SchemaError(f"missing {sorted(missing)}")
        extra = [name for name in value if name not in self.fields]
        if extra and not self.open:
            raise SchemaError(f"unknown {sorted(map(str, extra))}")
        for name, expected in self.fields.items():
            if name in value:
                try:
                    expected.check(value[name])
                except SchemaError as exc:
                    raise SchemaError(f"{name}: {exc}") from None

    def accepts(self, other: Schema) -> bool:
        if not isinstance(other, Record):
            return False
        if (other.open or other.fields.keys() - self.fields.keys()) and not self.open:
            return False   # the producer may send a key this record refuses
        for name, expected in self.fields.items():
            if name in other.fields:
                if not expected.accepts(other.fields[name]):
                    return False
                if name in other.optional and name not in self.optional:
                    return False
            elif name not in self.optional:
                return False
        return True

    def describe(self):
        return {"record": {name: s.describe() for name, s in self.fields.items()},
                "optional": sorted(self.optional), "open": self.open}


def schema(value) -> Schema:
    """The schema a short form stands for."""
    if isinstance(value, Schema):
        return value
    if value is None:
        return ANY
    if isinstance(value, type):
        return Is(value)
    if isinstance(value, tuple) and value and all(isinstance(v, type) for v in value):
        return Is(*value)
    if isinstance(value, Mapping):
        return Record(value)
    if isinstance(value, list) and len(value) == 1:
        return ListOf(schema(value[0]))
    raise SchemaError(f"not a schema: {value!r}")
