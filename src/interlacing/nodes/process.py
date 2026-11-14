"""Nodes that run outside this process: an external program, and a Python function in a process of its own."""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..base import Firing, Node, NodeError, Port, canonical

FORMATS = ("text", "json", "bytes")


class ProcessNode(Node):
    """Runs ``command`` (a list: the program and its arguments, no shell) once per activation.

    ``format`` says how the input is written and the output read: ``text`` (UTF-8), ``json`` or ``bytes``.
    With ``lines=True`` each line of output is sent on as soon as the program writes it; otherwise the whole
    output is sent when the program ends. With ``takes_input=False`` the node has no input and starts by
    itself. A non-zero exit, or running longer than ``timeout`` seconds, fails the node; when the run is
    being stopped the program is killed.
    """

    kind = "process"

    def __init__(self, id: str, command: Sequence[str], *, format: str = "text", lines: bool = False,
                 timeout: float | None = None, takes_input: bool = True, cwd: str | None = None,
                 env: Mapping[str, str] | None = None):
        if isinstance(command, (str, bytes)) or not command or not all(isinstance(part, str) for part in command):
            raise NodeError(f"{id}: command is a list of the program and its arguments")
        if format not in FORMATS:
            raise NodeError(f"{id}: format is one of {', '.join(FORMATS)}")
        if lines and format == "bytes":
            raise NodeError(f"{id}: lines needs text or json")
        super().__init__(id, inputs=[Port("x")] if takes_input else [], outputs=[Port("out")])
        self.command, self.format, self.lines, self.timeout = list(command), format, bool(lines), timeout
        self.cwd, self.env = cwd, dict(env) if env is not None else None
        self.workspace: str | None = None     # where it runs when it was given no cwd of its own; set by whoever runs the graph

    def config(self) -> Mapping:
        return {"command": self.command, "format": self.format, "lines": self.lines, "timeout": self.timeout,
                "cwd": self.cwd}

    def _write(self, value: Any) -> bytes:
        if self.format == "bytes":
            if not isinstance(value, (bytes, bytearray)):
                raise NodeError(f"expected bytes, got {type(value).__name__}")
            return bytes(value)
        return (value if self.format == "text" and isinstance(value, str) else canonical(value)).encode("utf-8")

    def _read(self, data: bytes) -> Any:
        if self.format == "bytes":
            return data
        text = data.decode("utf-8").replace("\r\n", "\n")     # a program on Windows ends its lines with \r\n
        if self.format == "text":
            return text
        try:
            return json.loads(text)
        except ValueError as exc:
            raise NodeError(f"the program's output is not JSON: {exc}") from None

    def run(self, ctx, inputs: Mapping[str, Any]):
        data = self._write(inputs["x"]) if "x" in self.inputs else b""
        with tempfile.TemporaryFile() as errors:
            try:
                process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                           cwd=self.cwd or self.workspace, env=self.env)
            except OSError as exc:
                raise NodeError(f"cannot start {self.command[0]}: {exc}") from None
            expired = threading.Event()
            def overrun():
                expired.set()
                process.kill()
            timer = threading.Timer(self.timeout, overrun) if self.timeout else None
            if timer:
                timer.start()
            try:
                output = self._lines(ctx, process, data) if self.lines else self._whole(ctx, process, data)
            finally:
                if timer:
                    timer.cancel()
                if process.poll() is None:
                    process.kill()
                process.wait()
                for pipe in (process.stdin, process.stdout):
                    if pipe and not pipe.closed:
                        pipe.close()
            if expired.is_set():
                raise NodeError(f"{self.command[0]} ran longer than {self.timeout} seconds")
            if ctx.stopping():
                return None
            if process.returncode:
                errors.seek(0)
                said = errors.read()[-2000:].decode("utf-8", "replace").strip()
                raise NodeError(f"{self.command[0]} exited with {process.returncode}" + (f": {said}" if said else ""))
        return None if output is None else {"out": output}

    def _whole(self, ctx, process, data: bytes):
        given: bytes | None = data
        while True:
            try:
                out, _ = process.communicate(given, timeout=0.05)
                return self._read(out) if not process.returncode else None
            except subprocess.TimeoutExpired:
                given = None            # already handed over; asking again loses nothing
                if ctx.stopping():
                    process.kill()

    def _lines(self, ctx, process, data: bytes):
        def feed():
            try:
                process.stdin.write(data)
                process.stdin.close()
            except OSError:
                pass                    # the program stopped reading
        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        for line in process.stdout:
            if ctx.stopping():
                process.kill()
                break
            line = line.rstrip(b"\r\n")
            if line or self.format == "text":
                ctx.emit("out", self._read(line))
        writer.join()
        return None


# What the other process runs: it finds the function by name, calls it with the inputs it reads, and writes
# what the function returned, or why it failed. Whatever the function prints goes to standard error.
_BOOT = """
import importlib, json, sys
out, sys.stdout = sys.stdout, sys.stderr
module, _, name = sys.argv[1].rpartition(".")
try:
    found = importlib.import_module(module)
    for part in name.split("."):
        found = getattr(found, part)
    said = {"value": found(**json.load(sys.stdin))}
    text = json.dumps(said)
except BaseException as exc:
    text = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
out.write(text)
out.flush()
"""


