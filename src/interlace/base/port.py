"""A port: one named place where a node takes information in or gives it out."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .schema import ANY, schema as as_schema

NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


class PortError(ValueError):
    pass


@dataclass(frozen=True)
class Port:
    """``required``: an input the node cannot start without. ``many``: an input several edges may feed.
    Both only matter on inputs; an output can always feed any number of edges.

    ``call``: an output through which the node asks and waits for an answer (``ctx.call``) instead of sending.
    Its one edge must be a call.

    What passes through a port is checked: against the schema, by the protocol of each edge, and by
    ``check``, one function or several, each called with the payload once it has the right shape. A check that
    returns a string (what is wrong), returns ``False`` or raises has found a problem. What then happens is the port's
    to say:

    ``invalid="fail"``   (the default) the node fails and the run stops; it can be resumed
    ``invalid="warn"``   the problem is recorded and the payload goes on as it is
    ``repairs=N``        before either, the node is told what is wrong and may send again, up to N times in
                         one activation; ``None`` leaves the number to the node (most never repair, an agent
                         does twice)

    On an input, only ``invalid`` matters: it says what happens when what arrives is not what was sent (a
    file that has changed on the way, for instance).
    """

    name: str
    schema: Any = ANY    # a Schema, or what ``schema()`` makes one from (a type, a mapping); a Schema afterwards
    required: bool = True
    many: bool = False
    call: bool = False
    check: Any = ()      # a function of the payload, or several; kept as a tuple
    invalid: str = "fail"
    repairs: int | None = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not NAME.fullmatch(self.name):
            raise PortError(f"invalid port name: {self.name!r}")
        object.__setattr__(self, "schema", as_schema(self.schema))
        checks = (self.check,) if callable(self.check) else tuple(self.check or ())
        if not all(callable(check) for check in checks):
            raise PortError(f"{self.name}: a check is a function of the payload")
        object.__setattr__(self, "check", checks)
        if self.invalid not in ("fail", "warn"):
            raise PortError(f"{self.name}: invalid must be 'fail' or 'warn', not {self.invalid!r}")
        if self.repairs is not None and (type(self.repairs) is not int or self.repairs < 0):
            raise PortError(f"{self.name}: repairs must be a whole number, 0 or more")

    def problem(self, payload) -> str | None:
        """What this port's own checks find wrong with a payload, or None."""
        for check in self.check:
            try:
                found = check(payload)
            except ValueError as exc:
                return str(exc) or type(exc).__name__
            except Exception as exc:   # noqa: BLE001 - a check that cannot judge the payload has not passed it
                return f"{getattr(check, '__name__', 'the check')} could not judge it: {type(exc).__name__}: {exc}"
            if found is False:
                return f"{getattr(check, '__name__', 'the check')} does not hold"
            if isinstance(found, str) and found:
                return found
        return None

    def describe(self) -> dict:
        described = {"name": self.name, "schema": self.schema.describe(), "required": self.required, "many": self.many,
                     "call": self.call}
        if self.check:
            described["check"] = [f"{getattr(c, '__module__', '')}.{getattr(c, '__qualname__', type(c).__qualname__)}"
                                  for c in self.check]
        if self.invalid != "fail":
            described["invalid"] = self.invalid
        if self.repairs is not None:
            described["repairs"] = self.repairs
        return described
