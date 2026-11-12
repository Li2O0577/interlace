"""Services the host application supplies; the kernel's own defaults are task-neutral.

The kernel never imports an application. A host that renders views, or enforces a folder
standard passes its own :class:`HostAdapters` to the
``AgentManager`` / ``Kernel`` it constructs. A process that embeds exactly one
application may also install process-wide defaults with :func:`set_default`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PROTECTED = ("state/", ".git/", "project.json", "LAYOUT.md")


class HostError(Exception):
    pass


class Views:
    """Rendered tables and charts are an application feature: by default text stays text."""
    block = None        # compiled pattern of an embedded view block, when the host has one

    def embed(self, root: Path, text: str) -> tuple[str, list]:
        return text, []

    def resolve_many(self, root: Path, specs: list) -> list:
        if specs:
            raise HostError("当前宿主没有注册视图服务：请把结果写入文件，在汇报里给出文件路径")
        return []

    def compact(self, view: dict) -> dict:
        return view


class FilePolicy:
    """Where agents may write. Graph node folders: work/ is the node's, the rest is kept by the program."""

    def check_write(self, rel: str, size: int, exists: bool) -> None:
        if not rel or rel.startswith(PROTECTED) or rel in PROTECTED:
            raise HostError(f"{rel or '项目根目录'} 由系统维护，不能直接写入")
        parts = rel.split("/")
        if parts[0] == "nodes" and not (len(parts) >= 5 and parts[3] == "work"):
            raise HostError("节点 submit/state/目录说明由系统维护，工作结果请写入本轮 work/")

    def read_special(self, target: Path) -> dict | None:
        """A converted reading of a file type the host understands (notebooks, …), or None."""
        return None


@dataclass
class HostAdapters:
    views: Any = field(default_factory=Views)
    files: Any = field(default_factory=FilePolicy)
    hooks: Any = None                                    # task policy (agents.hooks.Hooks); None = generic
    project_store: Callable[[Path], Any] | None = None   # workspace -> project store; None = kernel store


_default: HostAdapters | None = None


def default() -> HostAdapters:
    global _default
    if _default is None:
        _default = HostAdapters()
    return _default


def set_default(adapters: HostAdapters) -> None:
    """For a process hosting one application whose call sites build managers without adapters."""
    global _default
    _default = adapters
