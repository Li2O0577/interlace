"""Lineage tools: what is out of date after a change, and where a file came from."""

from __future__ import annotations

import contextlib

from .. import lineage as L
from .registry import ToolContext, ToolError, tool

WHY = {"changed": "被修改", "deleted": "被删除", "replaced": "已被新文件取代", "upstream": "所依据的文件已过时"}


def describe(s: dict) -> str:
    parts = []
    for r in s["reasons"][:3]:
        text = f"{r['input']} {WHY.get(r['why'], r['why'])}"
        if r.get("by"):
            text += f"（新文件 {r['by']}）"
        parts.append(text)
    return f"{s['path']}：{'；'.join(parts)}"


def begin(ctx: ToolContext, paths: list[str] | None = None) -> set[str] | None:
    """Files already out of date before a change (None: nothing was ever made from ``paths``, nothing to track)."""
    return L.stale_paths(ctx.root) if paths is None or L.consumed(ctx.root, [p for p in paths if p]) else None


def finish(ctx: ToolContext, before: set[str] | None, box: dict) -> None:
    """After a change: report files that became out of date and mark the finished stage leaves that produced
    them by running code as "needs re-checking"."""
    if before is None:
        return
    fresh, steps = L.newly_stale_steps(ctx.root, before)
    if not fresh:
        return
    box["stale"] = [describe(s) for s in fresh[:12]]
    marked = []
    if steps:
        with contextlib.suppress(Exception):
            marked = ctx.workflow.mark_stale(list(steps), "所依据的文件有变化：" + "、".join(sorted({p for v in steps.values() for p in v})[:4]))
    box["marked"] = marked
    ctx.emit("lineage.stale", {"files": [s["path"] for s in fresh], "steps": marked, "by": ctx.actor})


@contextlib.contextmanager
def watch(ctx: ToolContext, paths: list[str] | None = None):
    """begin … change … finish, for changes made while holding the project lock."""
    before = begin(ctx, paths)
    box: dict = {}
    yield box
    finish(ctx, before, box)


def attach(result: dict, box: dict) -> dict:
    if box.get("stale"):
        result["outdated"] = box["stale"]
        result["outdated_hint"] = ("这些文件是用旧版本做出来的，已过时：重新运行生成它们的代码（或说明为什么不受影响）；"
                                   "用 lineage.status 查看完整列表。")
        if box.get("marked"):
            result["outdated_hint"] += f"相关阶段已标记为需复查：{', '.join(box['marked'])}。"
    return result


@tool("lineage.status", "过时文件", "修改或替换了文件之后，哪些结果、图、文档和论文数字是用旧版本做出来的（需要重算或复查），以及原因。"
      "依据团队每次写文件、运行代码时自动记录的来源（谁由哪些文件做出）。", {}, readonly=True)
def status(ctx: ToolContext):
    st = L.status(ctx.root)
    return {"stale": [{**s, "summary": describe(s)} for s in st["stale"]], "replaced": st["replaced"],
            "paper_numbers": st["paper_numbers"],
            "summary": (f"{len(st['stale'])} 个文件过时，{len(st['paper_numbers'])} 个论文数字的出处过时"
                        if st["stale"] or st["paper_numbers"] else "没有过时的文件")}


@tool("lineage.trace", "文件来源", "一个文件是由哪些文件、哪次运行做出来的（上游），又被用来做了什么（下游）；"
      "用来回答“这个数字/这张图从哪来”“改这个文件会影响什么”。", {"path": {"type": "string"}}, ["path"], readonly=True)
def trace(ctx: ToolContext, path: str):
    rel = path.strip().lstrip("./")
    if not (ctx.root / rel).exists() and not any(rel in e["outputs"] or rel in e["inputs"] for e in L.entries(ctx.root)):
        raise ToolError(f"没有 {rel} 的记录")
    return L.trace(ctx.root, rel)
