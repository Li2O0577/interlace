"""What a node finds on an input fed by a stream: the chunks one activation of the sender emits, in order."""
from __future__ import annotations

import threading
from collections import deque


class Stream:
    """Iterate to read. Reading waits while the sender is still running and ends when its activation ends."""

    __slots__ = ("_protocol", "_port", "_chunks", "_changed", "_closed", "_kept")

    def __init__(self, protocol, port, keep: bool = False):
        self._protocol, self._port = protocol, port
        self._kept: list | None = [] if keep else None    # every chunk, for the journal
        self._chunks: deque = deque()
        self._changed = threading.Condition()
        self._closed = False

    def _put(self, body) -> None:
        with self._changed:
            self._chunks.append(body)
            if self._kept is not None:
                self._kept.append(body)
            self._changed.notify_all()

    def _close(self) -> None:
        with self._changed:
            self._closed = True
            self._changed.notify_all()

    def __iter__(self):
        return self

    def __next__(self):
        with self._changed:
            while not self._chunks and not self._closed:
                self._changed.wait()
            if not self._chunks:
                raise StopIteration
            body = self._chunks.popleft()
        return self._protocol.decode(body, self._port)
