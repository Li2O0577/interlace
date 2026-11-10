"""Task-neutral role defaults. A host application may register more roles (see extensions)."""
from pathlib import Path
import json

READ = ["fs.list", "fs.read", "fs.search"]
WRITE = ["fs.write", "fs.edit"]

# Graph nodes get the file and code tools of their role plus the graph tools; nothing here is a method or a pipeline.
ROLE_TOOLS: dict[str, list[str]] = {
    "file_keeper": ["fs.read", "fs.list", "fs.search", "access.decide"],
    "lead": ["*"],
    "worker": READ + WRITE + ["code.run"],
    "reviewer": READ + WRITE,
    "coder": READ + WRITE + ["code.run"],
    "analyst": READ + WRITE + ["code.run"],
    "researcher": READ + WRITE,
    "writer": READ + WRITE,
    "sublead": READ + WRITE + ["code.run"],
}

# Tools without which a role cannot do its job (a parent lacking them cannot spawn that role).
ESSENTIAL: dict[str, list[str]] = {}

ROLE_GUIDE = "按权限选角色：worker 执行、reviewer 检查（不运行代码）、coder 写代码、analyst 分析、researcher 整理资料、writer 写作；职责与验收要求由当前任务定义。"

ROLE_NAMES = {"lead": "主控", "sublead": "次主控", "worker": "执行员", "reviewer": "检查员", "coder": "程序员",
              "analyst": "分析师", "researcher": "资料员", "writer": "写作者", "file_keeper": "文件处理"}


def project_mode(root: Path) -> str:
    try:
        return json.loads((root / "project.json").read_text(encoding="utf-8")).get("mode") or "workflow"
    except (OSError, ValueError):
        return "workflow"


def tools_for(role: str) -> list[str]:
    return list(ROLE_TOOLS.get(role, READ))


def role_name(role):
    return ROLE_NAMES.get(role, role)


def clean_name(name: str) -> str:
    """Drop emoji, pictographs and decoration from an agent name; keep letters, digits and plain punctuation."""
    import re
    import unicodedata
    out = "".join(ch for ch in (name or "") if unicodedata.category(ch) not in ("So", "Sk", "Cs", "Co", "Cf", "Mn", "Me"))
    out = re.sub(r"[\s_]+", " ", out).strip(" -·:：|/")
    return out[:24]


class SystemPrompt(str):
    """The full prompt as a string, plus its stable head (``static``: role, rules, tool catalog) and the
    part that changes as the project moves (``dynamic``: project state, task). Providers that support
    prompt caching put a cache breakpoint after the stable head."""

    static: str = ""
    dynamic: str = ""


def tool_catalog(names: list[str], tools: dict | None = None) -> str:
    """One line per group: names and short titles of tools that are allowed but not loaded yet."""
    if tools is None:
        from ..tools import registry
        tools = registry.TOOLS
    groups: dict[str, list[str]] = {}
    for n in names:
        t = tools.get(n)
        if t:
            groups.setdefault(n.split(".")[0], []).append(f"{n}（{t.title}）")
    if not groups:
        return ""
    return ("## 按需加载的工具\n下面这些工具你有权限，但说明没有预先加载。需要时先用 tools.load 加载（一次可加载多个），再调用：\n"
            + "\n".join(f"- {', '.join(v)}" for v in groups.values()))
