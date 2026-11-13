"""``artifact``: content of any size travels as a reference; the content itself stays in a store."""
from __future__ import annotations

import json
from typing import Any

from ..base import Port, Protocol, ProtocolError, Ref, canonical
from .store import Store

TEXT, BYTES, JSON = "text/plain; charset=utf-8", "application/octet-stream", "application/json"


class Artifact(Protocol):
    """The sender emits text, bytes or plain data; the envelope holds a ``Ref`` (hash, size, kind) and the
    receiver gets the content back in the form it was sent. Both ends must reach the same store."""

    name = "artifact"

    def __init__(self, store: Store):
        if not isinstance(store, Store):
            raise ProtocolError(f"not a store: {store!r}")
        self.store = store

    def encode(self, payload: Any, source: Port) -> Ref:
        source.schema.check(payload)
        if isinstance(payload, str):
            return self.store.put(payload.encode("utf-8"), TEXT)
        if isinstance(payload, (bytes, bytearray, memoryview)):
            return self.store.put(payload, BYTES)
        try:
            return self.store.put(canonical(payload).encode("utf-8"), JSON)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"an artifact is text, bytes or plain data: {exc}") from None

    def encode_anyway(self, payload: Any, source: Port) -> Ref:
        raise ProtocolError("an artifact that cannot be stored cannot travel")

    def decode(self, body: Any, target: Port) -> Any:
        if not isinstance(body, Ref):
            raise ProtocolError(f"an artifact arrives as a Ref, not {type(body).__name__}")
        data = self.store.get(body)
        if body.media == TEXT:
            return data.decode("utf-8")
        return json.loads(data) if body.media == JSON else data
