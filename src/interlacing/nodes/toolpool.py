"""A tool pool: a directory of tool files, from which an agent is given tools by path.

A tool is one file:

``plot.py``     a Python file. The tool is the function in it that has the file's name (or is called ``run``,
                or is the only one whose name does not begin with ``_``). Its docstring is what the model is
                told about it; its parameters, with their annotations and defaults, are the tool's
                arguments. The file is read for this, not run.
``fetch.json``  ``{"description": ..., "parameters": {JSON schema}, "command": ["./fetch.sh", "--json"]}``:
                any program. It is given the arguments as JSON to read and what it writes is the answer.
                A program named with a relative path is looked for in the pool.

Using a tool runs it in a process of its own, in the directory of the node that used it, with the top of the
workspace in the environment variable ``INTERLACING_WORKSPACE``: it is ended when it overruns or the run stops, and when it fails the model is told and nothing else is affected. A tool
file is code, so which directory is a pool is for whoever starts the run to say, not for a graph.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from ..base import NodeError
from .agent import Tool
from .process import answer_of

_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array", "dict": "object"}

# What the other process runs for a Python tool: load the file, call the function, write what it returned.
_BOOT = """
import importlib.util, json, os, sys
out, sys.stdout = sys.stdout, sys.stderr
try:
    sys.path.insert(0, os.path.dirname(sys.argv[1]))
    spec = importlib.util.spec_from_file_location("tool", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = json.dumps({"value": getattr(module, sys.argv[2])(**json.load(sys.stdin))}, default=str)
except BaseException as exc:
    text = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
out.write(text)
out.flush()
"""


class ToolError(NodeError):
    """A tool file that cannot be made into a tool, or a tool that failed. The message says which and why."""


class ToolPool:
    """``ToolPool(directory)``. ``tool("plot.py")`` is one tool, ``tools("charts")`` every tool in a
    directory of the pool (``tools()``: in the pool itself). ``timeout`` is how long one use may take."""

    def __init__(self, directory, *, timeout: float | None = 120.0):
        self.root = Path(directory).resolve()
        if timeout is not None and not timeout > 0:
            raise ToolError("timeout must be a positive number of seconds")
        self.timeout = timeout

    def _path(self, name: str) -> Path:
        if not isinstance(name, str) or not name.strip():
            raise ToolError(f"a tool is named by its path in the pool, not {name!r}")
        path = (self.root / name).resolve()
        if path != self.root and self.root not in path.parents:
            raise ToolError(f"{name} is outside the tool pool")
        return path

    def tools(self, name: str = ".", *, cwd=None, top=None) -> list[Tool]:
        """The tool a path names, or every tool in the directory it names (``.py`` and ``.json`` files whose
        names do not begin with ``_`` or ``.``)."""
        path = self._path(name)
        if path.is_file():
            return [self.tool(name, cwd=cwd, top=top)]
        if not path.is_dir():
            raise ToolError(f"there is no {name} in the tool pool")
        found = [entry for entry in sorted(path.iterdir()) if entry.is_file() and entry.suffix in (".py", ".json")
                 and not entry.name.startswith(("_", "."))]
        return [self.tool(entry.relative_to(self.root).as_posix(), cwd=cwd, top=top) for entry in found]

    def tool(self, name: str, *, cwd=None, top=None, spec: Mapping | None = None) -> Tool:
        """The tool in the file ``name``. Its uses run in ``cwd``, and are told ``top``, the top of the
        workspace, in ``INTERLACING_WORKSPACE``. With ``spec`` (a tool as a graph's
        description recorded it) the file is not read again for what the tool is called and takes."""
        path = self._path(name)
        if not path.is_file():
            raise ToolError(f"there is no tool file {name} in the tool pool")
        where = path.relative_to(self.root).as_posix()
        if path.suffix == ".py":
            function, described = _python(path, where)
            run = self._python(path, function, where, cwd, top)
        elif path.suffix == ".json":
            command, described = _program(path, where, self.root)
            run = self._program(command, where, cwd, top)
        else:
            raise ToolError(f"{where}: a tool file is a .py file, or a .json file that names a program")
        if spec is not None:
            described = {key: spec.get(key, described[key]) for key in ("name", "description", "parameters")}
        return Tool(described["name"], described["description"], described["parameters"], run=run, file=where)

    # ---- running ---------------------------------------------------------------------------------------

    def _start(self, command: list, args: Mapping, ctx, where: str, cwd, top, env=None):
        try:
            data = json.dumps(dict(args)).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ToolError(f"the arguments are not plain data: {exc}") from None
        if cwd is not None:
            Path(cwd).mkdir(parents=True, exist_ok=True)
        if top is not None:
            env = {**(env or os.environ), "INTERLACING_WORKSPACE": str(top)}
        code, out, last, expired = answer_of(command, data, ctx, timeout=self.timeout, cwd=str(cwd) if cwd is not None else None,
                                             env=env, what=f"the tool {where}")
        if expired:
            raise ToolError(f"{where} ran longer than {self.timeout:g} seconds and was ended")
        return code, out, last

    def _python(self, path: Path, function: str, where: str, cwd, top):
        def run(args: Mapping, ctx) -> Any:
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            code, out, last = self._start([sys.executable, "-c", _BOOT, str(path), function], args, ctx, where, cwd, top, env)
            try:
                said = json.loads(out.decode("utf-8"))
                if not isinstance(said, dict) or not said.keys() & {"value", "error"}:
                    raise ValueError("not an answer")
            except ValueError:
                raise ToolError(f"{where} ended with {code} and no answer" + (f": {last}" if last else "")) from None
            if "error" in said:
                raise ToolError(said["error"])
            return said["value"]
        return run

    def _program(self, command: list, where: str, cwd, top):
        def run(args: Mapping, ctx) -> Any:
            code, out, last = self._start(command, args, ctx, where, cwd, top)
            if code:
                raise ToolError(f"{where} exited with {code}" + (f": {last}" if last else ""))
            text = out.decode("utf-8", "replace").replace("\r\n", "\n")
            try:
                return json.loads(text)
            except ValueError:
                return text
        return run


def _name(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", path.stem)


def _python(path: Path, where: str) -> tuple[str, dict]:
    """Which function of a Python file is the tool, and what the model is told about it."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as exc:
        raise ToolError(f"{where} cannot be read as Python: {exc}") from None
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    public = [name for name in functions if not name.startswith("_")]
    name = _name(path) if _name(path) in functions else "run" if "run" in functions else public[0] if len(public) == 1 else None
    if name is None:
        raise ToolError(f"{where}: the tool is a function named {_name(path)} or run, or the file's only public "
                        f"function; it has {', '.join(public) or 'none'}")
    function = functions[name]
    given = function.args
    if given.posonlyargs or given.vararg:
        raise ToolError(f"{where}: {name} is called with named arguments, so it cannot take positional-only or *args")
    properties: dict[str, dict] = {}
    required = []
    plain = given.args
    defaults = [None] * (len(plain) - len(given.defaults)) + list(given.defaults)
    for argument, default in [*zip(plain, defaults), *zip(given.kwonlyargs, given.kw_defaults)]:
        noted = argument.annotation
        kind = _TYPES.get(noted.id) if isinstance(noted, ast.Name) else _TYPES.get(getattr(getattr(noted, "value", None), "id", ""))
        properties[argument.arg] = {"type": kind} if kind else {}
        if default is None:
            required.append(argument.arg)
    return name, {"name": _name(path), "description": (ast.get_docstring(function) or "").strip(),
                  "parameters": {"type": "object", "properties": properties, "required": required}}


def _program(path: Path, where: str, root: Path) -> tuple[list, dict]:
    try:
        said = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError(f"{where} is not JSON: {exc}") from None
    command = said.get("command") if isinstance(said, dict) else None
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise ToolError(f"{where}: command is a list of the program and its arguments")
    parameters = said.get("parameters") or {"type": "object", "properties": {}}
    if not isinstance(parameters, dict):
        raise ToolError(f"{where}: parameters is a JSON schema of the arguments")
    program = (path.parent / command[0]).resolve()
    if not Path(command[0]).is_absolute() and program.is_file():
        if root not in program.parents:
            raise ToolError(f"{where}: {command[0]} is outside the tool pool")
        command = [str(program), *command[1:]]
    return command, {"name": _name(path), "description": str(said.get("description", "")), "parameters": parameters}
