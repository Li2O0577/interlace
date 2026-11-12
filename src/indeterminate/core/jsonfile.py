"""Small JSON state files written safely from several threads.

Agents run in their own threads and the web server answers requests in others, so two writers can
reach the same state file at once. Each write goes to its own temporary file before the atomic
rename (a shared ``.tmp`` name lets one writer rename the other's half-written file away), and
``locked(path)`` serialises a read-modify-write of one file.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def _lock(path: Path) -> threading.RLock:
    key = os.path.abspath(path)
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


@contextmanager
def locked(path: Path):
    with _lock(path):
        yield


def finite(data):
    """The same data with NaN and ±inf floats replaced by None: they are not JSON, and one of them in a stored
    record makes every response that includes it fail."""
    if isinstance(data, float):
        return data if math.isfinite(data) else None
    if isinstance(data, dict):
        return {k: finite(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [finite(v) for v in data]
    return data


def replace_file(source: Path,target: Path) -> None:
    """Publish a complete file, tolerating brief Windows reader sharing locks."""
    for attempt in range(10):
        try:
            source.replace(target)
            return
        except PermissionError:
            if os.name!='nt' or attempt==9:
                raise
            time.sleep(.005*(attempt+1))


def write(path: Path, data, indent: int | None = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(finite(data), ensure_ascii=False, indent=indent), encoding="utf-8")
        replace_file(tmp,path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
