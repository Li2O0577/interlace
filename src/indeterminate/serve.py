"""The same operations as the command line, offered to a client over the Model Context Protocol.

    1ndeterminate serve --runs DIRECTORY [--allow MODULE]... [--processes] [--workers N] [--time-limit SECONDS]
                        [--files DIRECTORY]

The client (usually a model in some other application) sends graphs as data and gets runs back. What those
graphs may reach is decided by whoever starts the server, on its command line, and never by the client: a
client cannot widen ``--allow``, cannot turn ``--processes`` on, cannot choose where a run is kept, and can
take text only from files under ``--files`` (from none when it is not given).
Model keys come from the server's environment. Every run is a directory under ``--runs``, the same directory
the command line works on, so a run started here can be looked at or answered from a terminal.

The transport is stdio: one JSON-RPC message per line, in on standard input and out on standard output.
Standard output carries nothing else; anything a node prints goes to standard error.

Tools: ``check``, ``start``, ``status``, ``resume``, ``answer``, ``runs``. A tool call lasts as long as the
run does until it is done, needs a person, or fails; calls are handled one at a time.
"""
from __future__ import annotations

import contextlib
import json
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from . import __version__
from .engine import JournalError
from .runs import Run, check, models_from_environment
from .spec import SpecError

VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")

_GRAPH = {"type": "object", "description": "The graph as data: nodes, edges, inputs and outputs, or a method with its nodes."}
_RUN = {"type": "string", "description": "The run's id, as start returned it."}
TOOLS = [
    {"name": "check", "description": "Build a graph from its description without running it. Returns its digest, "
     "nodes, inputs and outputs, or says why it cannot be built.",
     "inputSchema": {"type": "object", "properties": {"graph": _GRAPH}, "required": ["graph"]}},
    {"name": "start", "description": "Start a run of a graph. Returns when the run is done, is waiting for a person "
     "(status waiting, with the questions), has stopped, or has failed. Keep the run id it returns.",
     "inputSchema": {"type": "object", "properties": {
         "graph": _GRAPH, "inputs": {"type": "object", "description": "A value for each of the graph's inputs."}},
         "required": ["graph"]}},
    {"name": "status", "description": "Where a run stands, and the questions in it that are waiting for an answer.",
     "inputSchema": {"type": "object", "properties": {"run": _RUN}, "required": ["run"]}},
    {"name": "resume", "description": "Carry on a run that was waiting, stopped or failed. Nothing that had finished runs again.",
     "inputSchema": {"type": "object", "properties": {"run": _RUN}, "required": ["run"]}},
    {"name": "answer", "description": "Give a person's answer to a question a run is waiting on. The answer must come "
     "from the person who was asked. Call resume afterwards.",
     "inputSchema": {"type": "object", "properties": {
         "run": _RUN, "answer": {"description": "The answer, any JSON value."},
         "question": {"type": "string", "description": "The question's id; not needed when only one is open."}},
         "required": ["run", "answer"]}},
    {"name": "runs", "description": "The runs this server holds, newest first, with the status of each.",
     "inputSchema": {"type": "object", "properties": {}}},
]


