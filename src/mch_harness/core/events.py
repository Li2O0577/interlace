"""Append-only event log per project, with live subscribers and replay.

Every event gets a monotonically increasing ``seq``. Clients remember the last
``seq`` they saw and ask for everything after it when they reconnect, so no
event is lost across page reloads or network drops.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

from .jsonfile import finite


def _seq_of(raw: bytes) -> int | None:
    """The seq of a log line without parsing it (lines start with {"seq": N, …})."""
    if raw.startswith(b'{"seq": '):
        end = raw.find(b",", 8)
        if end > 0:
            try:
                return int(raw[8:end])
            except ValueError:
                return None
    return None


class EventLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._index: dict[int, int] = {}   # seq → byte offset of its line, every 200 events
        self._seq = self._load_last_seq()
        self._lock = threading.RLock()  # delivery happens under the lock to keep seq order
        # Each subscriber is a queue plus the event loop it belongs to, so that
        # events appended from worker threads are delivered thread-safely.
        self._subscribers: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}

    def _load_last_seq(self) -> int:
        if not self.path.exists():
            return 0
        last, offset = 0, 0
        with self.path.open("rb") as f:
            for raw in f:
                seq = _seq_of(raw)
                if seq is None:
                    try:
                        seq = json.loads(raw)["seq"]
                    except (ValueError, KeyError, TypeError):
                        seq = None
                if seq is not None:
                    last = seq
                    self._remember(seq, offset)
                offset += len(raw)
        return last

    @property
    def last_seq(self) -> int:
        return self._seq

    def append(self, type: str, data: dict[str, Any] | None = None, source: str = "system") -> dict:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "ts": time.time(), "type": type, "source": source, "data": finite(data or {})}
            with self.path.open("ab") as f:
                pos = f.tell()
                f.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
            self._remember(self._seq, pos)
            self._deliver(event)
        return event

    def broadcast(self, type: str, data: dict[str, Any] | None = None, source: str = "system") -> None:
        """Send a short-lived event (e.g. live experiment progress) to open
        connections without writing it to the log or giving it a seq."""
        event = {"seq": None, "ephemeral": True, "ts": time.time(), "type": type, "source": source, "data": finite(data or {})}
        self._deliver(event)

    def _deliver(self, event: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers.items())
        for queue, loop in subscribers:
            if loop.is_closed():
                continue
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                queue.put_nowait(event)
            else:
                loop.call_soon_threadsafe(queue.put_nowait, event)

    def replay(self, after: int = 0, limit: int | None = None) -> list[dict]:
        """Events with seq > ``after`` (the last ``limit`` of them). Lines before ``after`` are skipped by their
        seq prefix without parsing, and the read starts from a remembered offset, so a reconnect after a long
        project only reads what is new."""
        if not self.path.exists():
            return []
        events = []
        start = self._offset_before(after)
        with self.path.open("rb") as f:
            f.seek(start)
            offset = start
            for raw in f:
                pos, offset = offset, offset + len(raw)
                seq = _seq_of(raw)
                if seq is not None and seq <= after:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if event.get("seq", 0) > after:
                    events.append(event)
                    self._remember(event["seq"], pos)
        if limit is not None:
            events = events[-limit:] if limit > 0 else []
        return events

    def tail(self, limit: int) -> tuple[list[dict], int]:
        """The last ``limit`` events and how many earlier ones were left out, reading the file from the end."""
        if limit <= 0 or not self.path.exists():
            return [], self._seq
        size = self.path.stat().st_size
        block, data = 1 << 16, b""
        with self.path.open("rb") as f:
            pos = size
            while pos > 0 and data.count(b"\n") <= limit + 1:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
        lines = data.splitlines()
        if pos > 0:
            lines = lines[1:]  # first line may be cut
        events = []
        for raw in lines[-limit:]:
            try:
                events.append(json.loads(raw))
            except ValueError:
                continue
        first = events[0]["seq"] if events else self._seq + 1
        return events, max(0, first - 1)

    def _offset_before(self, after: int) -> int:
        """Byte offset of a remembered event at or before ``after`` (0 if none)."""
        best = 0
        for seq, pos in self._index.items():
            if seq <= after and pos > best:
                best = pos
        return best

    def _remember(self, seq: int, pos: int) -> None:
        if seq % 200 == 0:
            self._index[seq] = pos

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[queue] = asyncio.get_running_loop()
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.pop(queue, None)


class EventHub:
    """Holds one EventLog per project."""

    def __init__(self) -> None:
        self._logs: dict[str, EventLog] = {}

    def get(self, project_id: str, path: Path) -> EventLog:
        log = self._logs.get(project_id)
        if log is None:
            log = self._logs[project_id] = EventLog(path)
        return log

    def drop(self, project_id: str) -> None:
        self._logs.pop(project_id, None)
