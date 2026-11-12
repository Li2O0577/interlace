"""Agent state, approvals, constants and small helpers shared by the runtime modules."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

LEAD_ID = "lead"
ACTIVE = {"running", "waiting_approval", "waiting_user", "waiting_parent", "waiting_children"}
TERMINAL = {"done", "stopped", "failed"}
# System notices that wake an idle agent: a run it started has finished, or a child stopped on its own.
WAKING_NOTICES = {"run_done", "child_stopped", "graph.start_blocked", "graph.helpers_requested", "graph.change_approved", "external_request", "graph.block_progress", "graph.block_review_requested"}
PRIORITY = {"human": 0, "parent": 1, "child": 2, "peer": 2, "system": 3}
TOOL_RESULT_LIMIT = 20_000
SUB_MAX_STEPS = 80


class AgentError(Exception):
    pass


MIN_SUB_BUDGET = 60_000
CACHE_WEIGHT = 0.1  # cached prompt tokens are billed at roughly a tenth: count them that way against budgets


def spent_tokens(usage: dict) -> int:
    """Budget accounting in "equivalent tokens": cache hits count at CACHE_WEIGHT."""
    cached = min(usage.get("cached_tokens", 0), usage.get("input_tokens", 0))
    return int(usage.get("input_tokens", 0) - cached * (1 - CACHE_WEIGHT) + usage.get("output_tokens", 0))



# ---------------------------------------------------------------------------- state

@dataclass
class AgentState:
    project_id: str
    rec: dict
    messages: list = field(default_factory=list)
    inbox: list = field(default_factory=list)
    cv: threading.Condition = field(default_factory=threading.Condition)
    thread: threading.Thread | None = None
    io_lock: threading.RLock = field(default_factory=threading.RLock)
    stop: bool = False
    pending_turn: bool = False
    nudged: bool = False
    stop_reason: str = ""   # why the last model call ended (length = output cut at the limit)
    bridge_token: str | None = None  # valid only while a CLI turn runs
    working: bool = False            # inside a turn (see watchdog.py)
    stale_since: float | None = None


@dataclass
class Approval:
    id: str
    project_id: str
    agent: str
    tool: str
    params: dict
    created: float
    status: str = "pending"
    reason: str = ""
    edits: dict = field(default_factory=dict)      # agent.spawn: what the user changed before approving
    kind: str = "ask"         # ask: the agent waits for it; confirm: already done, the user confirms or sends it back
    orphan: bool = False      # restored after a restart: nobody is waiting on it any more
    event: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict:
        return {"id": self.id, "agent": self.agent, "tool": self.tool, "params": self.params, "created": self.created,
                "status": self.status, "reason": self.reason, "kind": self.kind,
                **({"restored": True} if self.orphan else {})}

    def stored(self) -> dict:
        return {**self.public(), "project_id": self.project_id}


def _report_for_parent(rec: dict) -> dict | None:
    """The report as the parent sees it: attached views become references it can forward with view.show(ref=…).
    A report the user has not reviewed yet (manual team mode) is withheld."""
    report = rec.get("report")
    if report and report.get("review") == "pending":
        return {"status": "awaiting_user_review", "summary": "（汇报正在等用户审查，审查后会转给你）"}
    if report and report.get("review") == "accepted":
        report = {**report, "user_review": "用户已审查这份汇报" + (f"，批注（以此为准）：{report['user_note']}" if report.get("user_note") else "，无补充")}
        report.pop("user_note", None)
        report.pop("review", None)
    if not report or not report.get("views"):
        return report
    out = {k: v for k, v in report.items() if k not in ("views", "view_summaries")}
    out["views"] = [{"ref": f"{rec['id']}#{i}", **s} for i, s in enumerate(report.get("view_summaries") or [])]
    out["views_note"] = "用 view.show(ref=[…]) 原样展示给用户"
    return out


def _short(params: dict, limit: int = 300) -> dict:
    out = {}
    for k, v in (params or {}).items():
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        out[k] = text if len(text) <= limit else text[:limit] + "…"
    return out


def _to_text(result: Any) -> str:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    return text if len(text) <= TOOL_RESULT_LIMIT else text[:TOOL_RESULT_LIMIT] + "\n…（结果过长，已截断；需要时用更精确的参数再查）"
