"""Task-neutral role defaults. A host application may register more roles (see extensions)."""
from pathlib import Path
import json

from . import kinds as _kinds

READ = list(_kinds.READ)
WRITE = list(_kinds.WRITE)

# Read-only views of the process default registry (see kinds.py), for code that reads the old role tables.
ROLE_TOOLS = _kinds.View(_kinds.DEFAULT, lambda k: list(k.tools))
ESSENTIAL = _kinds.View(_kinds.DEFAULT, lambda k: list(k.essential))
ROLE_NAMES = _kinds.View(_kinds.DEFAULT, lambda k: k.title or k.role)
ROLE_GUIDE = _kinds.DEFAULT.guide


def project_mode(root: Path) -> str:
    try:
        return json.loads((root / "project.json").read_text(encoding="utf-8")).get("mode") or "workflow"
    except (OSError, ValueError):
        return "workflow"


def tools_for(role: str) -> list[str]:
    return _kinds.DEFAULT.tools(role)


def role_name(role):
    return _kinds.DEFAULT.title(role)


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
