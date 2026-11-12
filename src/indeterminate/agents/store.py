"""Agent lifecycle (load, resume, shut down) and read-only views; files and lookups live in records.py."""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import providers
from .records import read_jsonl
from .state import (ACTIVE, AgentError, AgentState, spent_tokens)


class StoreMixin:
    """Lifecycle of the agents (load, resume, shut down) and the views shown to the operator. Files and lookups
    belong to ``self.records`` (records.py); the short names below stay for the rest of the runtime."""

    # =================================================================== records (see records.py)

    def _root(self, pid: str) -> Path:
        return self.records.root(pid)

    def _dir(self, pid: str, aid: str) -> Path:
        return self.records.folder(pid, aid)

    def _save(self, st: AgentState) -> None:
        self.records.save(st)

    def _append_message(self, st: AgentState, msg: dict) -> None:
        self.records.append_message(st, msg)

    def _rewrite_messages(self, st: AgentState) -> None:
        self.records.rewrite_messages(st)

    def _save_inbox(self, st: AgentState) -> None:
        self.records.save_inbox(st)

    def _get(self, pid: str, aid: str) -> AgentState:
        return self.records.get(pid, aid)

    def _project_states(self, pid: str) -> list[AgentState]:
        return self.records.project(pid)

    def children(self, pid: str, aid: str | None) -> list[AgentState]:
        return self.records.children(pid, aid)

    def descendants(self, pid: str, aid: str) -> list[AgentState]:
        return self.records.descendants(pid, aid)

    def ancestors(self, pid: str, aid: str) -> list[str]:
        return self.records.ancestors(pid, aid)

    def name_of(self, pid: str, aid: str | None) -> str:
        return self.records.name_of(pid, aid)

    def exists(self, pid: str, aid: str) -> bool:
        return self.records.exists(pid, aid)

    # =================================================================== setup / lifecycle

    def _default_provider(self, model: str) -> providers.Provider:
        return providers.build(model, self.settings.raw().get("providers", []))

    def _defaults(self) -> dict:
        return self.settings.raw().get("defaults", {})

    def load(self, pid: str) -> None:
        """Load a project's agents once. Agents that were working when the engine stopped continue
        automatically (setting ``auto_resume_agents``) or come back paused; unread messages are kept."""
        resume = []
        with self.guard:
            if pid in self.loaded:
                return
            self.loaded.add(pid)
            auto = bool(self._defaults().get("auto_resume_agents", True))
            for path in sorted((self._root(pid) / "agents").glob("*/agent.json")):
                try:
                    rec = json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                st = AgentState(pid, rec)
                msg_path = path.parent / "messages.jsonl"
                if msg_path.exists():
                    st.messages, dropped = read_jsonl(msg_path)
                    if dropped:
                        self._rewrite_messages(st)   # so the next line is not glued to the broken one
                inbox_path = path.parent / "inbox.json"
                if inbox_path.exists():
                    try:
                        st.inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
                    except ValueError:
                        st.inbox = []
                self._repair(st)
                if rec.get('graph_runtime') and rec.get('status') in ACTIVE | {'queued'}:
                    if not self.graph(pid).can_schedule(st):
                        rec['status']='queued'
                        rec['graph_start_ready']=False
                if rec.get("status") in ACTIVE:
                    if auto:
                        rec["status"], st.pending_turn = "running", True
                        resume.append(st)
                    else:
                        rec["status"] = "paused"
                        rec["hold"] = {"by": "system", "reason": "引擎重启后暂停，恢复后继续"}
                    self._save(st)
                self.states[(pid, rec["id"])] = st
            self._restore_approvals(pid)
        for st in resume:
            self._ensure_thread(st)
        if resume or any(s.rec["status"] == "queued" for s in self._project_states(pid)):
            self._schedule(pid)

    def _repair(self, st: AgentState) -> None:
        """Tool calls cut off by a shutdown get an explicit "interrupted" result, so the
        conversation stays valid for the model and the agent knows to redo that step."""
        answered = {m.get("tool_call_id") for m in st.messages if m["role"] == "tool"}
        last = next((m for m in reversed(st.messages) if m["role"] == "assistant"), None)
        if not last:
            return
        missing = [c for c in last.get("tool_calls") or [] if c["id"] not in answered]
        for c in missing:
            self._append_message(st, {"role": "tool", "tool_call_id": c["id"], "name": c["name"], "error": True,
                                      "content": "已中断：引擎在执行这一步时重启了，请检查结果后视情况重新执行。"})

    def resume_all(self) -> None:
        """Load every project at startup so interrupted agents continue without waiting for a page visit."""
        for project in self.store.list():
            try:
                self.load(project["id"])
                if (self._root(project['id'])/'state/graph.json').exists():
                    self.graph(project['id']).tick()
            except Exception:  # noqa: BLE001 — one broken project must not block the others
                continue

    def shutdown(self) -> None:
        with self.guard:
            self.closing = True
            states=list(self.states.values())
        for st in states:
            with st.cv:
                st.stop = True
                st.cv.notify_all()
        for approval in self.approvals.values():
            approval.event.set()

    # =================================================================== queries

    def view(self, st: AgentState) -> dict:
        rec = st.rec
        cost, currency = self.cost_of(rec)
        return {**rec, "inbox": len(st.inbox), "children": [c.rec["id"] for c in self.children(st.project_id, rec["id"])],
                "cost": cost, "currency": currency, "spent": spent_tokens(rec.get("usage", {})),
                "thinking_level": self.thinking_of(st)}

    def cost_of(self, rec: dict) -> tuple[float | None, str]:
        """Money spent by one agent at the prices set for its model (None when no price is set)."""
        spec = rec.get("model") or ""
        pid_, _, model = spec.partition(":")
        provider = next((p for p in self.settings.raw().get("providers", []) if pid_ in (p.get("id"), p.get("name"))), None)
        price = (provider or {}).get("prices", {}).get(model)
        if not price:
            reported = rec.get("usage", {}).get("cost_usd")
            if reported is not None:  # Claude Code reports its own cost
                return round(reported, 4), "$"
            return None, (provider or {}).get("currency", "¥")
        u = rec.get("usage", {})
        cached = min(u.get("cached_tokens", 0), u.get("input_tokens", 0))
        cost = ((u.get("input_tokens", 0) - cached) * price["input"] + cached * price["cached"]
                + u.get("output_tokens", 0) * price["output"]) / 1e6
        return round(cost, 4), provider.get("currency", "¥")

    def project_cost(self, pid: str) -> dict:
        totals: dict[str, float] = {}
        unpriced = 0
        for st in self._project_states(pid):
            cost, currency = self.cost_of(st.rec)
            if cost is None:
                unpriced += st.rec.get("usage", {}).get("steps", 0) > 0
            else:
                totals[currency] = round(totals.get(currency, 0) + cost, 4)
        return {"totals": totals, "unpriced_agents": unpriced}

    def list(self, pid: str) -> list[dict]:
        return sorted((self.view(st) for st in self._project_states(pid)), key=lambda r: r["created"])

    def get(self, pid: str, aid: str, limit: int = 300) -> dict:
        st = self._get(pid, aid)
        messages = []
        for m in st.messages[-limit:]:
            if not st.rec.get('graph_runtime') and m.get("role") == "assistant" and "```view" in (m.get("content") or ""):
                body, views = self.host.views.embed(self._root(pid), m["content"])  # show the views the agent wrote, rendered
                m = {**m, "content": body, "views": views}
            messages.append(m)
        return {**self.view(st), "messages": messages}

    def pending_approvals(self, pid: str) -> list[dict]:
        return [a.public() | {"agent_name": self.name_of(pid, a.agent)} for a in self.approvals.values()
                if a.project_id == pid and a.status == "pending"]