def _importable(id: str, function: Any) -> str:
    """The dotted name another process can find the function by."""
    if isinstance(function, str):
        name = function
    elif callable(function):
        name = f"{getattr(function, '__module__', '')}.{getattr(function, '__qualname__', '')}"
    else:
        raise NodeError(f"{id}: a function, or the dotted name of one; not {function!r}")
    module, _, attribute = name.rpartition(".")
    if not module or not attribute or "<" in name or module == "__main__":
        raise NodeError(f"{id}: {name} cannot be found from another process: it must be a function at the top "
                        f"level of a module that can be imported")
    if not isinstance(function, str):
        try:
            found: Any = importlib.import_module(module)
            for part in attribute.split("."):
                found = getattr(found, part)
        except (ImportError, AttributeError):
            found = None
        if found is not function:
            raise NodeError(f"{id}: {name} cannot be found from another process")
    return name


class IsolatedNode(Node):
    """Runs a Python function in a process of its own, once per activation, and so can be ended from outside.

    A function that runs in the engine's own process runs in a thread, and a thread cannot be stopped: one
    that overruns its time is abandoned and goes on in the background. This node's function runs in another
    process instead. When it overruns the run's time limit, or its own ``timeout``, or the run is stopped,
    that process is killed. A crash in it (a segfault in a library, memory running out) fails this node and
    leaves the engine standing.

    ``function`` is a function at the top level of an importable module, or its dotted name. It is called
    with the inputs as keyword arguments, as ``FunctionNode`` calls its function, and what it returns is
    treated the same way. Inputs and what it returns cross between the processes as JSON, so they are plain
    data. The function gets no context: it cannot emit several times or make calls.

    This is not a sandbox: the other process has the rights of this one.
    """

    kind = "isolated"

    def __init__(self, id: str, function: Callable | str, *, inputs: Iterable[Port] = (), outputs: Iterable[Port] = (),
                 firing: Firing | None = None, timeout: float | None = None, cwd: str | None = None,
                 env: Mapping[str, str] | None = None):
        super().__init__(id, inputs=inputs, outputs=outputs, firing=firing)
        if any(port.call for port in self.outputs.values()):
            raise NodeError(f"{id}: a function in another process cannot make calls")
        if timeout is not None and not timeout > 0:
            raise NodeError(f"{id}: timeout must be a positive number of seconds")
        self.function, self.timeout = _importable(id, function), timeout
        self.cwd, self.env = cwd, dict(env) if env is not None else None
        self.workspace: str | None = None
        self._sends = list(self.outputs)

    def config(self) -> Mapping:
        return {"function": self.function, "timeout": self.timeout, "cwd": self.cwd}

    def run(self, ctx, inputs: Mapping[str, Any]):
        try:
            data = json.dumps(dict(inputs)).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise NodeError(f"what goes to another process must be plain data: {exc}") from None
        env = {**(self.env if self.env is not None else os.environ),
               "PYTHONPATH": os.pathsep.join(path for path in sys.path if path), "PYTHONIOENCODING": "utf-8"}
        with tempfile.TemporaryFile() as errors:
            try:
                process = subprocess.Popen([sys.executable, "-c", _BOOT, self.function], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=errors, cwd=self.cwd or self.workspace, env=env)
            except OSError as exc:
                raise NodeError(f"cannot start a process for {self.function}: {exc}") from None
            expired = threading.Event()

            def overrun():
                expired.set()
                process.kill()
            timer = threading.Timer(self.timeout, overrun) if self.timeout else None
            if timer:
                timer.start()
            given: bytes | None = data
            try:
                while True:
                    try:
                        out, _ = process.communicate(given, timeout=0.05)
                        break
                    except subprocess.TimeoutExpired:
                        given = None
                        if ctx.stopping():
                            process.kill()
            finally:
                if timer:
                    timer.cancel()
                if process.poll() is None:
                    process.kill()
                process.wait()
            if expired.is_set():
                raise NodeError(f"{self.function} ran longer than {self.timeout:g} seconds and was ended")
            if ctx.stopping():
                return None
            try:
                said = json.loads(out.decode("utf-8"))
                if not isinstance(said, dict) or not said.keys() & {"value", "error"}:
                    raise ValueError("not an answer")
            except ValueError:
                errors.seek(0)
                last = errors.read()[-2000:].decode("utf-8", "replace").strip()
                raise NodeError(f"the process running {self.function} ended with {process.returncode} and no answer"
                                + (f": {last}" if last else "")) from None
        if "error" in said:
            raise NodeError(said["error"])
        result = said["value"]
        if result is None:
            return None
        if len(self._sends) == 1:
            return {self._sends[0]: result}
        if not isinstance(result, Mapping):
            raise NodeError(f"{self.id} has {len(self._sends)} output ports: its function must return a mapping of port to payload")
        return result
