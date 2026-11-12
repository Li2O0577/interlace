"""How a host adds its own task types, tools and roles to one Kernel.

An extension is trusted host code, registered explicitly when the Kernel is built. Nothing a model writes
(a message, a file, an MCP argument) can load one. Extensions are per Kernel: two kernels in one process
do not see each other's tools or roles.

A tool only reaches graph nodes when it declares which of its parameters are project paths it reads and
writes; the node boundary then checks those paths exactly like the built-in file tools. A tool without
such a contract stays out of graph nodes. This is a path check, not an OS sandbox: what the handler's
Python does beyond its declared parameters is the host's responsibility.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agents import kinds as agent_kinds
from .tools.registry import Tool

RESERVED = ("fs.", "code.", "graph.", "access.", "agent.", "tools.", "brainstorm.", "lineage.", "project.",
            "operation.", "events.", "artifact.", "approval.")


class ExtensionError(ValueError):
    pass


@dataclass(frozen=True)
class Contract:
    """Parameters of a tool that name project files (a string or a list of strings each)."""
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskType:
    """A plan template. ``build_plan(task)`` returns a graph plan (validated like any draft; the operator
    still confirms it). ``initialize(root)`` may write starting material into the new project folder."""
    id: str
    build_plan: Callable[[str], dict]
    initialize: Callable[[Path], None] | None = None
    title: str = ""


@dataclass
class Extension:
    name: str
    task_types: list[TaskType] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)
    contracts: dict[str, Contract] = field(default_factory=dict)   # tool name -> its path parameters
    roles: dict[str, list[str]] = field(default_factory=dict)      # role -> its default tools (becomes a Worker kind)
    kinds: list[type] = field(default_factory=list)                 # agent kinds: subclasses of kinds.Agent

    def tool(self, name: str, title: str, description: str, params: dict | None = None,
             required: list[str] | None = None, *, readonly: bool = False, dangerous: bool = False,
             reads: tuple[str, ...] = (), writes: tuple[str, ...] = (), node: bool = True):
        """Decorator: register ``handler(ctx, **params)``. ``node=False`` keeps it out of graph nodes."""
        def register(handler: Callable[..., Any]):
            self.tools.append(Tool(name, title, description, handler, params or {}, required or [], readonly, dangerous))
            if node:
                self.contracts[name] = Contract(tuple(reads), tuple(writes))
            return handler
        return register


@dataclass
class Registered:
    """What one Kernel knows after its extensions were checked."""
    tools: dict[str, Tool]
    contracts: dict[str, Contract]
    kinds: agent_kinds.AgentKinds
    task_types: dict[str, TaskType]

    @property
    def roles(self) -> dict[str, list[str]]:
        """Role -> default tools of the kinds the extensions added (for older callers)."""
        return {k.role: list(k.tools) for k in self.kinds.kinds() if k.role not in agent_kinds.DEFAULT}


def register(base_tools: dict[str, Tool], extensions, base_kinds: agent_kinds.AgentKinds | None = None) -> Registered:
    tools = dict(base_tools)
    out = Registered(tools, {}, (base_kinds or agent_kinds.DEFAULT).copy(), {})
    added: set[str] = set()
    for ext in extensions:
        if not isinstance(ext, Extension) or not str(ext.name).strip():
            raise ExtensionError("扩展必须是带名称的 Extension 对象")
        for t in ext.tools:
            if not isinstance(t, Tool) or "." not in t.name or t.name.startswith(RESERVED):
                raise ExtensionError(f"扩展 {ext.name} 的工具名 {getattr(t, 'name', t)!r} 不合法或占用了内核保留的命名空间")
            if t.name in tools:
                raise ExtensionError(f"工具 {t.name} 已经注册过")
            tools[t.name] = t
        for name, contract in ext.contracts.items():
            mine = next((t for t in ext.tools if t.name == name), None)
            if mine is None or not isinstance(contract, Contract):
                raise ExtensionError(f"扩展 {ext.name} 为不属于它的工具 {name} 声明了边界")
            unknown = (set(contract.reads) | set(contract.writes)) - set(mine.params)
            if unknown:
                raise ExtensionError(f"工具 {name} 的边界声明引用了不存在的参数：{', '.join(sorted(unknown))}")
            out.contracts[name] = contract
        new = [agent_kinds.define(role, names) if isinstance(role, str) and str(role).strip() else role
               for role, names in ext.roles.items()] + list(ext.kinds)
        for kind in new:
            role = getattr(kind, "role", kind)
            if not isinstance(role, str) or not role.strip() or role in added or out.kinds.get(role) in (agent_kinds.Lead, agent_kinds.Keeper):
                raise ExtensionError(f"角色 {role} 由内核保留或已经注册过")
            try:
                out.kinds.register(kind, replace=True)
            except agent_kinds.KindError as e:
                raise ExtensionError(str(e)) from None
            missing = [n for n in kind.tools if n != "*" and not any(t == n or n.endswith("*") and t.startswith(n[:-1]) for t in tools)]
            if missing:
                raise ExtensionError(f"角色 {role} 引用了未注册的工具：{', '.join(missing)}")
            added.add(role)
        for kind in ext.task_types:
            if not isinstance(kind, TaskType) or not str(kind.id).strip() or kind.id == "generic" or kind.id in out.task_types:
                raise ExtensionError(f"任务类型 {getattr(kind, 'id', kind)!r} 不合法、由内核保留或已经注册过")
            out.task_types[kind.id] = kind
    return out
