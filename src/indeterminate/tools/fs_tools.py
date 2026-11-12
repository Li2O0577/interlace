"""File tools (fs.*) and quick code execution (code.run) for agents.

Writes go through the host's file policy (``ctx.files``): ``.git`` and ``state``
are always off limits, node folders are writable only under ``work/``, and an
application may add its own folder standard and read-only areas.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from ..core import sandbox
from ..host import HostError, default as default_host
from .registry import ToolContext, ToolError, tool

MAX_READ = 200_000
MAX_OUTPUT = 30_000


def _files(ctx):
    """The host's file policy; a bare context (tests, scripts) gets the process default."""
    return getattr(ctx, "files", None) or default_host().files


def _resolve(ctx: ToolContext, path: str) -> tuple[Path, str]:
    root = ctx.root.resolve()
    target = (root / (path or ".")).resolve()
    if target != root and root not in target.parents:
        raise ToolError("路径越界：只能访问项目文件夹内的文件")
    rel = "" if target == root else target.relative_to(root).as_posix()
    if rel == ".git" or rel.startswith(".git/"):
        raise ToolError("不允许访问 .git")
    return target, rel


def _nearby(root: Path, name: str) -> str:
    """Folders elsewhere in the project with the name that was asked for (agents often drop the parent folder)."""
    hits = sorted(p.relative_to(root).as_posix() for p in root.rglob(name)
                  if p.is_dir() and ".git" not in p.parts and "agents" not in p.parts[:1])[:5]
    return f"（同名目录：{'、'.join(hits)}）" if hits else ""


@tool("fs.list", "列出文件", "列出目录内容（默认项目根目录，深度 2）。", {"path": {"type": "string"}, "depth": {"type": "integer"}},
      readonly=True)
def fs_list(ctx: ToolContext, path: str = ".", depth: int = 2):
    target, rel = _resolve(ctx, path)
    if not target.is_dir():
        raise ToolError(f"不是目录：{path}" if target.exists() else f"目录不存在：{path}" + _nearby(ctx.root, target.name))
    lines = []

    def walk(d: Path, level: int):
        for child in sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if child.name in (".git", ".gitkeep", "__pycache__") or child.name.endswith(".tmp"):
                continue
            indent = "  " * level
            if child.is_dir():
                lines.append(f"{indent}{child.name}/")
                if level + 1 < depth:
                    walk(child, level + 1)
            else:
                lines.append(f"{indent}{child.name}  ({child.stat().st_size} B)")
            if len(lines) > 500:
                return

    walk(target, 0)
    return {"path": rel or ".", "entries": "\n".join(lines[:500]) or "（空目录）"}


@tool("fs.read", "读取文件", "读取文本文件（可指定起始行和行数）。", {"path": {"type": "string"}, "start": {"type": "integer"},
      "lines": {"type": "integer"}}, ["path"], readonly=True)
def fs_read(ctx: ToolContext, path: str, start: int = 1, lines: int = 400):
    target, rel = _resolve(ctx, path)
    if not target.is_file():
        raise ToolError(f"文件不存在：{path}")
    special = _files(ctx).read_special(target)   # a file type the host converts to text (a notebook, …)
    if special is not None:
        all_lines = special["text"].splitlines()
        start = max(1, start)
        chunk = all_lines[start - 1: start - 1 + max(1, min(lines, 2000))]
        return {"path": rel, **special.get("fields", {}), "start": start, "end": start + len(chunk) - 1,
                "total_lines": len(all_lines),
                "content": "\n".join(f"{start + i:>5}| {line}" for i, line in enumerate(chunk))[:MAX_READ],
                **({"note": special["note"]} if special.get("note") else {})}
    raw = target.read_bytes()[: MAX_READ * 4]
    if b"\x00" in raw[:4096]:
        return {"path": rel, "binary": True, "size": target.stat().st_size,
                "content": "二进制文件，无法直接读取（表格请用 code.run 中的 pandas 读取）"}
    all_lines = raw.decode("utf-8", "replace").splitlines()
    start = max(1, start)
    chunk = all_lines[start - 1: start - 1 + max(1, min(lines, 2000))]
    return {"path": rel, "start": start, "end": start + len(chunk) - 1, "total_lines": len(all_lines),
            "content": "\n".join(f"{start + i:>5}| {line}" for i, line in enumerate(chunk))[:MAX_READ]}


@tool("fs.search", "搜索文件内容", "在项目文件中按正则表达式搜索（可限定目录）。", {"pattern": {"type": "string"}, "path": {"type": "string"}},
      ["pattern"], readonly=True)
