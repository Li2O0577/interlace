"""A node that is an external program: the input goes to its standard input, its standard output comes out."""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from typing import Any, Mapping, Sequence

from ..base import Node, NodeError, Port, canonical

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
