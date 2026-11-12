"""Keeping long conversations within the context window.

Three steps, cheapest first:

1. **Trim old tool output.** Large results from earlier steps (a file read, a long log) are cut to their
   head and tail; the full text goes to ``messages.archive.jsonl`` and the agent can read the file again.
   No model call, nothing the agent decided is lost.
2. **Summarise older steps.** When that is not enough, the steps before the last few are summarised by the
   agent's own model into ``rec["summary"]`` (shown in the system prompt as 之前工作的摘要). Summaries
   from earlier compactions are merged into one, so the oldest decisions are not the first to drop out.
3. **CLI agents** (Claude Code / Codex) keep their own conversation in a session. When the CLI's last request
   carried more than the limit, the turns recorded since the session started grow past twice it, or the user asks, the CLI is asked for a
   summary in one extra turn, and the next turn starts a fresh session with that summary in the prompt.

The user can compact any agent by hand (``AgentManager.compact``).
"""

from __future__ import annotations

import json
import time

from .providers import ProviderError
from .state import AgentError, AgentState

KEEP_STEPS = 8            # the most recent steps whose tool output stays whole
TRIM_OVER = 1500          # tool output longer than this (characters) is trimmed in older steps
SUMMARY_CHARS = 6000      # a merged summary longer than this is summarised again

SUMMARY_PROMPT = ("你负责压缩 Agent 的工作记录。保留：任务与上级/用户的要求、已做的决定和理由、已产出的文件路径、关键数字与结论（带出处）、"
                  "目标与当前最好结果、未完成的事项、遇到的问题和已经排除的做法。删去过程性细节和工具输出原文。用中文，条目式，不超过 1000 字。")
MERGE_PROMPT = ("下面是同一个 Agent 前后几次压缩得到的工作摘要，按时间先后排列。把它们合并成一份：保留全部仍然有效的要求、决定、文件、数字和未完成事项，"
                "后面的内容推翻前面时以后面为准，删去重复。用中文，条目式，不超过 1500 字。")
CLI_SUMMARY_PROMPT = ("【系统通知】对话很长了，引擎要为你开一个新会话。请先不要调用任何工具，直接用中文写一份工作摘要作为这一轮的回复："
                      "任务与上级/用户的要求、已做的决定和理由、已产出的文件路径、关键数字与结论（带出处）、目标与当前最好结果、"
                      "未完成的事项和下一步、遇到的问题和已经排除的做法。条目式，不超过 1500 字。新会话会带着这份摘要继续。")


def estimate(messages: list[dict]) -> int:
    """Rough token count of a message list (about three characters a token for mixed Chinese and code)."""
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 3


def _step_starts(messages: list[dict]) -> list[int]:
    return [i for i, m in enumerate(messages) if m["role"] == "assistant"]


def trim_tool_output(messages: list[dict], keep_steps: int = KEEP_STEPS, over: int = TRIM_OVER) -> list[tuple[int, dict]]:
    """Cut long tool results that are older than the last ``keep_steps`` steps, in place.
    Returns (index, original message) for what was cut, for the archive."""
    starts = _step_starts(messages)
    if len(starts) <= keep_steps:
        return []
    boundary = starts[-keep_steps]
    cut = []
    for i, m in enumerate(messages[:boundary]):
        content = m.get("content")
        if m["role"] != "tool" or not isinstance(content, str) or len(content) <= over or m.get("trimmed"):
            continue
        cut.append((i, dict(m)))
        dropped = len(content) - 900
        messages[i] = {**m, "trimmed": True,
                       "content": content[:600] + f"\n…（较早的工具输出，已省略 {dropped} 字；需要时重新读取）…\n" + content[-300:]}
    return cut


