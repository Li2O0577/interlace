"""Git operations on a project folder.

Every project is a git repository. The engine commits after each state change,
so every project has a full history that can be read back (status, diff, log).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

LOCAL_TIMEOUT = 60
MAX_DIFF_CHARS = 200_000

SYSTEM_AUTHOR = ("1ndeterminate", "system@1ndeterminate.local")
HUMAN_AUTHOR = ("Human", "human@1ndeterminate.local")


class GitError(Exception):
    pass


def git_available() -> bool:
    return shutil.which("git") is not None


def author_for(actor: str) -> tuple[str, str]:
    if actor == "human":
        return HUMAN_AUTHOR
    if actor in ("", "system"):
        return SYSTEM_AUTHOR
    return (f"Agent ({actor})", f"{re.sub(r'[^a-zA-Z0-9_.-]', '-', actor)}@agent.1ndeterminate.local")


class GitRepo:
    def __init__(self, root: Path):
        self.root = root

    # ---- plumbing -------------------------------------------------------------

    def run(self, *args: str, env: dict | None = None, timeout: int = LOCAL_TIMEOUT,
            check: bool = True, author: tuple[str, str] = SYSTEM_AUTHOR) -> str:
        if not git_available():
            raise GitError("未找到 git，请先安装 git")
        full_env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": SYSTEM_AUTHOR[0], "GIT_COMMITTER_EMAIL": SYSTEM_AUTHOR[1],
            **(env or {}),
        }
        try:
            result = subprocess.run(
                ["git", "-c", "core.quotepath=false", *args], cwd=self.root, env=full_env,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} 超时") from exc
        if check and result.returncode != 0:
            raise GitError((result.stderr or result.stdout).strip() or f"git {args[0]} 失败")
        return result.stdout

    def is_repo(self) -> bool:
        return (self.root / ".git").exists()

    def init(self) -> None:
        if not self.is_repo():
            self.run("init", "-q", "-b", "main")

    def has_commits(self) -> bool:
        return self.run("rev-parse", "--verify", "-q", "HEAD", check=False).strip() != ""

    # ---- state ----------------------------------------------------------------

    def current_branch(self) -> str:
        return self.run("branch", "--show-current").strip() or "(detached)"

    def head(self) -> str | None:
        out = self.run("rev-parse", "--short", "HEAD", check=False).strip()
        return out or None

    def status(self) -> dict:
        raw = self.run("status", "--porcelain=v1", "-z", "-uall")
        files, parts, i = [], raw.split("\0"), 0
        while i < len(parts):
            entry = parts[i]
            i += 1
            if len(entry) < 4:
                continue
            code, path = entry[:2], entry[3:]
            if code[0] in "RC":  # rename/copy: the next field is the original path
                i += 1
            files.append({"path": path, "code": code, "status": _describe_status(code)})
        return {"branch": self.current_branch(), "head": self.head(), "clean": not files, "files": files}

    def is_dirty(self) -> bool:
        return bool(self.run("status", "--porcelain", "-uall").strip())

    def diff(self, path: str | None = None) -> str:
        """Working tree vs HEAD; untracked files are shown as fully added."""
        args = ["diff", "HEAD", "--"] if self.has_commits() else ["diff", "--cached", "--"]
        out = self.run(*args, *([path] if path else []))
        untracked = self.run("ls-files", "--others", "--exclude-standard", "-z", *([path] if path else [])).split("\0")
        for rel in filter(None, untracked):
            target = self.root / rel
            try:
                text = target.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                out += f"\n新文件（二进制）: {rel}\n"
                continue
            out += f"\ndiff --git a/{rel} b/{rel}\nnew file\n--- /dev/null\n+++ b/{rel}\n"
            out += "".join(f"+{line}\n" for line in text.splitlines())
        return _clip(out)

    def show(self, ref: str) -> dict:
        self._check_ref(ref)
        meta = self.run("show", "-s", "--format=%H%x1f%h%x1f%at%x1f%an%x1f%B", ref)
        full, short, ts, author, body = (meta.split("\x1f") + [""] * 5)[:5]
        patch = self.run("show", "--format=", "--stat", "--patch", ref)
        return {"hash": full, "short": short, "ts": int(ts or 0), "author": author,
                "message": body.strip(), "patch": _clip(patch)}

    def log(self, limit: int = 100, ref: str | None = None) -> list[dict]:
        if not self.has_commits():
            return []
        args = ["log", f"-{limit}", "--decorate=short", "--pretty=format:%H%x1f%h%x1f%at%x1f%an%x1f%s%x1f%D"]
        if ref:
            self._check_ref(ref)
            args.append(ref)
        commits = []
        for line in self.run(*args).splitlines():
            parts = line.split("\x1f")
            if len(parts) == 6:
                commits.append({"hash": parts[0], "short": parts[1], "ts": int(parts[2]), "author": parts[3],
                                "message": parts[4], "refs": [r.strip() for r in parts[5].split(",") if r.strip()]})
        return commits

    # ---- commits ----------------------------------------------------------------

    def commit_all(self, message: str, actor: str = "system") -> str | None:
        """Stage everything and commit. Returns the short hash, or None if
        there was nothing to commit."""
        self.run("add", "-A")
        if not self.run("diff", "--cached", "--name-only").strip() and self.has_commits():
            return None
        # The engine checks the file layout itself; the pre-commit hook is for
        # commits made outside it (VS Code, terminal).
        self.run("commit", "-q", "--no-verify", "--allow-empty-message", "-m", message, author=author_for(actor))
        return self.head()

    # ---- branches ---------------------------------------------------------------

    # ---- helpers ---------------------------------------------------------------------

    def _check_ref(self, ref: str) -> None:
        if not ref or ref.startswith("-") or not self.run("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False).strip():
            raise GitError(f"找不到版本: {ref}")


def _describe_status(code: str) -> str:
    if code == "??":
        return "新文件"
    x, y = code[0], code[1]
    c = y if y != " " else x
    return {"M": "已修改", "A": "新增", "D": "已删除", "R": "重命名", "C": "复制", "U": "冲突"}.get(c, code.strip())


def _clip(text: str) -> str:
    if len(text) > MAX_DIFF_CHARS:
        return text[:MAX_DIFF_CHARS] + "\n…（内容过长，已截断）\n"
    return text
