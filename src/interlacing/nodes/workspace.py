"""The files of a run: one directory, laid out by who works where, with a record of who wrote what.

A ``Workspace`` is a directory. Every path in it is written relative to its top, the same way the ``file``
protocol writes the paths it carries, so a path means the same thing to every node. The layout is by node::

    solver/             the directory of the node ``solver``
    shared/             what every node may write to
    review/             the block ``review``: one more layer
        writer/         the directory of ``writer`` inside it
        shared/         what the nodes of that block share

An ``Area`` is one node's part of it: its own directory, what it may read and what it may write. By default
a node reads everything and writes to its own directory and to the shared directories of the layers it is
in. Whatever is written through an area is recorded (which node, in which activation), so the workspace can
say where each of its files came from.

An area keeps a node's file tools inside its bounds. It is not a sandbox: a command the node starts runs in
the node's directory with the rights of this process.
"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import threading
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping

from ..base import NodeError
from .agent import Tool
from .process import answer_of

KEPT = ".interlacing"          # where the workspace keeps its own record; no node reads or writes there
SHARED = "shared"
_MOST = 100_000                # the most text one tool answer carries


class AreaError(NodeError):
    """A path a node may not use, or that is not what the node took it for. The message is for the node."""


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._lock = threading.Lock()

    def __repr__(self):
        return f"<Workspace {self.root}>"

    # ---- areas -----------------------------------------------------------------------------------------

    def area(self, node: str, *, layer: Iterable[str] = (), reads: Iterable[str] | None = None,
             writes: Iterable[str] | None = None) -> "Area":
        """The area of ``node``. ``layer`` names the blocks it is inside, outermost first. ``reads`` and
        ``writes`` are paths relative to the top of the workspace (``"."`` is all of it); left out, the node
        reads everything and writes to its own directory and the shared ones of its layers."""
        layer = tuple(layer)
        home = self.root.joinpath(*layer, node)
        if writes is None:
            shared = [self.root.joinpath(*layer[:depth], SHARED) for depth in range(len(layer) + 1)]
            allowed = [home, *shared]
        else:
            allowed = [self._inside(name) for name in writes]
        area = Area(self, "/".join([*layer, node]), home, [self.root] if reads is None else [self._inside(name) for name in reads], allowed)
        area.given = {key: list(names) for key, names in (("reads", reads), ("writes", writes)) if names is not None}
        return area

    def whole(self, node: str = "") -> "Area":
        """An area that is the whole workspace: its node reads and writes anywhere in it."""
        return Area(self, node, self.root, [self.root], [self.root])

    def _inside(self, name: str) -> Path:
        if (not isinstance(name, str) or not name.strip() or PurePosixPath(name).is_absolute()
                or PureWindowsPath(name).is_absolute() or PureWindowsPath(name).drive):
            raise AreaError(f"{name!r} is not a path relative to the top of the workspace")
        path = (self.root / name).resolve()
        if path != self.root and self.root not in path.parents:
            raise AreaError(f"{name} leads outside the workspace")
        if self.name(path).split("/")[0] == KEPT:
            raise AreaError(f"{KEPT} is the workspace's own record")
        return path

    def name(self, path: Path) -> str:
        """A path as it is written: relative to the top, with forward slashes; ``.`` is the top."""
        return path.relative_to(self.root).as_posix()

    # ---- who wrote what --------------------------------------------------------------------------------

    def record(self, path: Path, node: str, round: int) -> None:
        """Note that ``node`` wrote ``path`` in its activation ``round``."""
        try:
            size = path.stat().st_size
        except OSError:
            return
        line = json.dumps({"path": self.name(path), "node": node, "round": round, "size": size,
                           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, ensure_ascii=False)
        with self._lock:
            kept = self.root / KEPT
            kept.mkdir(parents=True, exist_ok=True)
            with open(kept / "written.jsonl", "a", encoding="utf-8") as file:
                file.write(line + "\n")

    def written(self) -> dict[str, dict]:
        """The last record of each path that was written through an area."""
        found: dict[str, dict] = {}
        try:
            with open(self.root / KEPT / "written.jsonl", encoding="utf-8") as file:
                for line in file:
                    try:
                        entry = json.loads(line)
                        found[entry["path"]] = entry
                    except (ValueError, KeyError, TypeError):
                        continue            # a line cut short when a process died
        except OSError:
            pass
        return found

    def files(self) -> list[dict]:
        """Every file in the workspace: its path, its size, what kind of file it is, and which node wrote it
        in which activation, where that is known."""
        written, out = self.written(), []
        for folder, folders, names in os.walk(self.root):
            folders[:] = sorted(name for name in folders if not (Path(folder) == self.root and name == KEPT))
            for name in sorted(names):
                path = Path(folder) / name
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                entry = written.get(self.name(path), {})
                out.append({"path": self.name(path), "size": size, "media": mimetypes.guess_type(name)[0] or "",
                            "node": entry.get("node"), "round": entry.get("round")})
        return out


class Area:
    """One node's part of a workspace. ``locate`` turns a path the node wrote into the real one, or says
    why the node may not use it; the file tools (``tools()``) are built on it."""

    def __init__(self, workspace: Workspace, node: str, home: Path, reads: Iterable[Path], writes: Iterable[Path]):
        self.workspace, self.node, self.home = workspace, node, home
        self.given: dict = {}       # the bounds it was made with, where they are not the usual ones
        self.writes = tuple(dict.fromkeys(writes))
        self.reads = tuple(dict.fromkeys([*reads, *self.writes]))      # what a node may write it may read

    @property
    def root(self) -> Path:
        return self.workspace.root

    def _names(self, paths: Iterable[Path]) -> str:
        names = [self.workspace.name(path) for path in paths]
        return "everything" if "." in names else ", ".join(names) or "nothing"

    def about(self) -> str:
        """What the node is told about where it works."""
        home = self.workspace.name(self.home)
        return (f"You work with files in a workspace. Every path is relative to its top. "
                f"{'Your own directory is its top' if home == '.' else f'Your own directory is {home}'}. "
                f"You may write under: {self._names(self.writes)}. You may read: {self._names(self.reads)}.")

    def locate(self, name: Any, *, write: bool = False) -> Path:
        path = self.workspace._inside(name)
        allowed = self.writes if write else self.reads
        if not any(path == top or top in path.parents for top in allowed):
            raise AreaError(f"{name} is outside what you may {'write' if write else 'read'} ({self._names(allowed)})")
        return path

    def sends(self, payload: Any) -> str | None:
        """A check for a port that carries file paths: what is wrong with sending this one, or None."""
        name = payload.get("path") if isinstance(payload, Mapping) else payload
        try:
            self.locate(name)
        except AreaError as exc:
            return str(exc)
        return None

    # ---- what a node does with files -------------------------------------------------------------------

    def listing(self, name: str = ".") -> str:
        path = self.workspace._inside(name)
        if not any(path == top or top in path.parents or path in top.parents for top in self.reads):
            raise AreaError(f"{name} is outside what you may read ({self._names(self.reads)})")
        if not path.is_dir():
            raise AreaError(f"{name} is not a directory" if path.exists() else f"there is no directory {name}")
        lines = []
        for entry in sorted(path.iterdir(), key=lambda entry: entry.name):
            if path == self.root and entry.name == KEPT:
                continue
            lines.append(f"{entry.name}/" if entry.is_dir() else f"{entry.name}  {entry.stat().st_size} bytes")
        more = f"\n... and {len(lines) - 500} more" if len(lines) > 500 else ""
        return "\n".join(lines[:500]) + more if lines else "(empty)"

    def read(self, name: str, start: int = 1, lines: int | None = None) -> str:
        path = self.locate(name)
        if not path.is_file():
            raise AreaError(f"{name} is a directory" if path.is_dir() else f"there is no file {name}")
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            kind = mimetypes.guess_type(path.name)[0] or "of no known kind"
            return f"{name} is not text: it is {kind}, {len(data)} bytes"
        if start > 1 or lines is not None:
            rows = text.splitlines(keepends=True)
            text = "".join(rows[max(start, 1) - 1: None if lines is None else max(start, 1) - 1 + max(lines, 0)])
        if len(text) > _MOST:
            text = text[:_MOST] + f"\n... ({len(text) - _MOST} more characters: read it in parts, with start and lines)"
        return text

    def write(self, name: str, content: str, ctx=None) -> str:
        path = self.locate(name, write=True)
        if path.is_dir():
            raise AreaError(f"{name} is a directory")
        if not isinstance(content, str):
            raise AreaError("content is the text of the file")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        self._wrote(path, ctx)
        return f"wrote {self.workspace.name(path)} ({path.stat().st_size} bytes)"

    def edit(self, name: str, old: str, new: str, ctx=None) -> str:
        path = self.locate(name, write=True)
        if not path.is_file():
            raise AreaError(f"there is no file {name}")
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise AreaError(f"{name} is not text") from None
        count = text.count(old) if isinstance(old, str) and old else 0
        if count != 1:
            raise AreaError(f"the text to replace must be found exactly once in {name}; it is there {count} times")
        path.write_text(text.replace(old, new), encoding="utf-8", newline="")
        self._wrote(path, ctx)
        return f"changed {self.workspace.name(path)}"

    def delete(self, name: str) -> str:
        path = self.locate(name, write=True)
        if path in self.writes or path == self.home or path == self.root:
            raise AreaError(f"{name} is a directory of the workspace's layout and stays")
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        else:
            raise AreaError(f"there is nothing at {name}")
        return f"deleted {name}"

    def move(self, source: str, target: str, ctx=None) -> str:
        start, end = self.locate(source, write=True), self.locate(target, write=True)
        if not start.exists():
            raise AreaError(f"there is nothing at {source}")
        if end.exists():
            raise AreaError(f"there is already something at {target}")
        end.parent.mkdir(parents=True, exist_ok=True)
        os.replace(start, end)
        for path in ([end] if end.is_file() else [p for p in end.rglob("*") if p.is_file()]):
            self._wrote(path, ctx)
        return f"moved {source} to {target}"

    def command(self, line: str, ctx, timeout: float | None = None) -> str:
        """Run one line in the system's shell, in the node's own directory, and say how it ended and what it
        wrote. Files it left changed where the node may write are recorded as the node's."""
        if not isinstance(line, str) or not line.strip():
            raise AreaError("command is one line for the shell")
        self.home.mkdir(parents=True, exist_ok=True)
        before = self._seen()
        code, out, _, expired = answer_of(line, b"", ctx, timeout=timeout, cwd=str(self.home), shell=True, together=True,
                                          what="the command")
        for path, stamp in self._seen().items():
            if before.get(path) != stamp:
                self._wrote(path, ctx)
        said = out.decode("utf-8", "replace").replace("\r\n", "\n")
        if len(said) > 20_000:
            said = "... " + said[-20_000:]
        if expired:
            return f"the command ran longer than {timeout:g} seconds and was ended\n{said}"
        return f"exit code {code}\n{said}"

    def _seen(self) -> dict[Path, tuple]:
        found: dict[Path, tuple] = {}
        for top in self.writes:
            for folder, folders, names in os.walk(top):
                if Path(folder) == self.root and KEPT in folders:
                    folders.remove(KEPT)
                for name in names:
                    path = Path(folder) / name
                    try:
                        status = path.stat()
                    except OSError:
                        continue
                    found[path] = (status.st_size, status.st_mtime_ns)
        return found

    def _wrote(self, path: Path, ctx) -> None:
        self.workspace.record(path, self.node or getattr(ctx, "node", ""), getattr(ctx, "round", 0))

    # ---- the same, as tools a model uses ---------------------------------------------------------------

    def tools(self, *, commands: bool = False, timeout: float | None = 120.0) -> list[Tool]:
        """The file tools of this area, for an agent: list, read, write, edit, delete and move, and with
        ``commands`` a tool that runs a command in the node's own directory (for at most ``timeout``
        seconds)."""
        def text(description: str = "") -> dict:
            return {"type": "string", "description": description}

        def tool(name: str, description: str, properties: dict, required: list, run) -> Tool:
            return Tool(name, description, {"type": "object", "properties": properties, "required": required}, run=run)

        path = text("relative to the top of the workspace")
        made = [
            tool("list_files", "List what a directory of the workspace holds.", {"path": path}, [],
                 lambda a, ctx: self.listing(a.get("path") or ".")),
            tool("read_file", "Read a text file, or part of one.",
                 {"path": path, "start": {"type": "integer", "description": "the first line to read, from 1"},
                  "lines": {"type": "integer", "description": "how many lines to read"}}, ["path"],
                 lambda a, ctx: self.read(a.get("path"), _whole(a.get("start"), 1), _whole(a.get("lines"), None))),
            tool("write_file", "Write a text file, replacing it if it is there.", {"path": path, "content": text()},
                 ["path", "content"], lambda a, ctx: self.write(a.get("path"), a.get("content"), ctx)),
            tool("edit_file", "Replace one piece of text in a file. The text to replace must occur exactly once.",
                 {"path": path, "old": text("the text to replace"), "new": text("what replaces it")}, ["path", "old", "new"],
                 lambda a, ctx: self.edit(a.get("path"), a.get("old"), a.get("new", ""), ctx)),
            tool("delete_file", "Delete a file, or a directory with all it holds.", {"path": path}, ["path"],
                 lambda a, ctx: self.delete(a.get("path"))),
            tool("move_file", "Move or rename a file or a directory.", {"source": path, "target": path},
                 ["source", "target"], lambda a, ctx: self.move(a.get("source"), a.get("target"), ctx)),
        ]
        if commands:
            made.append(tool("run_command", "Run one command line in the shell, in your own directory, and get its "
                                            "exit code and what it wrote.", {"command": text()}, ["command"],
                             lambda a, ctx: self.command(a.get("command"), ctx, timeout)))
        return made


def _whole(value: Any, default):
    return value if type(value) is int else default