class Refused(Exception):
    """A request the protocol does not allow; becomes a JSON-RPC error."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class Server:
    def __init__(self, runs, *, allow=(), processes: bool = False, workers: int = 0,
                 models: Callable[[str], Any] | None = models_from_environment, time_limit: float | None = None, files=None):
        self.root = Path(runs)
        self.time_limit, self.files = time_limit, files
        self.allow, self.processes, self.workers, self.models = list(allow), bool(processes), int(workers), models

    # ---- the operations ----------------------------------------------------------------------------------

    def _run(self, arguments: Mapping) -> Run:
        id = arguments.get("run")
        if not isinstance(id, str) or not _ID.match(id) or not (self.root / id / "graph.json").exists():
            raise SpecError(f"no run {id!r}")
        return Run(self.root / id, models=self.models)

    def _graph(self, arguments: Mapping) -> Mapping:
        graph = arguments.get("graph")
        if not isinstance(graph, Mapping):
            raise SpecError("graph must be an object describing the graph")
        return graph

    def check(self, arguments: Mapping) -> dict:
        return check(self._graph(arguments), allow=self.allow, processes=self.processes, files=self.files)

    def start(self, arguments: Mapping) -> dict:
        inputs = arguments.get("inputs") or {}
        if not isinstance(inputs, Mapping):
            raise SpecError("inputs must be an object: a value for each graph input")
        id = f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{secrets.token_hex(3)}"
        run = Run.create(self.root / id, self._graph(arguments), allow=self.allow, processes=self.processes,
                         workers=self.workers, models=self.models, time_limit=self.time_limit, files=self.files)
        return {"run": id, **run.start(inputs)}

    def status(self, arguments: Mapping) -> dict:
        return {"run": arguments["run"], **self._run(arguments).status()}

    def resume(self, arguments: Mapping) -> dict:
        return {"run": arguments["run"], **self._run(arguments).resume()}

    def answer(self, arguments: Mapping) -> dict:
        if "answer" not in arguments:
            raise SpecError("answer is missing")
        return {"run": arguments["run"], **self._run(arguments).answer(arguments["answer"], arguments.get("question"))}

    def runs(self, arguments: Mapping) -> dict:
        found = []
        for path in sorted(self.root.glob("*/state.json"), key=lambda p: p.parent.name, reverse=True):
            state = json.loads(path.read_text(encoding="utf-8"))
            found.append({"run": path.parent.name, "status": state["status"], "updated": state["updated"]})
        return {"runs": found}

    # ---- the protocol ------------------------------------------------------------------------------------

    def handle(self, message: Any) -> dict | None:
        """Answer one JSON-RPC message. Notifications get no answer."""
        if not isinstance(message, Mapping) or not isinstance(message.get("method"), str):
            if isinstance(message, Mapping) and ("result" in message or "error" in message):
                return None                                        # a reply to something we never asked
            return _error(None, -32600, "not a JSON-RPC request")
        id, method, params = message.get("id"), message["method"], message.get("params") or {}
        if "id" not in message:
            return None
        try:
            return {"jsonrpc": "2.0", "id": id, "result": self._method(method, params)}
        except Refused as exc:
            return _error(id, exc.code, str(exc))

    def _method(self, method: str, params: Mapping) -> dict:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "1ndeterminate", "version": __version__},
                    "instructions": "Runs graphs of nodes written as data. check a graph, start it, and when a run "
                                    "is waiting put its questions to the person, answer, then resume."}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments") or {}
            if name not in {tool["name"] for tool in TOOLS}:
                raise Refused(-32602, f"no tool {name!r}")
            if not isinstance(arguments, Mapping):
                raise Refused(-32602, "arguments must be an object")
            try:
                state, failed = getattr(self, name)(arguments), False
            except (SpecError, JournalError) as exc:
                state, failed = {"status": "refused", "error": str(exc)}, True
            except Exception as exc:                                # the server outlives a call that goes wrong
                state, failed = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}, True
            return {"content": [{"type": "text", "text": json.dumps(state, ensure_ascii=False, indent=2, default=str)}],
                    "structuredContent": json.loads(json.dumps(state, default=str)), "isError": failed}
        raise Refused(-32601, f"no method {method!r}")

    def serve(self, lines=None, out=None) -> None:
        """Read messages, one per line, until the input ends."""
        lines, out = lines if lines is not None else sys.stdin, out or sys.stdout
        with contextlib.redirect_stdout(sys.stderr):                # what a node prints must not reach the client
            for line in lines:
                if not line.strip():
                    continue
                try:
                    reply = self.handle(json.loads(line))
                except ValueError:
                    reply = _error(None, -32700, "not JSON")
                if reply is not None:
                    out.write(json.dumps(reply, ensure_ascii=False) + "\n")
                    out.flush()


def _error(id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}