def fs_search(ctx: ToolContext, pattern: str, path: str = "."):
    target, _ = _resolve(ctx, path)
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"正则表达式有误：{exc}") from exc
    root = ctx.root.resolve()
    hits = []
    files = [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file()]
    for f in files:
        rel = f.relative_to(root).as_posix()
        if rel.startswith((".git/", "state/")) or f.stat().st_size > 2_000_000:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{no}: {line.strip()[:200]}")
                if len(hits) >= 200:
                    return {"matches": hits, "truncated": True}
    return {"matches": hits, "truncated": False}


def _check_write(ctx: ToolContext, rel: str, size: int, exists: bool) -> None:
    try:
        _files(ctx).check_write(rel, size, exists)
    except HostError as exc:
        raise ToolError(str(exc)) from exc


@tool("fs.write", "写入文件", "创建或覆盖一个文本文件。只读区域不能写。",
      {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])
def fs_write(ctx: ToolContext, path: str, content: str):
    target, rel = _resolve(ctx, path)
    data = content.encode("utf-8")
    _check_write(ctx, rel, len(data), target.exists())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": rel, "bytes": len(data)}


@tool("fs.edit", "修改文件", "把文件中唯一出现的一段文字替换为新文字。", {"path": {"type": "string"}, "old": {"type": "string"},
      "new": {"type": "string"}}, ["path", "old", "new"])
def fs_edit(ctx: ToolContext, path: str, old: str, new: str):
    target, rel = _resolve(ctx, path)
    if not target.is_file():
        raise ToolError(f"文件不存在：{path}")
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise ToolError(f"要替换的文字在文件中出现了 {count} 次，需要恰好 1 次（多带一些上下文）")
    updated = text.replace(old, new)
    _check_write(ctx, rel, len(updated.encode()), True)
    target.write_text(updated, encoding="utf-8")
    return {"path": rel, "bytes": len(updated.encode())}


_SLEEP = re.compile(r"(?:\btime\.sleep|\bsleep)\s*\(?\s*(\d+(?:\.\d+)?)\s*([smh]?)\b")


def _idle_wait(text: str) -> str:
    """A long sleep in a quick run is waiting, not work (a few seconds of pacing is fine)."""
    for m in _SLEEP.finditer(text or ""):
        seconds = float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
        if seconds >= 20:
            return m.group(0).strip()
    return ""


@tool("code.run", "运行代码", "在项目根目录下运行一段 Python 代码（或一条 shell 命令）并返回输出，最长 300 秒。"
      "适合快速试算和检查；不适合长时间等待。",
      {"code": {"type": "string", "description": "Python 代码"}, "command": {"type": "string", "description": "或者一条 shell 命令"},
       "timeout": {"type": "integer"}}, dangerous=True)
def code_run(ctx: ToolContext, code: str = "", command: str = "", timeout: int = 120):
    if bool(code) == bool(command):
        raise ToolError("code 和 command 需要且只能提供一个")
    timeout = max(1, min(timeout, 300))
    idle = _idle_wait(code or command)
    if idle:
        raise ToolError(f"不要在 code.run 里干等（{idle}）：需要等待时先做别的，"
                        "或直接结束本轮。")
    limits = sandbox.config(ctx.settings.raw() if ctx.settings else None)
    if command:
        try:
            sandbox.check_command(command, ctx.root, limits)
        except sandbox.SandboxError as exc:
            raise ToolError(str(exc)) from exc
    env = sandbox.env_for(ctx.root, limits, extra={"PYTHONUNBUFFERED": "1", "MPLBACKEND": "Agg"})
    if code:
        scratch = ctx.root / "agents" / (ctx.actor if ctx.actor != "human" else "human") / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        script = scratch / f"run_{time.strftime('%H%M%S')}_{os.getpid() % 1000}.py"
        script.write_text(code, encoding="utf-8")
        args, shell = [sys.executable, str(script)], False
    else:
        args, shell = sandbox.shell_command(command), True
    started = time.time()
    # Runs without the project lock, so other agents keep working.
    try:
        result = subprocess.run(args, shell=shell, cwd=ctx.root, env=env, capture_output=True, timeout=timeout,
                                preexec_fn=sandbox.preexec(limits))
        out, err, rc = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as exc:
        out, err, rc = exc.stdout or b"", (exc.stderr or b"") + f"\n超时（{timeout} 秒）被终止".encode(), -1
    except OSError as exc:
        out, err, rc = b"", f"无法启动：{exc}".encode(), -1
    clip = lambda b: b.decode("utf-8", "replace").replace('\r\n','\n')[-MAX_OUTPUT:]  # noqa: E731
    res = {"exit_code": rc, "stdout": clip(out), "stderr": clip(err), "seconds": round(time.time() - started, 2)}
    return res
