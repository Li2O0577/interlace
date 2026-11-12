"""The bottom layer of the agent runtime: every project's agents in memory and their files on disk.

``AgentRecords`` knows nothing about models, tools, scheduling or graphs. It keeps the in-memory states, writes
records, transcripts and inboxes atomically, and answers who is who (parent, children, names). The first time a
project is touched it calls ``ensure_loaded(pid)``, which the runtime supplies; that is its only way up.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from ..core.jsonfile import replace_file
from .state import AgentError, AgentState

OPERATOR = "human"


class AgentRecords:
    def __init__(self, store, ensure_loaded: Callable[[str], None] = lambda pid: None):
        self.store = store
        self.states: dict[tuple[str, str], AgentState] = {}
        self._ensure_loaded = ensure_loaded

    # ------------------------------------------------------------------ files

    def root(self, pid: str) -> Path:
        return self.store.path(pid)

    def folder(self, pid: str, aid: str) -> Path:
        return self.root(pid) / "agents" / aid

    def save(self, st: AgentState) -> None:
        with st.io_lock:  # the agent's thread and request threads may save at the same time
            st.rec["updated"] = time.time()
            path = self.folder(st.project_id, st.rec["id"]) / "agent.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"agent.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(st.rec, ensure_ascii=False, indent=2), encoding="utf-8")
            replace_file(tmp, path)

    def append_message(self, st: AgentState, msg: dict) -> None:
        msg = {**msg, "ts": time.time()}
        with st.io_lock:
            st.messages.append(msg)
            with (self.folder(st.project_id, st.rec["id"]) / "messages.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def rewrite_messages(self, st: AgentState) -> None:
        path = self.folder(st.project_id, st.rec["id"]) / "messages.jsonl"
        path.write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in st.messages), encoding="utf-8")

    def save_inbox(self, st: AgentState) -> None:
        path = self.folder(st.project_id, st.rec["id"]) / "inbox.json"
        with st.io_lock:
            try:
                path.write_text(json.dumps(st.inbox, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass

    # ------------------------------------------------------------------ lookups

    def get(self, pid: str, aid: str) -> AgentState:
        self._ensure_loaded(pid)
        st = self.states.get((pid, aid))
        if st is None:
            raise AgentError(f"找不到 Agent：{aid}")
        return st

    def exists(self, pid: str, aid: str) -> bool:
        if not aid:
            return False
        self._ensure_loaded(pid)
        return (pid, aid) in self.states

    def project(self, pid: str) -> list[AgentState]:
        self._ensure_loaded(pid)
        return [st for (p, _), st in list(self.states.items()) if p == pid]

    def children(self, pid: str, aid: str | None) -> list[AgentState]:
        return [st for st in self.project(pid) if st.rec.get("parent") == aid]

    def descendants(self, pid: str, aid: str) -> list[AgentState]:
        result, frontier = [], [aid]
        while frontier:
            current = frontier.pop()
            for child in self.children(pid, current):
                result.append(child)
                frontier.append(child.rec["id"])
        return result

    def ancestors(self, pid: str, aid: str) -> list[str]:
        chain, current = [], self.get(pid, aid).rec.get("parent")
        while current:
            chain.append(current)
            current = self.get(pid, current).rec.get("parent")
        return chain

    def name_of(self, pid: str, aid: str | None) -> str:
        if aid in (None, OPERATOR):
            return "用户"
        try:
            return self.get(pid, aid).rec["name"]
        except AgentError:
            return aid


def read_jsonl(path) -> tuple[list[dict], int]:
    """The lines of a conversation log. A line cut short (the machine stopped mid-write) is dropped
    instead of making the whole project's agents unloadable."""
    out, dropped = [], 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            item = None
        if isinstance(item, dict):
            out.append(item)
        else:
            dropped += 1
    return out, dropped
