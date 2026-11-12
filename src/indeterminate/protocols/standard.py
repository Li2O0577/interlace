"""The protocols that need nothing outside themselves."""
from __future__ import annotations

from typing import Any

from ..base import Delivery, Port, Protocol, ProtocolError, SchemaError


class Latest(Protocol):
    """``latest``: only the newest value waits at the receiving port; an older one not yet read is replaced."""

    name = "latest"
    delivery = Delivery.LATEST


class State(Protocol):
    """``state``: the newest value is there to be read whenever the receiving node runs, but its arrival never
    starts the node, and reading does not use it up. A setting, a position, the last frame of a camera."""

    name = "state"
    delivery = Delivery.LATEST
    triggers = False


class Signal(Protocol):
    """``signal``: an event with no content. Whatever is emitted, ``None`` arrives; only the arrival matters."""

    name = "signal"

    def check(self, source: Port, target: Port) -> None:
        if source.call:
            raise ProtocolError(f"{source.name} is a call port and needs a call edge")
        try:
            target.schema.check(None)
        except SchemaError:
            raise ProtocolError(f"{target.name} must accept None to take a signal") from None

    def encode(self, payload: Any, source: Port) -> Any:
        return None


class Stream(Protocol):
    """``stream``: everything one activation emits is one sequence, which the receiver reads as it is written."""

    name = "stream"
    delivery = Delivery.STREAM


class Call(Protocol):
    """``call``: a request that is answered. The sender uses ``ctx.call`` and waits; the receiving node runs
    for that request alone, and what it returns is the answer."""

    name = "call"
    delivery = Delivery.CALL
