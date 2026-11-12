"""Run the document_review task once with scripted fake models (SIMULATION: no model is called).

    python run_simulation.py WORKDIR

It shows the order of control: the operator confirms the plan, the scheduler starts nodes as their inputs
arrive, the controller releases each block, and the proofreader's custom tool passes the node boundary.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from indeterminate import ChatResult, FakeProvider, Kernel  # noqa: E402
from indeterminate.graph.recovery import STATE_TEMPLATE  # noqa: E402

import document_review  # noqa: E402

STATE = STATE_TEMPLATE.replace("（无事项时写明无；只保存可核查工作记录）", "SIMULATION：无")


def call(name, args):
    return ChatResult("", [{"id": name, "name": name, "args": args}])


def scripted(kernel):
    """A fake model per agent: node instances do their steps in order; the controller releases on request."""
    def policy(system, messages, tools):
        done = [c["name"] for m in messages for c in m.get("tool_calls", [])]
        node = re.search(r"你是节点 ([a-z_-]+)，实例 ([a-z_-]+):r(\d+)", system)
        if node is None:   # the controller
            for msg in reversed(messages):
                asked = re.search(r"RELEASE ([a-z_-]+) IN ([a-z0-9-]+)", msg.get("content") or "")
                if msg.get("role") == "user" and asked:
                    block, pid = asked.groups()
                    state = kernel.manager.graph(pid).run.state()["blocks"].get(block)
                    if state and not state["released"] and "graph.release" not in done[-1:]:
                        return call("graph.release", {"block": block, "round": state["round"], "note": "SIMULATION：放行"})
                    break
            return ChatResult("SIMULATION：等待。", [])
        name, _, rnd = node.groups()
        work = f"nodes/{name}/r{rnd}/work/"
        if name == "drafter":
            steps = [("fs.write", {"path": work + "summary.md", "content": "背景\n方法\n结论\n"}),
                     ("graph.save_state", {"content": STATE}),
                     ("graph.submit", {"summary": "SIMULATION：摘要", "files": [work + "summary.md"]})]
        else:
            source = "nodes/drafter/r1/work/summary.md"
            steps = [("review.count_lines", {"path": source, "out": work + "lines.txt"}),
                     ("fs.write", {"path": work + "report.md", "content": "SIMULATION：未发现问题\n"}),
                     ("graph.save_state", {"content": STATE}),
                     ("graph.submit", {"summary": "SIMULATION：校对完成", "files": [work + "lines.txt", work + "report.md"]})]
        for tool, args in steps:
            if tool not in done:
                return call(tool, args)
        return ChatResult("SIMULATION：已交付。", [])
    return lambda model: FakeProvider(policy)


def wait(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not check():
        time.sleep(0.05)
    return check()


def main(workdir: Path, inspect=None) -> dict:
    """``inspect(kernel, pid)`` runs before the kernel closes (tests use it to probe the boundaries)."""
    kernel = Kernel(workdir / "workspace", workdir / "home", extensions=[document_review.ext], simulation=True)
    kernel.manager.provider_factory = scripted(kernel)
    with kernel:
        created = kernel.create_project("审查示例", "为材料写摘要并校对", task_type="document_review", request_id="example-1")
        pid = created["project"]["id"]
        run = kernel.manager.graph(pid).run
        kernel.operator.confirm(pid, created["plan_hash"])
        assert wait(lambda: run.state()["submissions"].get("drafter")), "drafter did not deliver"
        assert "proofreader" not in run.state()["instances"], "the next block started before release"
        kernel.request(pid, "release-draft", f"RELEASE draft IN {pid}")
        assert wait(lambda: run.state()["submissions"].get("proofreader")), "proofreader did not deliver"
        lines = (kernel.store.path(pid) / "nodes/proofreader/r1/work/lines.txt").read_text(encoding="utf-8")
        result = {"project": pid, "lines": lines.strip(), "artifacts": [a["path"] for a in kernel.artifacts(pid)],
                  "released": [b for b, s in run.state()["blocks"].items() if s.get("released")]}
        if inspect:
            result["inspected"] = inspect(kernel, pid)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "example-run"))