class CompactionMixin:
    # ------------------------------------------------------------------ API agents

    def _context_limit(self) -> int:
        return int(self._defaults().get("context_tokens", 100_000))

    def _maybe_compact(self, st: AgentState, force: bool = False) -> bool:
        limit = self._context_limit()
        if not force and (estimate(st.messages) < limit * 0.7 or len(st.messages) < 10):
            return False
        trimmed = trim_tool_output(st.messages)
        if trimmed:
            self._archive(st, [m for _, m in trimmed])
            self._rewrite_messages(st)
            if not force and estimate(st.messages) < limit * 0.6:
                self._emit(st.project_id, "agent.compacted", {"agent": st.rec["id"], "name": st.rec["name"],
                                                              "trimmed": len(trimmed), "removed": 0})
                return True
        # Cut at the start of a step (an assistant message), so tool calls stay with their results.
        cut = len(st.messages) - 6
        while cut > 0 and st.messages[cut]["role"] != "assistant":
            cut -= 1
        if cut <= 1:
            return bool(trimmed)
        old = st.messages[:cut]
        transcript = "\n".join(f"[{m['role']}] {m.get('content') or ''} "
                               f"{json.dumps(m.get('tool_calls'), ensure_ascii=False) if m.get('tool_calls') else ''}"[:3000]
                               for m in old)[-120_000:]
        try:
            provider = self.provider_factory(st.rec["model"])
            summary = provider.chat(SUMMARY_PROMPT, [{"role": "user", "content": transcript}], []).text.strip()
            merged = self._merge_summary(provider, st.rec.get("summary") or "", summary)
        except ProviderError:
            return bool(trimmed)
        if not summary:
            return bool(trimmed)
        self._archive(st, old)
        st.rec["summary"] = merged
        st.messages = [{"role": "user", "content": "【系统通知】较早的工作记录已压缩为摘要（见系统提示中的“之前工作的摘要”），请在此基础上继续。",
                        "ts": time.time()}] + st.messages[cut:]
        self._rewrite_messages(st)
        self._save(st)
        self._emit(st.project_id, "agent.compacted", {"agent": st.rec["id"], "name": st.rec["name"], "removed": len(old),
                                                      "trimmed": len(trimmed)})
        return True

    @staticmethod
    def _merge_summary(provider, previous: str, new: str) -> str:
        combined = (previous + "\n" + new).strip() if previous else new
        if len(combined) <= SUMMARY_CHARS:
            return combined
        try:
            merged = provider.chat(MERGE_PROMPT, [{"role": "user", "content": f"【较早的摘要】\n{previous}\n\n【最近的摘要】\n{new}"}], []).text.strip()
        except ProviderError:
            merged = ""
        return merged[:SUMMARY_CHARS * 2] if merged else combined[-SUMMARY_CHARS * 2:]

    def _archive(self, st: AgentState, messages: list[dict]) -> None:
        if not messages:
            return
        path = self._dir(st.project_id, st.rec["id"]) / "messages.archive.jsonl"
        with st.io_lock, path.open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)

    # ------------------------------------------------------------------ CLI agents

    def _cli_needs_fresh_session(self, st: AgentState) -> bool:
        state = st.rec.get("cli") or {}
        if not state.get("session"):
            return False
        # The CLI's own context (tool output included) is larger than what the engine records of the turns, so
        # the size its last request reported counts first.
        if int(state.get("context") or 0) > self._context_limit():
            return True
        since = st.messages[int(state.get("session_start") or 0):]
        return estimate(since) > 2 * self._context_limit()

    def _cli_compact(self, st: AgentState, runner, server_for) -> bool:
        """Ask the CLI for a summary of its session, then let the next turn start a new session with it."""
        state = st.rec.setdefault("cli", {"session": None, "sent": 0, "kind": runner.kind})
        if not state.get("session"):
            return False
        try:
            out = runner.run(self._root(st.project_id), CLI_SUMMARY_PROMPT, self._system(st, full=True), state["session"],
                             server_for(), lambda kind, data: None, lambda: st.stop)
        except ProviderError:
            return False
        summary = (out.text or "").strip()
        if len(summary) < 40:
            return False
        usage = st.rec["usage"]
        usage["input_tokens"] = usage.get("input_tokens", 0) + out.input_tokens
        usage["output_tokens"] = usage.get("output_tokens", 0) + out.output_tokens
        usage["cached_tokens"] = usage.get("cached_tokens", 0) + out.cached_tokens
        if out.cost_usd is not None:
            usage["cost_usd"] = round(usage.get("cost_usd", 0) + out.cost_usd, 6)
        # The CLI wrote this with its whole session in view (earlier summaries included), so it replaces them.
        st.rec["summary"] = summary[:SUMMARY_CHARS * 2]
        state.update(session=None, session_start=len(st.messages), sent=len(st.messages), context=0)
        self._save(st)
        self._emit(st.project_id, "agent.compacted", {"agent": st.rec["id"], "name": st.rec["name"], "removed": 0,
                                                      "new_session": True})
        return True

    # ------------------------------------------------------------------ by hand

    def compact(self, pid: str, aid: str) -> dict:
        """The user compacts an agent's conversation now (a busy agent does it before its next step)."""
        st = self._get(pid, aid)
        if st.rec["status"] in ("running", "waiting_approval", "waiting_children", "waiting_parent", "waiting_user", "queued"):
            st.rec["compact_requested"] = True
            self._save(st)
            return {"agent": aid, "status": "scheduled", "note": "它正在工作，会在下一步开始前压缩"}
        runner = self.provider_factory(st.rec["model"])
        from . import cli
        if isinstance(runner, cli.CLIRunner):
            if not self.bridge_url:
                raise AgentError("引擎没有提供 MCP 桥接地址")
            import secrets

            def server():
                st.bridge_token = secrets.token_urlsafe(24)
                return cli.bridge_server(self.bridge_url, pid, aid, st.bridge_token)
            try:
                done = self._cli_compact(st, runner, server)
            finally:
                st.bridge_token = None
        else:
            done = self._maybe_compact(st, force=True)
        if not done:
            raise AgentError("对话还很短，或者压缩没有成功（模型没有给出摘要）")
        return {"agent": aid, "status": "compacted", "summary": (st.rec.get("summary") or "")[:2000]}
