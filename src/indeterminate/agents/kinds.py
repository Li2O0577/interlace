"""Agent kinds: what each kind of model instance may do, as classes.

A kind declares capabilities and boundaries: its default tools, the tools it cannot work without, whether it
coordinates others, whether it is the project's controller. It says nothing about how the model thinks; the
task comes from the plan or from whoever assigned it. The runtime and the graph engine ask a kind what it may
do ("may it release a block?") and never compare role names.

    Agent
    ├─ Coordinator         spawns and follows subordinates
    │  ├─ Lead             the project's controller: releases blocks, requests plan changes, approves helpers
    │  └─ SubLead          coordinates one block or one assignment; cannot release
    ├─ Worker              does the work in its own folder, may run code
    ├─ Reviewer            checks a deliverable and writes its findings; does not run code
    └─ Keeper              out of the graph, read only: decides on extra file requests

A host adds kinds by subclassing (``class Translator(Worker)``) and registering the class on a registry. The
controller and keeper capabilities are reserved for the kernel's own ``Lead`` and ``Keeper``: a registered
subclass cannot claim them. Stored records keep the plain role name; it is resolved to a class when read.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping

READ = ("fs.list", "fs.read", "fs.search")
WRITE = ("fs.write", "fs.edit")
CODE = ("code.run",)

THINKING_TIERS = ("lead", "worker", "sub")   # which thinking setting applies: thinking_lead / _worker / _sub


class KindError(ValueError):
    pass


class Agent:
    """Base of every kind. Class attributes only; a running instance keeps its state in its record."""

    role: str = ""                   # the name stored in records and plans
    title: str = ""                  # the display name
    tools: tuple[str, ...] = READ    # default tools; a parent can only narrow them
    essential: tuple[str, ...] = ()  # without these the kind cannot do its job (a parent lacking them cannot spawn it)

    controller: bool = False     # the project's top agent: answers to the operator, never reports to a parent
    coordinates: bool = False    # follows others' progress (in a graph: block status, review requests)
    decides_files: bool = False  # the out-of-graph keeper: decides on file requests, nothing else
    spawnable: bool = True       # agent.spawn may create it
    scheduler_only: bool = False # only the graph scheduler may create it
    thinking: str = "sub"        # see THINKING_TIERS

    @classmethod
    def reports(cls) -> bool:
        """Whether it ends its work with agent.report to a parent."""
        return not cls.controller

    @classmethod
    def step_limited(cls) -> bool:
        return not cls.controller


class Coordinator(Agent):
    coordinates = True
    tools = READ + WRITE + CODE


class Lead(Coordinator):
    role = "lead"
    title = "主控"
    tools = ("*",)
    controller = True
    spawnable = False
    thinking = "lead"


class SubLead(Coordinator):
    role = "sublead"
    title = "次主控"
    tools = READ + WRITE + CODE
    thinking = "worker"


class Worker(Agent):
    role = "worker"
    title = "执行员"
    tools = READ + WRITE + CODE


class Reviewer(Agent):
    role = "reviewer"
    title = "检查员"
    tools = READ + WRITE


class Keeper(Agent):
    role = "file_keeper"
    title = "文件处理"
    tools = READ + ("access.decide",)
    decides_files = True
    spawnable = False
    scheduler_only = True


_RESERVED = (Lead, Keeper)   # the only classes that may hold reserved capabilities


def _is_kind(value) -> bool:
    return isinstance(value, type) and issubclass(value, Agent)


class AgentKinds:
    """An ordered set of kinds by role name. Each engine holds its own: extensions of one do not leak to another.

    ``fallback`` are the tools of a role nobody registered (a record from an older version, say)."""

    def __init__(self, kinds=(), *, fallback=READ, guide: str = ""):
        self._kinds: dict[str, type[Agent]] = {}
        self.fallback = tuple(fallback)
        self.guide = guide
        for kind in kinds:
            self.register(kind)

    # ------------------------------------------------------------------ changes

    def _check(self, kind):
        if not _is_kind(kind) or not isinstance(kind.role, str) or not kind.role.strip():
            raise KindError(f"{kind!r} 不是带角色名的 Agent 子类")
        if kind.thinking not in THINKING_TIERS:
            raise KindError(f"角色 {kind.role} 的思考档位未知：{kind.thinking}")
        if (kind.controller or kind.decides_files) and kind not in _RESERVED:
            raise KindError(f"角色 {kind.role} 不能声明内核保留的主控或文件处理能力")
        if not all(isinstance(t, str) and t for t in kind.tools) or not all(isinstance(t, str) for t in kind.essential):
            raise KindError(f"角色 {kind.role} 的工具声明无效")

    def register(self, kind, *, replace: bool = False) -> type[Agent]:
        """Add a kind. ``replace`` lets a host redefine a non-reserved kind in place (same position)."""
        self._check(kind)
        old = self._kinds.get(kind.role)
        if old is not None and old is not kind:
            if old in _RESERVED or not replace:
                raise KindError(f"角色 {kind.role} 由内核保留或已经注册过")
        self._kinds[kind.role] = kind
        return kind

    def install(self, kinds) -> None:
        """Replace every non-reserved kind with ``kinds``, in that order (the reserved ones keep their place)."""
        kinds = list(kinds)
        for kind in kinds:
            self._check(kind)
            if kind.role in self._kinds and self._kinds[kind.role] in _RESERVED and self._kinds[kind.role] is not kind:
                raise KindError(f"角色 {kind.role} 由内核保留")
        kept = {r: k for r, k in self._kinds.items() if k in _RESERVED}
        self._kinds = kept
        for kind in kinds:
            self._kinds[kind.role] = kind

    def copy(self) -> AgentKinds:
        out = AgentKinds(fallback=self.fallback, guide=self.guide)
        out._kinds = dict(self._kinds)
        return out

    # ------------------------------------------------------------------ lookups

    def get(self, role) -> type[Agent] | None:
        return self._kinds.get(role) if isinstance(role, str) else None

    def of(self, role) -> type[Agent]:
        """The kind of a stored role; a role nobody registered acts as a plain read-only agent."""
        return self.get(role) or _unknown(role, self.fallback)

    def __contains__(self, role) -> bool:
        return isinstance(role, str) and role in self._kinds

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._kinds))

    def __len__(self) -> int:
        return len(self._kinds)

    def kinds(self) -> list[type[Agent]]:
        return list(self._kinds.values())

    def tools(self, role) -> list[str]:
        kind = self.get(role)
        return list(kind.tools) if kind else list(self.fallback)

    def title(self, role) -> str:
        kind = self.get(role)
        return kind.title or kind.role if kind else role

    def essential(self, role) -> list[str]:
        kind = self.get(role)
        return list(kind.essential) if kind else []

    def spawnable(self) -> list[str]:
        return [r for r, k in self._kinds.items() if k.spawnable and not k.scheduler_only]

    def assignable(self) -> list[str]:
        """Roles that can be given their own model in the settings: every role but the controller."""
        return [r for r, k in self._kinds.items() if not k.controller]


_UNKNOWN: dict[tuple, type[Agent]] = {}


def _unknown(role, fallback) -> type[Agent]:
    key = (role, tuple(fallback))
    if key not in _UNKNOWN:
        _UNKNOWN[key] = type("Unregistered", (Agent,), {"role": str(role), "title": str(role), "tools": tuple(fallback)})
    return _UNKNOWN[key]


def define(role: str, tools, *, base: type[Agent] = Worker, title: str = "", **capabilities) -> type[Agent]:
    """A kind built from data (for hosts that list roles in a table rather than write classes)."""
    if not _is_kind(base):
        raise KindError("base 必须是 Agent 子类")
    attrs = {"role": role, "title": title or role, "tools": tuple(tools), **capabilities}
    for key in ("essential",):
        if key in attrs:
            attrs[key] = tuple(attrs[key])
    return type("".join(p.capitalize() for p in str(role).replace("-", "_").split("_")) or "Kind", (base,), attrs)


DEFAULT_GUIDE = "按权限选角色：worker 执行、reviewer 检查（不运行代码）；职责与验收要求由当前任务定义。"

# The process default. A Kernel copies it, then adds its extensions' kinds to the copy.
DEFAULT = AgentKinds([Keeper, Lead, Worker, Reviewer, SubLead], guide=DEFAULT_GUIDE)


class KindLookup:
    """Mixed into the runtime parts that ask what an agent may do. An engine sets its own ``kinds``."""

    kinds: AgentKinds = DEFAULT

    def kind(self, of) -> type[Agent]:
        """The kind of an agent: ``of`` is a role name, an agent record, or anything with a ``rec`` record."""
        rec = getattr(of, "rec", of)
        return self.kinds.of(rec.get("role") if isinstance(rec, dict) else rec)


class View(Mapping):
    """A read-only dict view of one attribute over a registry, for code that reads the old role tables."""

    def __init__(self, registry: AgentKinds, read):
        self._registry, self._read = registry, read

    def __getitem__(self, role):
        kind = self._registry.get(role)
        if kind is None:
            raise KeyError(role)
        return self._read(kind)

    def __iter__(self):
        return iter(self._registry)

    def __len__(self):
        return len(self._registry)

    def __repr__(self):
        return repr(dict(self))
