"""A whole folder as one node: an application of any size, in any language, joined to a graph as it stands.

The folder holds a file ``node.json`` that says how to start the application and what it takes and gives:

    {
      "command": ["python", "main.py"],
      "inputs": ["problem", {"name": "data", "required": false, "type": "list"}],
      "outputs": ["answer", "notes"],
      "timeout": 600
    }

Each activation starts the command in the folder. What arrived is handed over as one JSON object, input name
to value, on standard input and in the file named by the environment variable ``INTERLACING_IN``. The
application answers with one JSON object, output name to value: it writes it to the file named by
``INTERLACING_OUT``, or, when it leaves that file empty, as the whole of its standard output. An output it
leaves out sends nothing. Writing the answer to the file leaves standard output free for whatever the
application likes to print.

With ``"format": "text"`` the folder has one input and one output, and the application reads plain text and
writes plain text, as most command-line programs do.

A command that starts with ``python`` runs under the Python that runs the graph. A non-zero exit fails the
node, with the end of what the application wrote to standard error. The application is killed when it runs
longer than ``timeout`` seconds, or the run's time limit, or the run is stopped. When the graph has a
workspace, ``INTERLACING_WORKSPACE`` names it.

This is not a sandbox: the application has the rights of the process that runs the graph.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ..base import Firing, Node, NodeError, Port, PortError, digest
from .process import answer_of

MANIFEST = "node.json"
FORMATS = ("json", "text")
TYPES: dict[str, Any] = {"any": None, "int": int, "float": (float, int), "number": (float, int), "str": str,
                         "bool": bool, "list": list, "dict": dict}


def _port(given: Any, where: str) -> Port:
    if isinstance(given, str):
        given = {"name": given}
    if not isinstance(given, Mapping) or "name" not in given:
        raise NodeError(f"{where}: a port is a name, or a mapping with a name")
    unknown = given.keys() - {"name", "required", "type"}
    if unknown:
        raise NodeError(f"{where}: {given['name']}: unknown {sorted(unknown)}")
    kind = given.get("type", "any")
    if kind not in TYPES:
        raise NodeError(f"{where}: {given['name']}: type is one of {', '.join(TYPES)}, not {kind!r}")
    try:
        return Port(given["name"], TYPES[kind], required=bool(given.get("required", True)))
    except PortError as exc:
        raise NodeError(f"{where}: {exc}") from None


def read_manifest(folder: Path) -> dict:
    """What ``node.json`` in ``folder`` says, checked."""
    path = folder / MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise NodeError(f"{folder} has no {MANIFEST}: it says how to start the application and what it takes and gives") from None
    except ValueError as exc:
        raise NodeError(f"{path}: not JSON: {exc}") from None
    if not isinstance(manifest, Mapping):
        raise NodeError(f"{path}: a JSON object")
    unknown = manifest.keys() - {"command", "inputs", "outputs", "timeout", "format", "env", "about"}
    if unknown:
        raise NodeError(f"{path}: unknown {sorted(unknown)}")
    command = manifest.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        raise NodeError(f"{path}: command is a list: the program and its arguments")
    if manifest.get("format", "json") not in FORMATS:
        raise NodeError(f"{path}: format is one of {', '.join(FORMATS)}")
    timeout = manifest.get("timeout")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout > 0):
        raise NodeError(f"{path}: timeout is a positive number of seconds")
    env = manifest.get("env", {})
    if not isinstance(env, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise NodeError(f"{path}: env maps names to text")
    for side in ("inputs", "outputs"):
        if not isinstance(manifest.get(side, []), list):
            raise NodeError(f"{path}: {side} is a list")
    return dict(manifest)


class FolderNode(Node):
    """``FolderNode("apps/solver")``: the application in that folder, as a node (see the module's description).
    The id is the folder's name unless one is given; ``timeout`` overrides the one in ``node.json``."""

    kind = "folder"

    def __init__(self, folder, id: str | None = None, *, timeout: float | None = None, firing: Firing | None = None,
                 env: Mapping[str, str] | None = None):
        self.given = str(folder)
        self.folder = Path(folder).resolve()
        if not self.folder.is_dir():
            raise NodeError(f"{folder} is not a folder")
        manifest = self.manifest = read_manifest(self.folder)
        where = str(self.folder / MANIFEST)
        inputs = [_port(port, where) for port in manifest.get("inputs", [])]
        outputs = [_port(port, where) for port in manifest.get("outputs", [])]
        self.format = manifest.get("format", "json")
        if self.format == "text" and (len(inputs) > 1 or len(outputs) != 1):
            raise NodeError(f"{where}: a text application takes at most one input and gives one output")
        name = id or self.folder.name.lower().replace("-", "_").replace(" ", "_")
        super().__init__(name, inputs=inputs, outputs=outputs, firing=firing)
        if timeout is not None and not timeout > 0:
            raise NodeError(f"{name}: timeout must be a positive number of seconds")
        self.timeout = timeout if timeout is not None else manifest.get("timeout")
        command = list(manifest["command"])
        if command[0] in ("python", "python3"):
            command[0] = sys.executable
        self.command = command
        self.env = {**manifest.get("env", {}), **(env or {})}
        self.workspace: str | None = None     # set by whoever runs the graph, when it has a workspace

    def config(self) -> Mapping:
        return {"path": self.given, "timeout": self.timeout, "manifest": digest(self.manifest)}

    def _environment(self, given: Path, answer: Path) -> dict:
        env = {**os.environ, **self.env, "INTERLACING_IN": str(given), "INTERLACING_OUT": str(answer),
               "PYTHONIOENCODING": "utf-8"}
        if self.workspace is not None:
            env["INTERLACING_WORKSPACE"] = self.workspace
        return env

    def run(self, ctx, inputs: Mapping[str, Any]):
        if self.format == "text":
            value = next(iter(inputs.values()), "")
            data = (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).encode("utf-8")
        else:
            try:
                data = json.dumps(dict(inputs), ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise NodeError(f"what goes to an application must be plain data: {exc}") from None
        with tempfile.TemporaryDirectory(prefix="interlacing-") as scratch:
            given, answer = Path(scratch) / "in.json", Path(scratch) / "out.json"
            given.write_bytes(data)
            code, out, last, expired = answer_of(self.command, data, ctx, timeout=self.timeout, cwd=self.folder,
                                                 env=self._environment(given, answer), what=f"{self.id} ({self.command[0]})")
            written = answer.read_bytes() if answer.exists() else b""
        if expired:
            raise NodeError(f"{self.id} ran longer than {self.timeout:g} seconds and was ended")
        if ctx.stopping():
            return None
        if code:
            raise NodeError(f"{self.id} exited with {code}" + (f": {last}" if last else ""))
        text = (written or out).decode("utf-8", "replace").replace("\r\n", "\n")
        if self.format == "text":
            return {next(iter(self.outputs)): text}
        if not text.strip():
            return None
        try:
            said = json.loads(text)
        except ValueError as exc:
            raise NodeError(f"{self.id} answered with something that is not JSON ({exc}): {text[:200]!r}") from None
        if not isinstance(said, Mapping):
            raise NodeError(f"{self.id} answers with a JSON object, output name to value; not {type(said).__name__}")
        unknown = said.keys() - self.outputs.keys()
        if unknown:
            raise NodeError(f"{self.id} answered on {sorted(unknown)}, and its outputs are {sorted(self.outputs)}")
        return dict(said)
