"""Per-project re-entrant locks: git and workflow operations on one project
never overlap, whether they come from a request, a tool or the experiment
monitor."""

from __future__ import annotations

import threading


class ProjectLocks:
    def __init__(self) -> None:
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def __getitem__(self, project_id: str) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(project_id)
            if lock is None:
                lock = self._locks[project_id] = threading.RLock()
            return lock
