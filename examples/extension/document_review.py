"""A third-party extension of 1ndeterminate: document review.

It imports nothing but ``indeterminate`` and adds, for the one Kernel it is given to:

* an agent kind, ``Proofreader``: a Reviewer that may also count lines;
* a tool, ``review.count_lines``, with a path contract (it reads ``path`` and writes ``out``), so the kernel
  checks both paths against the node's boundary exactly like its own file tools;
* a task type, ``document_review``: draft a summary, then proofread it, each in its own block.

The operator still confirms the plan; the controller still releases each block.
"""
from __future__ import annotations

from pathlib import Path

from indeterminate import Extension, TaskType, Reviewer

ext = Extension("document_review")


class Proofreader(Reviewer):
    role = "proofreader"
    title = "校对员"
    tools = ("fs.list", "fs.read", "fs.search", "fs.write", "fs.edit", "review.count_lines")
    essential = ("review.count_lines",)


ext.kinds.append(Proofreader)


@ext.tool("review.count_lines", "统计行数", "统计一个已授权文件的非空行数，把结果写入自己的工作目录。",
          {"path": {"type": "string", "description": "要统计的文件"},
           "out": {"type": "string", "description": "结果写到哪里（本节点 work/ 下）"}},
          ["path", "out"], reads=("path",), writes=("out",))
def count_lines(ctx, path: str, out: str) -> dict:
    text = (ctx.root / path).read_text(encoding="utf-8")
    count = sum(1 for line in text.splitlines() if line.strip())
    target = ctx.root / out
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{path}: {count} 行\n", encoding="utf-8")
    return {"path": path, "lines": count, "written": out}


def build_plan(task: str) -> dict:
    return {
        "task": task,
        "nodes": [
            {"id": "lead", "name": "主控", "summary": "确认、验收和放行", "role": "lead"},
            {"id": "drafter", "name": "起草", "summary": "写摘要", "role": "worker", "block": "draft",
             "duty": "阅读 material/ 下的材料并写出摘要", "accept": "摘要覆盖材料要点", "reads": ["material/"]},
            {"id": "proofreader", "name": "校对", "summary": "校对摘要", "role": "proofreader", "block": "check",
             "duty": "校对起草节点交付的摘要，统计行数并写出问题清单", "accept": "列出问题和行数"},
        ],
        "edges": [
            {"id": "draft", "from": "drafter", "to": "proofreader", "kind": "deliverable", "start": True, "label": "摘要"},
            {"id": "report", "from": "proofreader", "to": "lead", "kind": "review", "start": False, "label": "校对结论"},
        ],
        "blocks": [{"id": "draft", "name": "起草", "after": []},
                   {"id": "check", "name": "校对", "after": ["draft"]}],
    }


def initialize(root: Path) -> None:
    folder = root / "material"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "source.md").write_text("第一段：背景。\n第二段：方法。\n第三段：结论。\n", encoding="utf-8")


ext.task_types.append(TaskType("document_review", build_plan, initialize, title="文档审查"))
