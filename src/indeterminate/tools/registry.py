"""Unified tool layer.

Every operation an agent or the operator performs on a project is a named
tool with a JSON-schema parameter spec. Agents and the operator call the same
tools; tools marked ``dangerous`` need the operator's approval. Each call is
recorded in the project event stream with the acting party as ``source``.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core.events import EventLog
from ..core.git import GitRepo
from ..core.settings_store import SettingsStore


class ToolError(Exception):
    pass


@dataclass
class ToolContext:
    project_id: str
    root: Path
    repo: GitRepo
    events: EventLog
    settings: SettingsStore
    actor: str = "human"
    lock: Any = field(default_factory=contextlib.nullcontext)  # project lock, for tools that manage it themselves
    agents: Any = None   # the AgentManager, for tools that call a model themselves
    host: Any = None     # HostAdapters; None = the process default

    def emit(self, type: str, data: dict) -> None:
        self.events.append(type, data, source=self.actor)

    @property
    def files(self):
        """The host's file policy (where agents may write, special file readers)."""
        from .. import host as host_module
        return (self.host or host_module.default()).files


@dataclass
class Tool:
    name: str
    title: str
    description: str
    handler: Callable[..., Any]
    params: dict = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    readonly: bool = False
    dangerous: bool = False

    def spec(self) -> dict:
        return {
            "name": self.name, "title": self.title, "description": self.description,
            "readonly": self.readonly, "dangerous": self.dangerous,
            "parameters": {"type": "object", "properties": self.params, "required": self.required},
        }


TOOLS: dict[str, Tool] = {}


def tool(name: str, title: str, description: str, params: dict | None = None, required: list[str] | None = None,
         readonly: bool = False, dangerous: bool = False):
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        TOOLS[name] = Tool(name, title, description, fn, params or {}, required or [], readonly, dangerous)
        return fn
    return register


def specs() -> list[dict]:
    return [t.spec() for t in TOOLS.values()]


_TYPES = {"string": str, "integer": int, "boolean": bool, "number": (int, float), "object": dict, "array": list}


def call(ctx: ToolContext, name: str, params: dict | None, tools: dict | None = None) -> Any:
    tool_ = (TOOLS if tools is None else tools).get(name)
    if tool_ is None:
        raise ToolError(f"未知工具: {name}")
    params = dict(params or {})
    unknown = set(params) - set(tool_.params)
    if unknown:
        accepted = "、".join(tool_.params) or "无"
        raise ToolError(f"{name} 不支持参数: {', '.join(sorted(unknown))}（可用参数：{accepted}）")
    for key in tool_.required:
        if params.get(key) is None or (params.get(key) == "" and key != "content"):   # an empty file is a file
            raise ToolError(f"{name} 缺少参数: {key}")
    for key, value in params.items():
        expected = _TYPES.get(tool_.params[key].get("type"))
        if expected and value is not None and not isinstance(value, expected):
            raise ToolError(f"参数 {key} 类型应为 {tool_.params[key]['type']}")
        allowed = tool_.params[key].get("enum")
        if allowed and value not in allowed:
            raise ToolError(f"参数 {key} 只能是: {', '.join(allowed)}")
    return tool_.handler(ctx, **params)


def load_generic():
    from . import fs_tools, graph_tools  # noqa: F401
