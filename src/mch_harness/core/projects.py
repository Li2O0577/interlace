"""Project storage. A project is a folder under the workspace; the folder is
the source of truth (``project.json`` + artifacts), versioned with git.

This is the task-neutral store: a project has a name, a task description, an
autonomy level and a mode. An application subclasses it to add its own fields,
folder standard and start-up files (``_validate``, ``_meta``, ``_initialize``).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import time
from pathlib import Path
from typing import Any

from .jsonfile import replace_file
from .git import GitError, GitRepo, git_available

# Runtime files that change constantly and should not clutter the history.
PROJECT_GITIGNORE = "state/events.jsonl\nstate/mailbox/\nagents/\n__pycache__/\n"

EDITABLE_FIELDS = {"name", "language", "description", "autonomy"}
AUTONOMY_LEVELS = {"full", "checkpoints", "step"}
PROJECT_MODES = {"graph"}
MAX_PREVIEW_BYTES = 512 * 1024


class ProjectError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug[:32] or "project"


def safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(" .")
    return name or "file"


class ProjectStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)

    # ---- paths -------------------------------------------------------------

    def repo(self, project_id: str) -> GitRepo:
        return GitRepo(self.path(project_id))

    def path(self, project_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9-]+", project_id or ""):
            raise ProjectError("项目 ID 不合法", 404)
        p = self.workspace / project_id
        if not (p / "project.json").is_file():
            raise ProjectError("项目不存在", 404)
        return p

    def events_path(self, project_id: str) -> Path:
        return self.path(project_id) / "state" / "events.jsonl"

    def resolve(self, project_id: str, rel: str) -> Path:
        """Resolve a project-relative path, refusing anything outside the project."""
        root = self.path(project_id).resolve()
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            raise ProjectError("路径越界", 400)
        if ".git" in target.relative_to(root).parts:
            raise ProjectError("不允许访问 .git", 400)
        return target

    # ---- CRUD --------------------------------------------------------------

    gitignore = PROJECT_GITIGNORE
    modes = PROJECT_MODES
    default_mode = "graph"

    def _validate(self, fields: dict) -> None:
        """Raise ProjectError for application fields that are not acceptable."""
        if fields:
            raise ProjectError(f"未知的项目字段: {', '.join(sorted(fields))}")

    def _meta(self, fields: dict) -> dict:
        """Application fields to store in project.json next to the generic ones."""
        return {}

    def _initialize(self, root: Path, repo: GitRepo, meta: dict) -> None:
        """Start-up files of a new project (before its first commit)."""
        for folder in ("work", "nodes", "state"):
            (root / folder).mkdir(exist_ok=True)
        (root / "work" / ".gitkeep").touch()
        if meta["mode"] == "graph":
            from ..graph.store import GraphStore
            from ..graph.templates import starter
            GraphStore(root).draft("human", starter(meta["description"] or meta["name"]))

    def create(self, name: str, *, language: str = "zh", description: str = "", autonomy: str = "checkpoints",
               mode: str | None = None, **fields) -> dict:
        name = (name or "").strip()
        mode = mode or self.default_mode
        if not name:
            raise ProjectError("项目名称不能为空")
        if mode not in self.modes:
            raise ProjectError(f"未知任务类型: {mode}")
        self._validate(fields)
        if autonomy not in AUTONOMY_LEVELS:
            autonomy = "checkpoints"
        if not git_available():
            raise ProjectError("未找到 git：项目的版本管理与回溯依赖 git，请先安装 git", 500)
        project_id = f"{_slug(name)}-{secrets.token_hex(3)}"
        root = self.workspace / project_id
        root.mkdir(parents=True)
        (root / ".gitignore").write_text(self.gitignore, encoding="utf-8")
        now = time.time()
        meta = {"id": project_id, "name": name, "language": language, "description": description,
                "autonomy": autonomy, "mode": mode, **self._meta(fields), "created_at": now, "updated_at": now}
        self._write_meta(root, meta)
        repo = GitRepo(root)
        try:
            repo.init()
            self._initialize(root, repo, meta)
            repo.commit_all("创建项目")
            repo.checkpoint("项目创建", kind="created")
        except (GitError, OSError, ValueError) as exc:
            shutil.rmtree(root, ignore_errors=True)
            raise ProjectError(f"初始化项目失败: {exc}", 500) from exc
        return meta

    def list(self) -> list[dict]:
        projects = []
        for child in self.workspace.iterdir():
            meta_file = child / "project.json"
            if meta_file.is_file():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                events = child / "state" / "events.jsonl"
                if events.exists():
                    meta["updated_at"] = max(meta.get("updated_at", 0), events.stat().st_mtime)
                projects.append(meta)
        projects.sort(key=lambda m: m.get("updated_at", 0), reverse=True)
        return projects

    def get(self, project_id: str) -> dict:
        return json.loads((self.path(project_id) / "project.json").read_text(encoding="utf-8"))

    def update(self, project_id: str, changes: dict[str, Any]) -> dict:
        unknown = set(changes) - EDITABLE_FIELDS
        if unknown:
            raise ProjectError(f"不可修改的字段: {', '.join(sorted(unknown))}")
        if "autonomy" in changes and changes["autonomy"] not in AUTONOMY_LEVELS:
            raise ProjectError(f"未知自动程度: {changes['autonomy']}")
        if "name" in changes and not str(changes["name"]).strip():
            raise ProjectError("项目名称不能为空")
        return self._apply(project_id, changes)

    def _apply(self, project_id: str, changes: dict[str, Any]) -> dict:
        meta = self.get(project_id)
        meta.update(changes)
        meta["updated_at"] = time.time()
        root = self.path(project_id)
        self._write_meta(root, meta)
        GitRepo(root).commit_all("更新项目设置: " + ", ".join(sorted(changes)), actor="human")
        return meta

    def delete(self, project_id: str) -> None:
        target=self.path(project_id)
        resolved=target.resolve()
        workspace=self.workspace.resolve()
        if (resolved==workspace or not resolved.is_relative_to(workspace) or target.is_symlink()
                or hasattr(target,'is_junction') and target.is_junction()):
            raise ProjectError('拒绝删除工作区之外或链接指向的项目')
        def retry_readonly(function,path,error):
            candidate=Path(path)
            if (os.name!='nt' or not isinstance(error,PermissionError) or
                    not candidate.resolve().is_relative_to(resolved) or candidate.is_symlink() or
                    not candidate.is_file() or candidate.stat().st_mode & stat.S_IWRITE):
                raise error
            os.chmod(candidate,stat.S_IREAD|stat.S_IWRITE)
            function(path)
        shutil.rmtree(target,onexc=retry_readonly)
        self._deleted(project_id)

    def _deleted(self, project_id: str) -> None:
        """After a project folder is gone (an application drops references to it)."""

    @staticmethod
    def _write_meta(root: Path, meta: dict) -> None:
        tmp = root / "project.json.tmp"
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        replace_file(tmp,root / "project.json")

    # ---- files -------------------------------------------------------------

    def tree(self, project_id: str) -> list[dict]:
        root = self.path(project_id)

        def walk(directory: Path) -> list[dict]:
            entries = []
            for child in sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                if child.name in {".git", ".gitkeep"} or child.name.endswith(".tmp"):
                    continue
                rel = child.relative_to(root).as_posix()
                if child.is_dir():
                    entries.append({"name": child.name, "path": rel, "type": "dir", "children": walk(child)})
                else:
                    st = child.stat()
                    entries.append({"name": child.name, "path": rel, "type": "file",
                                    "size": st.st_size, "mtime": st.st_mtime})
            return entries

        return walk(root)

    def _preview(self, target: Path) -> dict | None:
        """Extra fields for a file type the application knows how to preview, or None."""
        return None

    def read_text(self, project_id: str, rel: str) -> dict:
        target = self.resolve(project_id, rel)
        if not target.is_file():
            raise ProjectError("文件不存在", 404)
        size = target.stat().st_size
        special = self._preview(target)
        if special is not None:
            return {"path": rel, "size": size, "binary": False, "content": None, "truncated": False, **special}
        raw = target.open("rb").read(MAX_PREVIEW_BYTES)
        if b"\x00" in raw[:8192]:
            return {"path": rel, "size": size, "binary": True, "content": None, "truncated": False}
        return {
            "path": rel,
            "size": size,
            "binary": False,
            "content": raw.decode("utf-8", errors="replace"),
            "truncated": size > MAX_PREVIEW_BYTES,
        }
