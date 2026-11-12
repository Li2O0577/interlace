"""The collaboration tools every agent gets: spawn, message, wait, ask, report, … (schemas only)."""

from __future__ import annotations

from . import kinds, roles


def _spec(name, description, params=None, required=None):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": params or {}, "required": required or []}}


AGENT_TOOLS = {
    "agent.spawn": _spec("agent.spawn", "派生一个子 Agent 并立即开始工作（不等待）。只给它任务说明和相关文件路径，不要粘贴长内容。"
                         "tools 可选，只能是你自己工具的子集；默认按角色分配。", {
                             "role": {"type": "string", "enum": kinds.DEFAULT.spawnable(), "description": roles.ROLE_GUIDE},
                             "task": {"type": "string", "description": "清楚的任务说明：目标、输入文件、背景与约束。交回什么由它自己判断"},
                             "name": {"type": "string", "description": "简短正式的称呼，写负责范围即可，例如“资料核对”“摘要”"
                                      "（界面显示为“角色·范围”）；不要用表情符号或昵称。可不填"},
                             "tools": {"type": "array", "items": {"type": "string"}},
                             "skills": {"type": "array", "items": {"type": "string"}, "description":
                                        "给它选的技能 ID（见系统提示里的技能列表），它一开始就会读到这些技能的全文；只选这项任务真正用得到的"},
                             "expect": {"type": "string", "description": "用户（操作者）明确要求的交付形式，照实转述，例如用户说“只要一张对比表”。"
                                        "用户没有要求时不填：交回什么由子 Agent 自己判断"},
                             "budget_tokens": {"type": "integer", "description":
                                 "这个子 Agent 整个任务的总 token 预算（从你的预算中划拨），不是单次回复长度。每次调用模型都会把说明和对话计入，"
                                 "一次调用通常就要 1–3 万 token。一般不填（使用默认值）；大任务可填 200000–600000，最少 60000。"}},
                         ["role", "task"]),
    "agent.message": _spec("agent.message", "给你的下级 Agent 发指令或答复它的问题；也可以给你的上级发进展更新。",
                           {"to": {"type": "string", "description": "Agent ID"}, "text": {"type": "string"},
                            "expect": {"type": "string", "description": "用户明确要求的交付形式（照实转述）；用户没有要求时不填"}}, ["to", "text"]),
    "agent.wait": _spec("agent.wait", "等待下级 Agent 完成并收取它们的汇报（默认等待自己全部未完成的下级）。用户发来消息时会提前返回。",
                        {"agents": {"type": "array", "items": {"type": "string"}},
                         "timeout": {"type": "integer", "description": "最长等待秒数，默认 600"}}),
    "agent.list": _spec("agent.list", "查看你的下级 Agent 的状态、任务和汇报摘要。"),
    "agent.control": _spec("agent.control", "暂停、恢复或停止你的下级 Agent（用户暂停的、用户亲自安排的 Agent 你无权控制）。",
                           {"agent": {"type": "string"}, "action": {"type": "string", "enum": ["pause", "resume", "stop"]},
                            "reason": {"type": "string"}}, ["agent", "action"]),
    "agent.ask": _spec("agent.ask", "向上级提问并等待答复（主控 Agent 向用户提问）。用于必须由上级或用户做的取舍。",
                       {"question": {"type": "string"}}, ["question"]),
    "tools.load": _spec("tools.load", "加载工具说明：系统提示里“按需加载的工具”只列了名字，用这个工具加载它们的完整说明后再调用。"
                        "可以写通配符，例如 git.*。", {"names": {"type": "array", "items": {"type": "string"}}}, ["names"]),
    "agent.report": _spec("agent.report", "向上级提交任务汇报并结束工作。汇报要短：摘要 + 产出文件路径 + 关键发现 + 遗留问题。"
                          "上级要的是表、图或数字时，用 views 附上展示规格（与 view.show 相同，需要几样给几样），上级可以原样转给用户。", {
        "summary": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}},
        "views": {"type": "array", "items": {"type": "object"}, "description": "附带的展示规格（表格、图、关键数字）"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["done", "partial", "blocked"]}}, ["summary"]),
}

# agent.spawn arguments the runtime reads itself; anything else a caller passes is an extra attribute for the new
# agent's record, which the application's ``Hooks.before_spawn`` keeps or drops. Limits are not shown on an
# approval card (the operator decides who does what, not the budget split).
SPAWN_ARGS = ("role", "task", "name", "tools", "skills", "expect", "budget_tokens", "max_tokens")
SPAWN_LIMITS = ("tools", "budget_tokens", "max_tokens")


def spawn_attrs(args: dict) -> dict:
    return {k: v for k, v in args.items() if k not in SPAWN_ARGS}
