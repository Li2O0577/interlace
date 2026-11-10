"""Each agent's work loop: API turns, CLI (Claude Code / Codex) turns, budgets and commits."""

from __future__ import annotations

import secrets
import time
from typing import Callable

from ..core.git import GitRepo
from . import cli
from .providers import ProviderError
from .state import (WAKING_NOTICES, AgentError, AgentState, spent_tokens)


class LoopMixin:
    # =================================================================== the loop

    def _loop(self, st: AgentState) -> None:
        while True:
            action = None
            with st.cv:
                while action is None:
                    if st.stop or st.rec["status"] == "queued":
                        return
                    if st.rec["status"] == "paused":
                        st.cv.wait()
                    elif st.rec["status"] == "running" and (st.pending_turn or self._actionable(st)):
                        action = "work"
                    elif st.rec["status"] == "running":
                        report = st.rec.get("report")
                        self._set_status(st, ("review" if report.get("review") == "pending" else "done") if report else "idle")
                        action = "idle"
                    elif self._actionable(st) and st.rec["status"] in ("idle", "done", "review"):
                        self._set_status(st, "running")
                        action = "work"
                    else:
                        st.cv.wait()
                items, st.inbox = (st.inbox, []) if action == "work" else ([], st.inbox)
                if items:
                    self._save_inbox(st)
            if action == "idle":
                self._after_idle(st)  # commit outside the agent lock
                continue
            if items:
                if any(i["channel"] in ("human", "parent", "peer") or st.rec.get('graph') and (i['channel']=='child' or i.get('kind')=='graph.block_progress') for i in items) and st.rec.get("report"):
                    st.rec["report"] = None  # new instructions after a report: back to work
                self._append_message(st, self._inbox_message(st, items))
            st.pending_turn = True
            st.working = True
            try:
                self._turn(st)
            except Exception as exc:  # noqa: BLE001 — never let an agent thread die silently
                st.pending_turn = False
                if not st.stop:
                    st.rec["error"] = f"{type(exc).__name__}: {exc}"
                    self._set_status(st, "failed", reason=st.rec["error"])
                    self._notify_parent(st, f"「{st.rec['name']}」出错停止：{st.rec['error']}")
            finally:
                st.working = False

    @staticmethod
    def _actionable(st: AgentState) -> bool:
        """System notices wait for the next real message (except a finished experiment); anything else needs attention."""
        return any(i["channel"] != "system" or i.get("kind") in WAKING_NOTICES for i in st.inbox)

    def _after_idle(self, st: AgentState) -> None:
        self._commit(st)
        self._schedule(st.project_id)
        if (self._root(st.project_id)/'state/graph.json').exists():
            self.graph(st.project_id).tick()

    def _wait_if_paused(self, st: AgentState) -> bool:
        """Block while paused. Returns False if the agent was stopped."""
        with st.cv:
            while st.rec["status"] == "paused" and not st.stop:
                st.cv.wait()
            return not st.stop

    def _turn(self, st: AgentState) -> None:
        if st.rec['role']=='lead' and (self._root(st.project_id)/'state/graph.json').exists():
            self.graph(st.project_id).prepare_lead(st)
        runner = self.provider_factory(st.rec["model"])
        if isinstance(runner, cli.CLIRunner):
            return self._cli_turn(st, runner)
        while st.pending_turn:
            if not self._wait_if_paused(st):
                return
            if not self._check_budget(st):
                return
            self._maybe_compact(st, force=bool(st.rec.pop("compact_requested", False)))
            provider = self.provider_factory(st.rec["model"])
            provider.thinking, provider.thinking_budgets = self.thinking_of(st), self._thinking_budgets()
            system = self._system(st)
            messages, has_images = self._with_images(st)
            try:
                try:
                    result = provider.chat(system, messages, self._tool_specs(st), on_text=self._streamer(st),
                                           max_tokens=int(self._defaults().get("max_output_tokens", 16_000)))
                except ProviderError as exc:
                    if not has_images:
                        raise
                    # The model may not take pictures: go on without them and say so once.
                    st.rec["no_images"] = True
                    self._emit(st.project_id, "system.notice", {"text": f"「{st.rec['name']}」的模型没能接收图片（{str(exc)[:160]}），"
                                                                "这次先不带图片继续；要让它看图，请换一个支持图片输入的模型。"})
                    messages, _ = self._with_images(st)
                    result = provider.chat(system, messages, self._tool_specs(st), on_text=self._streamer(st),
                                           max_tokens=int(self._defaults().get("max_output_tokens", 16_000)))
            except ProviderError as exc:
                st.rec["error"] = str(exc)
                st.rec["hold"] = {"by": "system", "reason": f"模型调用失败：{exc}"}
                self._set_status(st, "paused", reason=f"模型调用失败：{exc}")
                self._notify_parent(st, f"「{st.rec['name']}」调用模型失败，已暂停：{exc}")
                continue
            st.stop_reason = result.stop_reason
            usage = st.rec["usage"]
            usage["steps"] += 1
            usage["input_tokens"] += result.input_tokens
            usage["output_tokens"] += result.output_tokens
            usage["cached_tokens"] = usage.get("cached_tokens", 0) + result.cached_tokens
            self._append_message(st, {"role": "assistant", "content": result.text, "tool_calls": result.tool_calls,
                                      **({"thinking_blocks": result.thinking_blocks} if result.thinking_blocks else {}),
                                      **({"reasoning": result.reasoning[:20000]} if result.reasoning else {})})
            self._save(st)
            if result.text.strip():
                self._emit_message(st, result.text, result.reasoning)
            if not result.tool_calls:
                st.pending_turn = False
                if st.rec["role"] != "lead" and not st.rec.get("report"):
                    if not st.nudged:
                        st.nudged = True
                        st.pending_turn = True
                        instruction=('先用 graph.save_state 保存进度，再用 graph.submit 正式交付；受阻时沿 graph.send 反馈问题。'
                                     if st.rec.get('graph') else '请用 agent.report 向上级汇报结果后再结束。')
                        self._append_message(st, {"role": "user", "content": '【系统通知】'+instruction})
                        continue
                    self._submit_report(st, {"summary": result.text or "（未提交汇报）", "status": "partial"})
                return
            for i, call in enumerate(result.tool_calls):
                if st.stop:
                    if self.closing:
                        return  # left unanswered on purpose: _repair marks them interrupted after the restart
                    for rest in result.tool_calls[i:]:
                        self._append_message(st, {"role": "tool", "tool_call_id": rest["id"], "name": rest["name"],
                                                  "content": "已取消：Agent 被停止", "error": True})
                    return
                content, error = self._execute(st, call)
                self._append_message(st, {"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                          "content": content, **({"error": True} if error else {})})
            with st.cv:  # messages that arrived meanwhile (user first) are seen at the next step
                arrived, st.inbox = st.inbox, []
                if arrived:
                    self._save_inbox(st)
            if arrived:
                self._append_message(st, self._inbox_message(st, arrived))
            if st.rec.get("report"):
                st.pending_turn = False
                self._commit(st)
                return

    # =================================================================== CLI agents (Claude Code / Codex)

    def _cli_turn(self, st: AgentState, runner: "cli.CLIRunner") -> None:
        """One CLI process per turn; the CLI keeps its own conversation (resumed by session id),
        and acts only through the workbench tools (MCP bridge → _execute)."""
        pid = st.project_id
        state = st.rec.setdefault("cli", {"session": None, "sent": 0, "kind": runner.kind})
        while st.pending_turn:
            if not self._wait_if_paused(st) or not self._check_budget(st):
                return
            if not self.bridge_url:
                raise ProviderError("工作台没有提供 MCP 桥接地址，CLI Agent 无法使用工具")
            if st.rec.pop("compact_requested", False) or self._cli_needs_fresh_session(st):
                def server_for():
                    st.bridge_token = secrets.token_urlsafe(24)
                    return cli.bridge_server(self.bridge_url, pid, st.rec["id"], st.bridge_token)
                try:
                    self._cli_compact(st, runner, server_for)
                finally:
                    st.bridge_token = None
            fresh = [m["content"] for m in st.messages[state["sent"]:] if m["role"] == "user"]
            # Claude Code drifts into English over a long session: every turn ends with the language rule.
            prompt = ("\n\n".join(fresh) or "继续。") + "\n\n（用简体中文写所有文字，包括调用工具前的说明。）"
            state["sent"] = len(st.messages)
            system = self._system(st, full=True) + "\n\n" + cli.CLI_NOTE
            st.bridge_token = secrets.token_urlsafe(24)
            server = cli.bridge_server(self.bridge_url, pid, st.rec["id"], st.bridge_token)

            usage = st.rec["usage"]
            base = {k: usage.get(k, 0) for k in ("input_tokens", "output_tokens", "cached_tokens")}
            runner.thinking = self.thinking_of(st)

            def emit(kind: str, data: dict) -> None:
                if kind == "assistant":
                    if not data["tool_calls"] and cli.usage_limited(cli.CLIOutcome(text=data["text"])):
                        return                           # the account's usage limit, not something the agent said
                    self._append_message(st, {"role": "assistant", "content": data["text"], "tool_calls": data["tool_calls"]})
                    text = data["text"].strip()
                    if text and data["tool_calls"]:
                        st.rec["activity"] = {"kind": "say", "text": text[:160], "ts": time.time()}  # narration, not a reply
                    elif text:
                        self._emit_message(st, data["text"])
                elif kind == "usage":                    # live usage mid-turn; the final figures come with the result
                    for k in base:
                        usage[k] = base[k] + data.get(k, 0)
                    self._save(st)
                elif kind == "tool_result":
                    self._append_message(st, {"role": "tool", "tool_call_id": data["id"], "name": "",
                                              "content": data["content"][:4000], **({"error": True} if data["error"] else {})})
            resumed, before = bool(state.get("session")), len(st.messages)
            folder = self._dir(pid, st.rec["id"])
            runner.restore_session(self._root(pid), state.get("session"), folder)
            try:
                out = runner.run(self._root(pid), prompt, system, state.get("session"), server, emit, lambda: st.stop)
            except ProviderError as exc:
                st.rec["error"] = str(exc)
                st.rec["hold"] = {"by": "system", "reason": f"{runner.kind} 运行失败：{exc}"}
                self._set_status(st, "paused", reason=f"{runner.kind} 运行失败：{exc}")
                self._notify_parent(st, f"「{st.rec['name']}」运行失败，已暂停：{exc}")
                continue
            finally:
                st.bridge_token = None
                with st.cv:   # an agent.wait still open when the CLI ended its turn no longer holds the agent
                    if st.rec["status"] == "waiting_children":
                        self._set_status(st, "running")
                    st.cv.notify_all()
            state["session"] = out.session or state.get("session")
            state["sent"] = len(st.messages)
            runner.save_session(self._root(pid), state["session"], folder)
            if out.live:   # what the CLI's last request carried: the real size of its conversation
                state["context"] = list(out.live.values())[-1]["input_tokens"]
            usage["steps"] += max(1, out.turns)
            if out.input_tokens or out.output_tokens or not out.live:
                usage["input_tokens"] = base["input_tokens"] + out.input_tokens
                usage["output_tokens"] = base["output_tokens"] + out.output_tokens
                usage["cached_tokens"] = base["cached_tokens"] + out.cached_tokens
            if out.cost_usd is not None:
                usage["cost_usd"] = round(usage.get("cost_usd", 0) + out.cost_usd, 6)
            self._save(st)
            if st.stop:
                return
            if out.error and not out.text and resumed and len(st.messages) == before:
                # The CLI could not resume its conversation (its session files are gone, e.g. on another machine or
                # after a reinstall): start a new one with what the workbench recorded, instead of ending the turn.
                state["session"], state["sent"] = None, 0
                self._append_message(st, {"role": "user", "content": "【系统通知】之前的会话无法续接，对话细节丢失了。"
                                          "先看你已经写下的文件和上面的任务，从那里接着做。"})
                if not st.rec.get("session_lost"):
                    st.rec["session_lost"] = True
                    continue
            elif st.rec.pop("session_lost", None):
                # The new conversation worked: a later loss (say, after the next machine restart) restarts again.
                self._save(st)
            limited = cli.usage_limited(out)
            if limited:
                # Not an answer: the account is out of quota. Hold the agent; the watchdog resumes it once the limit resets.
                until = cli.limit_reset(limited, time.time())
                when = time.strftime("%H:%M", time.localtime(until))
                reason = f"模型用量到了上限，已暂停，{when} 左右自动继续：{limited}"
                st.rec["prev_status"] = "running"
                st.rec["hold"] = {"by": "system", "reason": reason, "until": until, "limit": True}
                self._set_status(st, "paused", reason=reason)
                self._notify_parent(st, f"「{st.rec['name']}」{reason}")
                continue
            with st.cv:
                arrived, st.inbox = st.inbox, []
                if arrived:
                    self._save_inbox(st)
            if arrived:
                self._append_message(st, self._inbox_message(st, arrived))
                continue
            if st.rec.get("report"):
                st.pending_turn = False
                self._commit(st)
                return
            st.pending_turn = False
            if st.rec["role"] != "lead" and not st.nudged:
                st.nudged = True
                st.pending_turn = True
                self._append_message(st, {"role": "user", "content": "【系统通知】请用 agent.report 向上级汇报结果后再结束。"})
            elif st.rec["role"] != "lead":
                self._submit_report(st, {"summary": out.text or "（未提交汇报）", "status": "partial"})

    def bridge_tools(self, pid: str, aid: str, token: str) -> list[dict]:
        return self._tool_specs(self._bridge_state(pid, aid, token), full=True)

    def bridge_call(self, pid: str, aid: str, token: str, name: str, args: dict) -> dict:
        st = self._bridge_state(pid, aid, token)
        if st.stop:
            return {"content": self._interrupted(), "error": True}
        budget, spent = st.rec["budget"], spent_tokens(st.rec["usage"])
        if spent >= budget["max_tokens"] and st.rec["role"] != "lead" and not self._wrap_up_call(st, name, args or {}):
            return {"content": "预算已用完：立即用 agent.report 汇报已完成的部分和未完成的事项（status 写 partial）。"
                               + ("这一问的解答概括还可以写。" if st.rec.get("question") else ""), "error": True}
        content, error = self._execute(st, {"id": f"cli-{secrets.token_hex(3)}", "name": name, "args": args or {}})
        return {"content": content, "error": error}

    def _wrap_up_call(self,st,name,args):
        return self.hooks.wrap_up_call(self,st,name,args)


    def _bridge_state(self, pid: str, aid: str, token: str) -> AgentState:
        st = self._get(pid, aid)
        expected = getattr(st, "bridge_token", None)
        if not expected or not token or not secrets.compare_digest(expected, token):
            raise AgentError("桥接凭证无效或已过期")
        return st

    def _streamer(self, st: AgentState) -> Callable[[str], None] | None:
        """Relay streamed text to open browsers in small batches (not stored; the full message is)."""
        if not self._defaults().get("stream", True):
            return None
        events = self.events_for(st.project_id)
        state = {"buf": "", "at": 0.0}
        base = {"agent": st.rec["id"], "name": st.rec["name"], "to_user": not st.rec.get("parent")}

        def on_text(chunk: str) -> None:
            state["buf"] += chunk
            if time.time() - state["at"] >= 0.15:
                events.broadcast("agent.delta", {**base, "text": state["buf"]})
                state["buf"], state["at"] = "", time.time()
        return on_text

    def _interrupted(self) -> str:
        return "已中断：工作台正在关闭" if self.closing else "已取消：Agent 被停止"

    def _check_budget(self, st: AgentState) -> bool:
        budget, usage = st.rec["budget"], st.rec["usage"]
        spent = spent_tokens(usage)
        over_tokens = spent >= budget["max_tokens"]
        over_steps = budget.get("max_steps") and usage["steps"] >= budget["max_steps"]
        wraps_up = st.rec["role"] != "lead" and not st.rec.get("report")
        # About to run out (less than ~2 more calls of the current size): ask for a report while there is room.
        per_step = spent / max(1, usage["steps"])
        low = budget["max_tokens"] - spent < 2 * per_step or (budget.get("max_steps") and usage["steps"] >= budget["max_steps"] - 2)
        if wraps_up and low and not st.rec.get("budget_warned"):
            st.rec["budget_warned"] = True
            self._append_message(st, {"role": "user", "content": "【系统通知】你的预算快用完了。现在就用 agent.report 汇报："
                                      "已完成的部分（含产出文件路径）、关键发现、还没做完的事。不要再开始新的工作。"})
        if not (over_tokens or over_steps):
            return True
        if wraps_up and not st.rec.get("budget_grace"):
            # One last step to hand in what it has, instead of stopping silently.
            st.rec["budget_grace"] = True
            self._append_message(st, {"role": "user", "content": "【系统通知】预算已用完。这是最后一步：立即用 agent.report 汇报已完成的部分和未完成的事项。"})
            return True
        reason = "token 预算已用完" if over_tokens else f"已达到最大步数 {budget['max_steps']}"
        st.rec["hold"] = {"by": "system", "reason": reason}
        self._set_status(st, "paused", reason=reason)
        self._notify_parent(st, f"「{st.rec['name']}」{reason}，已暂停（它交来的汇报会单独送到）。你可以请用户追加预算后恢复它，"
                                "自己接手收尾，或另派下级接着做。")
        return self._wait_if_paused(st)

    def _notify_parent(self, st: AgentState, text: str) -> None:
        """A child stopped on its own (budget, step limit, a failure): the parent wakes to decide what to do."""
        parent_id = st.rec.get("parent")
        if parent_id:
            self._deliver(self._get(st.project_id, parent_id),
                          {"from": "system", "channel": "system", "kind": "child_stopped", "text": text})

    def _commit(self, st: AgentState) -> None:
        try:
            with self.locks[st.project_id]:
                commit = GitRepo(self._root(st.project_id)).commit_all(
                    f"{st.rec['name']}（{st.rec['id']}）的工作", actor=st.rec["id"])
            if commit:
                self._emit(st.project_id, "git.changed", {"action": "commit", "commit": commit,
                                                          "message": f"{st.rec['name']} 的工作"})
        except Exception:  # noqa: BLE001 — a failed commit must not kill the agent
            pass
