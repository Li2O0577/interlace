"""Tool calls inside a turn: tool lists, execution, waits, questions, reports and events."""

from __future__ import annotations

from .kinds import KindLookup
import copy

import time
from typing import Any

from .. import skills
from ..graph.boundary import Boundary, for_agent as graph_boundary
from ..tools import registry
from . import roles
from .state import (TERMINAL, Approval,
                    AgentError, AgentState, _report_for_parent, _short, _to_text)
from .specs import AGENT_TOOLS

CLI_WAIT = 90   # seconds a CLI agent's agent.wait may hold its tool call (CLIs give up on long MCP calls)


class ToolsMixin(KindLookup):
    # =================================================================== tools

    def _agent_tool_names(self, st: AgentState) -> list[str]:
        if self.kind(st).decides_files:
            return []
        if st.rec.get('graph'):
            return ['agent.ask','agent.message','tools.load']
        if st.rec.get('graph_helper'):
            return ['agent.report','agent.ask','tools.load']
        depth_ok = st.rec["depth"] < int(self._defaults().get("max_agent_depth", 3))
        names = []
        for name in AGENT_TOOLS:
            if (self._root(st.project_id)/'state/graph.json').exists() and (name=='agent.spawn' or name.startswith('brainstorm.')):
                continue
            if name.startswith("brainstorm.") and not self.hooks.special_tools:
                continue
            if name == "agent.report" and not self.kind(st).reports():
                continue
            if (name == "agent.spawn" or name.startswith("brainstorm.")) and not depth_ok:
                continue
            names.append(name)
        return names

    def _loaded(self, st: AgentState) -> tuple[list[str], list[str]]:
        """(tools whose full specs go to the model, permitted tools only listed by name)."""
        allowed = [n for n in self._expand(st.rec["tools"]) if n in self.tools and self._available(n)]
        controls_graph = self.kind(st).controller and (self._root(st.project_id)/'state/graph.json').exists()
        if controls_graph:
            allowed=[n for n in allowed if n not in ('graph.confirm','graph.approve','access.decide')]
        core = set(self.hooks.core_tools(self,st,allowed))
        if controls_graph:
            core.update(n for n in allowed if n.startswith('graph.') or n=='access.revoke')
        if st.rec.get('graph'):
            core.update({'graph.save_state','graph.submit','graph.send','graph.request_helpers','access.request'})
            core.update(n for n in allowed if n in self.contracts)
            if self.kind(st).coordinates:
                core.update({'graph.block_status','graph.request_review'})
        core |= set(st.rec.get("loaded") or [])
        return [n for n in allowed if n in core], [n for n in allowed if n not in core]

    def _available(self, name: str) -> bool:
        """Tools that need a service the user has not set up are not offered (each call would only fail)."""
        if not self.hooks.tool_enabled(name) and name not in self.contracts:
            return False
        if name == "web.search":
            return bool((self.settings.raw().get("research") or {}).get("provider"))
        return True

    def _tool_specs(self, st: AgentState, full: bool = False) -> list[dict]:
        loaded, rest = self._loaded(st)
        if full:  # CLI agents list their tools once per process through MCP: give them all
            loaded = loaded + rest
        specs = [self.tools[n].spec() for n in loaded]
        specs += [self.hooks.agent_spec(n,self._agent_spec(n)) for n in self._agent_tool_names(st)]
        return [{"name": s["name"], "description": s["description"], "parameters": s["parameters"]} for s in specs]

    def _agent_spec(self, name: str) -> dict:
        """The schema of a collaboration tool; agent.spawn offers the roles this engine can spawn."""
        spec = AGENT_TOOLS[name]
        if name != "agent.spawn":
            return spec
        spec = copy.deepcopy(spec)
        fields = spec["parameters"]["properties"]
        fields["role"] = {**fields["role"], "enum": self.kinds.spawnable(), "description": self.kinds.guide or fields["role"].get("description", "")}
        return spec

    def _system(self, st: AgentState, full: bool = False) -> "roles.SystemPrompt":
        pid = st.project_id
        loaded, unloaded = self._loaded(st)
        if full:
            loaded, unloaded = loaded + unloaded, []
        return self.hooks.prompt_sections(self,st,
                                   allowed=loaded + unloaded + self._agent_tool_names(st), unloaded=unloaded,
                                   skills=self._skills_prompt(st))

    def _skills_prompt(self, st: AgentState) -> str:
        if not self._available("skill.load") or "skill.load" not in self._expand(st.rec["tools"]):
            return ""
        try:
            return skills.prompt_section(self.skills, self._root(st.project_id), st.rec, self.kinds.title, self.kind(st))
        except Exception:  # noqa: BLE001 — a broken skill file must not stop the agent
            return ""

    def _load_tools(self, st: AgentState, names: list) -> dict:
        allowed = {n for n in self._expand(st.rec["tools"]) if self._available(n)}
        wanted = self._expand([str(n) for n in names or []])
        ok = [n for n in wanted if n in allowed and n in self.tools]
        denied = [str(n) for n in names or [] if not self._expand([str(n)]) or not set(self._expand([str(n)])) & allowed]
        st.rec["loaded"] = sorted(set(st.rec.get("loaded") or []) | set(ok))
        self._save(st)
        return {"loaded": ok, **({"not_allowed": denied} if denied else {}),
                "note": "说明已加载，从下一步起可以直接调用。" if ok else "没有可加载的工具（名称不对或没有权限）。"}

    def _execute(self, st: AgentState, call: dict) -> tuple[str, bool]:
        name, args = call["name"], call.get("args") or {}
        pid, aid = st.project_id, st.rec["id"]
        st.rec["activity"] = {"kind": "tool", "tool": name, "args": _short(args, 160), "ts": time.time()}
        if "_raw" in args:  # the arguments were not valid JSON
            cut = getattr(st, "stop_reason", "") in ("length", "max_tokens")
            self._emit(pid, "agent.tool", {"agent": aid, "name": st.rec["name"], "tool": name, "args": {},
                                           "error": "参数不是完整的 JSON" + ("（输出超过长度上限被截断）" if cut else "")})
            if cut:
                return ("错误：这次输出超过了长度上限，工具参数被截断。请把长内容分成几段："
                        "先用 fs.write 写入前一部分，再用 fs.edit 在末尾追加后续部分（每段控制在几千字以内）。"), True
            return "错误：工具参数不是合法的 JSON，没有执行。请检查引号和转义后重新调用。", True
        try:
            if name in AGENT_TOOLS:
                result = self._agent_tool(st, name, args)
            else:
                if name not in self._expand(st.rec["tools"]) or not self._available(name):
                    raise AgentError(f"你没有使用 {name} 的权限（可以请上级或用户授权，或交给有权限的 Agent）")
                if name in self.tools and name not in self._loaded(st)[0]:
                    self._load_tools(st, [name])  # called straight from the catalog: keep its spec from now on
                self._formulation_gate(st, name)
                scoped = self._scope_gate(st, name, args)
                if scoped is not None:
                    args = scoped[0]
                if self.confirms_later(pid, st, name, args):
                    # A key stage finished: the user confirms afterwards; the agent does not sit and wait for it.
                    result = self.run_tool(pid, name, args, aid)
                    self._request_approval(st, name, args, kind="confirm")
                    if isinstance(result, dict):
                        result = {**result, "confirmation": "已标记。用户会在界面上确认；不必等待，直接进入下一阶段。"
                                  "用户打回时你会收到通知，那时再回来补做。"}
                    self._emit(pid, "agent.tool", {"agent": aid, "name": st.rec["name"], "tool": name, "ok": True,
                                                   "args": _short(args)})
                    return _to_text(result), False
                if self.needs_approval(pid, st, name, args):
                    if self._await_approval(st, self._request_approval(st, name, args)) is None:
                        return self._interrupted(), True
                result = self.run_tool(pid, name, args, aid)
                if scoped is not None and not isinstance(scoped[2], Boundary):
                    result = self.hooks.filter_result(name,result,scoped)
            self._emit(pid, "agent.tool", {"agent": aid, "name": st.rec["name"], "tool": name, "ok": True,
                                           "args": _short(args)})
            return _to_text(result), False
        except Exception as exc:  # noqa: BLE001 — tool errors go back to the model
            self._emit(pid, "agent.tool", {"agent": aid, "name": st.rec["name"], "tool": name, "ok": False,
                                           "args": _short(args), "error": str(exc)[:300]})
            return f"错误：{exc}", True

    def _await_approval(self, st: AgentState, approval) -> "Approval | None":
        """Block until the user decides. None if the agent was stopped meanwhile; raises if denied."""
        self._set_status(st, "waiting_approval")
        while not approval.event.wait(1.0):
            if st.stop:
                return None
        if st.stop:
            return None
        if st.rec["status"] == "waiting_approval":
            self._set_status(st, "running")
        if not self._wait_if_paused(st):
            return None
        if approval.status != "approved":
            why = f"（理由：{approval.reason}）" if approval.reason else ""
            raise AgentError(f"用户拒绝了这次 {approval.tool} 操作{why}。不要换方式绕过，请调整方案或向上级说明。")
        return approval

    def _agent_tool(self, st: AgentState, name: str, args: dict) -> Any:
        if self.kind(st).decides_files:
            raise AgentError('文件处理模型只能只读查找材料并输出 access.decide，不能协作或管理 Agent')
        if (st.rec.get('graph') and name not in ('agent.ask','agent.message','tools.load') or st.rec.get('graph_helper') and name not in ('agent.report','agent.ask','tools.load')):
            raise AgentError('图节点必须使用 graph.submit 和获批 helper 批次，不能绕过正式流程')
        if st.rec.get('graph') and name=='agent.message':
            return self.graph(st.project_id).reply_helper(st.rec['id'],args.get('to',''),args.get('text',''))
        pid, aid = st.project_id, st.rec["id"]
        if name == "agent.spawn" and self.kind(st).controller and self.team_mode(pid) == "manual":
            # Manual team mode: the user sees who the lead wants to send and may change the task or role first.
            shown = {k: args[k] for k in ("role", "name", "task", "expect", "skills", "question") if args.get(k)}
            approval = self._await_approval(st, self._request_approval(st, name, shown))
            if approval is None:
                return self._interrupted()
            if approval.edits:
                if "role" in approval.edits and "name" not in approval.edits:
                    args = {k: v for k, v in args.items() if k != "name"}  # the old name described the old role
                args = {**args, **approval.edits}
                note = "、".join({"role": "角色", "task": "任务", "expect": "交回要求", "name": "名字", "skills": "技能"}[k]
                                for k in approval.edits)
                self._emit(pid, "agent.spawn_edited", {"agent": aid, "fields": sorted(approval.edits)})
                edited = f"用户修改了{note}后批准，按修改后的安排执行"
            else:
                edited = ""
        else:
            edited = ""
        if name == "agent.spawn":
            task = args.get("task", "")
            child = self.spawn(pid, aid, args.get("role", ""), task, args.get("name", ""),
                               tools=args.get("tools"), max_tokens=args.get("budget_tokens") or args.get("max_tokens"),
                               skills=args.get("skills") if isinstance(args.get("skills"), list) else None,
                               expect=args.get("expect", ""), question=args.get("question", ""))
            out = {"agent": child["id"], "name": child["name"], "status": child["status"], "tools": len(child["tools"])}
            if child.get("skills"):
                out["skills"] = child["skills"]
            if child.get("skills_dropped"):
                out["skills_not_found"] = child["skills_dropped"]
                out["skills_note"] = "这些技能不存在或在本项目已关闭，没有带上；可用的技能见 skill.list"
            if edited:
                out.update({"note": edited, "role": child["role"], "task": child["task"][:600]})
            return out
        if name == "agent.message":
            text = args.get("text", "")
            if args.get("expect"):
                text = f"{text}\n【用户要求的交付形式】{args['expect']}"
            self.send(pid, aid, args.get("to", ""), text)
            return {"sent": True}
        if name == "agent.list":
            return [{"agent": s.rec["id"], "name": s.rec["name"], "role": s.rec["role"], "status": s.rec["status"],
                     "task": s.rec["task"][:200], "report": (s.rec.get("report") or {}).get("summary"),
                     "parent": s.rec["parent"]} for s in self.descendants(pid, aid)]
        if name == "agent.control":
            v = self.control(pid, aid, args.get("agent", ""), args.get("action", ""), args.get("reason", ""))
            return {"agent": v["id"], "status": v["status"]}
        if name == "agent.wait":
            return self._wait_children(st, args.get("agents"), int(args.get("timeout") or 600))
        if name == "agent.ask":
            return self._ask(st, args.get("question", ""))
        if name.startswith('brainstorm.'):
            return self.hooks.special_tool(self,st,name,args)
        if name == "tools.load":
            return self._load_tools(st, args.get("names") or [])
        if name == "agent.report":
            if not self.kind(st).reports():
                raise AgentError("主控 Agent 直接在对话中向用户汇报")
            args=self.hooks.before_report(self,st,args)
            self._submit_report(st, args)
            return {"reported": True}
        raise AgentError(f"未知工具：{name}")

    def _arranged(self,pid,slot,role,fallback):
        return self.hooks.arranged(self,pid,slot,role,fallback)




    def _formulation_gate(self,st,name):
        if graph_boundary(self,st) is not None:
            return
        return self.hooks.before_tool(self,st,name)


    def _scope_gate(self,st,name,args):
        boundary=graph_boundary(self,st)
        if boundary is not None:
            return boundary.check_call(name,args,getattr(self,'contracts',None)),set(),boundary
        return self.hooks.scope_call(self,st,name,args)


    def _sublead_delivery(self,st,args):
        return self.hooks.delivery(self,st,args)


    def _devil_gone(self,pid,record):
        return self.hooks.devil_gone(self,pid,record)


    def _wait_children(self, st: AgentState, ids: list | None, timeout: int) -> dict:
        pid, aid = st.project_id, st.rec["id"]
        own = {c.rec["id"]: c for c in self.children(pid, aid)}
        targets = [own[i] for i in (ids or own) if i in own]
        if ids and len(targets) != len(ids):
            raise AgentError("只能等待你自己的直接下级")
        if not targets:
            return {"children": [], "note": "没有需要等待的下级"}
        # A CLI agent's tool call is a request over the MCP bridge. Its turn can end while the call is still open
        # (the CLI gave up waiting on it); from then on this call must not touch the agent's status or inbox.
        turn = st.bridge_token
        live = (lambda: st.bridge_token == turn) if turn else (lambda: True)
        if turn:
            timeout = min(timeout, CLI_WAIT)
        self._set_status(st, "waiting_children")
        self._schedule(pid)  # its slot is free while it waits
        deadline = time.time() + max(5, min(timeout, 3600))
        interrupted = False
        with st.cv:
            while time.time() < deadline and not st.stop:
                if not live():
                    return {"children": [], "note": "这一轮已经结束，等待作废"}
                if all(t.rec["status"] in TERMINAL or t.rec["status"] == "paused" for t in targets):
                    break
                if any(i["channel"] == "human" for i in st.inbox):
                    interrupted = True
                    break
                if any(i.get("kind") == "question" for i in st.inbox) or \
                        any(t.rec["status"] == "waiting_parent" for t in targets):
                    break  # a child is blocked on us: go answer it instead of waiting it out
                st.cv.wait(timeout=1.0)
            if not live():
                return {"children": [], "note": "这一轮已经结束，等待作废"}
            reports = [i for i in st.inbox if i["kind"] == "report" and i["from"] in {t.rec["id"] for t in targets}]
            st.inbox = [i for i in st.inbox if i not in reports]
            self._save_inbox(st)
            if st.rec["status"] == "waiting_children":
                self._set_status(st, "running")
        questions = [{"from": i["from"], "text": i["text"]} for i in st.inbox if i.get("kind") == "question"]
        questions += [{"from": t.rec["id"], "text": "（问题见之前收到的消息）"} for t in targets
                      if t.rec["status"] == "waiting_parent" and t.rec["id"] not in {q["from"] for q in questions}]
        in_review = [t.rec["name"] for t in targets if t.rec["status"] == "review"]
        return {"interrupted_by_user": interrupted, **({"awaiting_user_review": in_review,
                "review_note": "这些下级已经交了汇报，正在等用户审查；审查通过后汇报会自动转给你，期间可以先做别的或继续等待"}
                if in_review else {}), **({"questions_waiting": questions,
                "note": "有下级在等你答复：先用 agent.message 回答，再继续等待"} if questions else {}),
                "children": [{"agent": t.rec["id"], "name": t.rec["name"], "status": t.rec["status"],
                              "report": _report_for_parent(t.rec)} for t in targets],
                "timed_out": time.time() >= deadline,
                **({"note_cli": f"每次最多等 {CLI_WAIT} 秒。下级还没完成时，可以再调用 agent.wait，或者直接结束这一轮：下级交付、提问或停下时都会唤醒你"}
                   if turn and time.time() >= deadline else {})}

    def _ask(self, st: AgentState, question: str) -> dict:
        pid = st.project_id
        if not question.strip():
            raise AgentError("问题不能为空")
        parent_id = st.rec.get("parent")
        if parent_id:
            self._deliver(self._get(pid, parent_id), {"from": st.rec["id"], "channel": "child", "kind": "question",
                                                      "text": f"{question}\n（请用 agent.message 发给 {st.rec['id']} 答复）"})
            status, accept = "waiting_parent", ("human", "parent")
        else:
            status, accept = "waiting_user", ("human",)
        self._emit(pid, "agent.question", {"agent": st.rec["id"], "name": st.rec["name"], "question": question,
                                           "to": parent_id or "human"})
        self._set_status(st, status)
        self._schedule(pid)  # waiting for an answer frees the slot
        deadline = time.time() + 3600
        with st.cv:
            while not st.stop and time.time() < deadline:
                answers = [i for i in st.inbox if i["channel"] in accept]
                if answers and st.rec["status"] != "paused":
                    st.inbox = [i for i in st.inbox if i not in answers]
                    self._save_inbox(st)
                    break
                st.cv.wait(timeout=1.0)
            else:
                answers = []
        if st.rec["status"] == status:
            self._set_status(st, "running")
        if not answers:
            return {"answer": None, "note": "暂未得到答复，请根据已有信息自行判断，并在汇报中注明这一假设"}
        return {"answer": self._render_inbox(st, answers)}

    def _submit_report(self, st: AgentState, args: dict) -> None:
        boundary=graph_boundary(self,st)
        if st.rec.get('graph_runtime') and args.get('views'):
            raise AgentError('图实例汇报使用文件引用；尚未注册视图读取边界，不能自动解析 views')
        if boundary is not None and any(not boundary.readable(ref) for ref in args.get('files') or []):
            raise AgentError('汇报包含未获准读取的文件引用')
        self.hooks.on_report(self,st,args)
        specs = [v for v in (args.get("views") or []) if isinstance(v, dict)][:12]
        resolved = self.host.views.resolve_many(self._root(st.project_id), specs) if specs else []
        report = {"summary": str(args.get("summary", "")).strip(), "files": args.get("files") or [],
                  "findings": args.get("findings") or [], "open_questions": args.get("open_questions") or [],
                  "status": args.get("status") or "done", "ts": time.time(),
                  **({"views": specs, "view_summaries": [self.host.views.compact(v) for v in resolved]} if specs else {})}
        if self.reviews_report(st.project_id, st):
            report["review"] = "pending"
        st.rec["report"] = report
        self._save(st)
        if report.get("review") != "pending":
            self._deliver_report(st)
        self._emit(st.project_id, "agent.reported", {"agent": st.rec["id"], "name": st.rec["name"],
                                                     "parent": st.rec.get("parent"), "summary": report["summary"][:500],
                                                     "status": report["status"], "files": report["files"][:20],
                                                     "views": len(report.get("views") or []),
                                                     **({"review": "pending"} if report.get("review") == "pending" else {})})

    def _deliver_report(self, st: AgentState, user_note: str = "") -> None:
        report = st.rec["report"]
        specs = report.get("views") or []
        resolved = self.host.views.resolve_many(self._root(st.project_id), specs) if specs else []
        lines = [f"状态：{report['status']}", f"摘要：{report['summary']}"]
        if resolved:
            lines.append("附带视图（用 view.show 的 ref 参数可原样展示给用户）：\n" + "\n".join(
                f"- {st.rec['id']}#{i} {v.get('title') or ''}（{v.get('type')}）：{(v.get('error') or v.get('summary') or '')[:300]}"
                for i, v in enumerate(resolved)))
        if report["files"]:
            lines.append("产出文件：" + "、".join(map(str, report["files"])))
        if report["findings"]:
            lines.append("关键发现：\n" + "\n".join(f"- {f}" for f in report["findings"]))
        if report["open_questions"]:
            lines.append("遗留问题：\n" + "\n".join(f"- {q}" for q in report["open_questions"]))
        if report.get("review") == "accepted":
            lines.append("【用户已审查这份汇报】" + (f"用户批注：{user_note}（以用户意见为准）" if user_note else "用户认可，无补充。"))
        if st.rec.get("parent"):
            self._deliver(self._get(st.project_id, st.rec["parent"]),
                          {"from": st.rec["id"], "channel": "child", "kind": "report", "text": "\n".join(lines)})

    # =================================================================== misc

    def _emit_message(self, st: AgentState, text: str, reasoning: str = "") -> None:
        """A reply is whatever mix of text and views the agent wrote: ```view blocks become rendered views."""
        pid = st.project_id
        body, views = (text,[]) if st.rec.get('graph_runtime') else self.host.views.embed(self._root(pid), text)
        broken = [v for v in views if v.get("error")]
        if broken:  # tell the agent, quietly, so it can fix the spec next turn
            self._deliver(st, {"from": "system", "channel": "system", "kind": "notice", "text": "你回复里的视图有误："
                               + "；".join(f"{v.get('title') or '视图'}：{v['error']}" for v in broken)}, wake=False)
        block = self.host.views.block
        plain = block.sub(lambda m: "（图表）", text) if block is not None else text
        st.rec["activity"] = {"kind": "say", "text": plain.strip()[:160], "ts": time.time()}
        self._emit(pid, "agent.message", {"agent": st.rec["id"], "name": st.rec["name"], "text": body,
                                          "to_user": not st.rec.get("parent"), **({"views": views} if views else {}),
                                          **({"reasoning": reasoning[:6000]} if reasoning else {})})

    def _emit(self, pid: str, type: str, data: dict) -> None:
        source = data.get("agent") if type.startswith("agent.") and data.get("by") is None else data.get("by", "system")
        self.events_for(pid).append(type, data, source=source or "system")
