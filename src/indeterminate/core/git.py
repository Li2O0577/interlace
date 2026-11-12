"""Git operations on a project folder.

Every project is a git repository. The workbench uses it for:
- automatic commits after each state change (full history, diff, rollback);
- checkpoints: annotated tags ``cp-NNNN`` carrying JSON metadata (which
  workflow node, why). Tags survive restores, so a backtrack can be undone;
- exploration branches for trying alternative approaches;
- syncing with a remote (GitHub) via push / pull.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

LOCAL_TIMEOUT = 60
NETWORK_TIMEOUT = 180
MAX_DIFF_CHARS = 200_000

SYSTEM_AUTHOR = ("MCH", "mch@localhost")
HUMAN_AUTHOR = ("Human", "human@mch.local")


class GitError(Exception):
    pass


def git_available() -> bool:
    return shutil.which("git") is not None


def author_for(actor: str) -> tuple[str, str]:
    if actor == "human":
        return HUMAN_AUTHOR
    if actor in ("", "system"):
        return SYSTEM_AUTHOR
    return (f"MCH Agent ({actor})", f"{re.sub(r'[^a-zA-Z0-9_.-]', '-', actor)}@agent.mch.local")


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

    def run_bytes(self, *args: str, timeout: int = NETWORK_TIMEOUT) -> bytes:
        """Run git and return raw stdout (e.g. ``git archive``)."""
        try:
            result = subprocess.run(["git", *args], cwd=self.root, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} 超时") from exc
        if result.returncode != 0:
            raise GitError(result.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} 失败")
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
        # The workbench checks the file layout itself; the pre-commit hook is for
        # commits made outside it (VS Code, terminal).
        self.run("commit", "-q", "--no-verify", "--allow-empty-message", "-m", message, author=author_for(actor))
        return self.head()

    # ---- branches ---------------------------------------------------------------

    def branches(self) -> list[dict]:
        out = self.run("for-each-ref", "refs/heads", "--format=%(refname:short)%1f%(objectname:short)%1f%(HEAD)%1f%(committerdate:unix)%1f%(contents:subject)")
        result = []
        for line in out.splitlines():
            name, head, cur, ts, subject = (line.split("\x1f") + [""] * 5)[:5]
            result.append({"name": name, "head": head, "current": cur == "*", "ts": int(ts or 0), "subject": subject})
        return result

    def validate_branch_name(self, name: str) -> str:
        name = (name or "").strip()
        if not name or self.run("check-ref-format", "--branch", name, check=False).strip() != name:
            raise GitError(f"分支名不合法: {name!r}")
        return name

    def create_branch(self, name: str, start: str | None = None) -> None:
        name = self.validate_branch_name(name)
        if start:
            self._check_ref(start)
        self.run("branch", name, *([start] if start else []))

    def checkout(self, name: str) -> None:
        if self.is_dirty():
            raise GitError("工作区有未提交的修改，请先提交")
        self.run("checkout", "-q", name)

    def delete_branch(self, name: str) -> None:
        if name == self.current_branch():
            raise GitError("不能删除当前所在分支")
        self.run("branch", "-D", name)

    # ---- checkpoints --------------------------------------------------------------

    def checkpoint(self, label: str, **meta) -> str:
        """Tag HEAD as a checkpoint. Returns the tag name."""
        numbers = [int(t[3:]) for t in self.run("tag", "--list", "cp-*").split() if t[3:].isdigit()]
        tag = f"cp-{(max(numbers) + 1) if numbers else 1:04d}"
        payload = json.dumps({"label": label, **meta}, ensure_ascii=False)
        self.run("tag", "-a", tag, "-m", payload)
        return tag

    def checkpoints(self) -> list[dict]:
        out = self.run("for-each-ref", "refs/tags/cp-*",
                       "--format=%(refname:short)%1f%(*objectname:short)%1f%(taggerdate:unix)%1f%(contents)%1e")
        result = []
        for record in out.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            tag, commit, ts, contents = (record.split("\x1f") + [""] * 4)[:4]
            try:
                meta = json.loads(contents.strip())
            except ValueError:
                meta = {"label": contents.strip()}
            result.append({"tag": tag, "commit": commit, "ts": int(ts or 0), **meta})
        # Newest first. Tag numbers are monotonic; timestamps only have 1 s resolution.
        result.sort(key=lambda cp: int(cp["tag"][3:]) if cp["tag"][3:].isdigit() else -1, reverse=True)
        return result

    def restore(self, ref: str) -> None:
        """Make the working tree identical to ``ref`` (tracked files only).
        History is kept: the caller commits the result as a new commit."""
        self._check_ref(ref)
        if self.is_dirty():
            raise GitError("工作区有未提交的修改，请先提交")
        self.run("read-tree", "-u", "--reset", ref)

    # ---- remotes -------------------------------------------------------------------

    def remote_url(self, name: str = "origin") -> str | None:
        out = self.run("remote", "get-url", name, check=False).strip()
        return out or None

    def set_remote(self, url: str, name: str = "origin") -> None:
        if self.remote_url(name):
            self.run("remote", "set-url", name, url)
        else:
            self.run("remote", "add", name, url)

    def remove_remote(self, name: str = "origin") -> None:
        if self.remote_url(name):
            self.run("remote", "remove", name)

    def push(self, branch: str | None = None, env: dict | None = None, tags: bool = True) -> str:
        branch = branch or self.current_branch()
        out = self.run("push", "-u", "origin", branch, env=env, timeout=NETWORK_TIMEOUT)
        if tags:
            out += self.run("push", "origin", "--tags", env=env, timeout=NETWORK_TIMEOUT)
        return out

    def pull(self, branch: str | None = None, env: dict | None = None) -> str:
        if self.is_dirty():
            raise GitError("工作区有未提交的修改，请先提交")
        branch = branch or self.current_branch()
        return self.run("pull", "--ff-only", "origin", branch, env=env, timeout=NETWORK_TIMEOUT)

    def fetch(self, env: dict | None = None) -> None:
        self.run("fetch", "origin", env=env, timeout=NETWORK_TIMEOUT)

    def ahead_behind(self, branch: str | None = None) -> dict | None:
        branch = branch or self.current_branch()
        upstream = f"origin/{branch}"
        if not self.run("rev-parse", "--verify", "-q", upstream, check=False).strip():
            return None
        out = self.run("rev-list", "--left-right", "--count", f"HEAD...{upstream}").split()
        return {"ahead": int(out[0]), "behind": int(out[1])}

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
