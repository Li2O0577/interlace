"""A node that lives in another process or on another machine, and stays there between activations.

On the far side, any node is served: ``serve_node(node, port=7000)``, or ``interlacing node-serve
mypkg.arm:make --listen 0.0.0.0:7000`` from a shell. In the graph, ``RemoteNode("arm", "tcp://host:7000")``
stands for it: it asks the far side for its ports, and each activation is carried out there. Unlike a
``FolderNode``, which starts its program for every activation, a served node keeps running, and keeps what it
holds (an open device, a loaded model) from one activation to the next.

The connection carries one JSON object per line, in UTF-8:

    -> {"op": "hello", "token": ..., "version": 1}
    <- {"ok": true, "node": {"id": ..., "inputs": [{"name", "required", "many"}...], "outputs": [...]}}
    -> {"op": "run", "id": 1, "round": 3, "key": "run/arm#3", "inputs": {...}}
    <- {"id": 1, "emit": "out", "payload": ..., "ref": "1.2"}   any number, as the node emits; ``ref`` names it
    <- {"id": 1, "correct": "1.2", "payload": ...}         put this in the place of a message sent earlier
    <- {"id": 1, "use": {"seconds": 2.5}}                  what the node reports using
    <- {"id": 1, "done": true, "returned": {...} | null}   or {"id": 1, "error": "..."}
    -> {"op": "stop", "id": 1}                             the run is being stopped, or this node stopped
    -> {"op": "pause", "id": 1}                            this node is paused: wait at the next step
    -> {"op": "resume", "id": 1}                           go on from there

What crosses is plain data. A served node runs one activation at a time, whoever asks. Its context has
``emit`` and ``correct``, ``use``,
``stopping``, ``checkpoint`` (which waits while the node is paused), ``key``, ``round``, and ``engine`` for a graph run inside it; ``once`` does
the thing every time, because the far side keeps no journal; ``call`` is refused, since the far side cannot
reach back into the graph.

A server listens on this machine only (127.0.0.1) unless it is given a token, which every client must then
send. This is not encryption: across machines, run it inside a network that is already trusted, or through a
tunnel.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import socket
import threading
from typing import Any, Callable, Iterable, Mapping

from ..base import Context, Node, NodeError, Port, waited
from .function import as_node

VERSION = 1
LOCAL = ("127.0.0.1", "localhost", "::1")


def parse_address(address: str) -> tuple[str, int]:
    """``tcp://host:port`` (or ``host:port``) as ``(host, port)``."""
    text = address[len("tcp://"):] if address.startswith("tcp://") else address
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise NodeError(f"an address is tcp://host:port, not {address!r}")
    return host.strip("[]"), int(port)


def _send(stream, message: Mapping) -> None:
    stream.write((json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8"))
    stream.flush()


class _Lines:
    """Whole lines from a socket that has a timeout: a timeout loses nothing already received."""

    def __init__(self, sock: socket.socket):
        self.sock, self.buffer = sock, bytearray()

    def read(self) -> dict:
        while b"\n" not in self.buffer:
            data = self.sock.recv(65536)       # socket.timeout passes through, the buffer is kept
            if not data:
                raise OSError("the connection was closed")
            self.buffer += data
        line, _, rest = bytes(self.buffer).partition(b"\n")
        self.buffer = bytearray(rest)
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ValueError("not a message")
        return message

    def send(self, message: Mapping) -> None:
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        self.sock.sendall(data)


def _port_of(described: Mapping) -> Port:
    return Port(described["name"], required=bool(described.get("required", True)), many=bool(described.get("many", False)))


def _ports(ports: Iterable[Port]) -> list[dict]:
    return [{"name": port.name, "required": port.required, "many": port.many} for port in ports if not port.call]


# ---- the near side ---------------------------------------------------------------------------------------

class RemoteNode(Node):
    """The node served at ``address``. Its ports are the far node's, asked for when this is made, unless
    ``inputs`` and ``outputs`` are given here (then they also carry schemas, checked on this side).
    ``token`` is the server's token, if it has one. ``connect_timeout`` is how long to wait for the server."""

    kind = "remote"

    def __init__(self, id: str, address: str, *, token: str | None = None, inputs: Iterable[Port] | None = None,
                 outputs: Iterable[Port] | None = None, firing=None, connect_timeout: float = 10.0):
        self.address, self.token, self.connect_timeout = address, token, connect_timeout
        self.token_env: str | None = None      # where a graph file said the token is: kept when the graph is written out
        self.host, self.port = parse_address(address)
        self._socket: socket.socket | None = None
        self._stream: _Lines | None = None
        self._lock = threading.Lock()
        self._next = 0
        self._refs: dict[str, str] = {}        # what the far side calls a message it sent -> its name in the run
        if inputs is None or outputs is None:
            far = self._connect()
            inputs = inputs if inputs is not None else [_port_of(p) for p in far.get("inputs", [])]
            outputs = outputs if outputs is not None else [_port_of(p) for p in far.get("outputs", [])]
        super().__init__(id, inputs=inputs, outputs=outputs, firing=firing)
        if any(port.call for port in self.outputs.values()):
            raise NodeError(f"{id}: a node on the far side cannot make calls into the graph")

    def config(self) -> Mapping:
        return {"address": self.address, **({"token_env": self.token_env} if self.token_env else {})}

    def _connect(self) -> dict:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as exc:
            raise NodeError(f"cannot reach {self.address}: {exc}") from None
        stream = _Lines(sock)
        try:
            stream.send({"op": "hello", "token": self.token, "version": VERSION})
            said = stream.read()
        except (OSError, ValueError) as exc:
            sock.close()
            raise NodeError(f"{self.address} did not answer: {exc}") from None
        if not said.get("ok"):
            sock.close()
            raise NodeError(f"{self.address} refused: {said.get('error', 'no reason given')}")
        sock.settimeout(0.2)
        self._socket, self._stream = sock, stream
        return said.get("node", {})

    def _drop(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._socket = self._stream = None

    def open(self, ctx) -> None:
        if self._socket is None:
            self._connect()

    def close(self, ctx) -> None:
        self._drop()

    def run(self, ctx, inputs: Mapping[str, Any]):
        with self._lock:
            if self._socket is None:
                self._connect()
            self._next += 1
            id, stream = self._next, self._stream
            assert stream is not None
            try:
                stream.send({"op": "run", "id": id, "round": ctx.round, "key": str(ctx.key), "inputs": dict(inputs)})
            except (TypeError, ValueError) as exc:
                raise NodeError(f"what goes to {self.address} must be plain data: {exc}") from None
            except OSError as exc:
                self._drop()
                raise NodeError(f"lost {self.address}: {exc}") from None
            stopped = False
            while True:
                try:
                    message = stream.read()
                except (socket.timeout, TimeoutError):
                    if not stopped and ctx.paused():
                        # paused: the far side waits at its next step, and so does this activation, so that the
                        # run knows it is held; what the far side sends meanwhile waits in the connection
                        _tell(stream, "pause", id)
                        if ctx.checkpoint():
                            _tell(stream, "resume", id)
                    if not stopped and ctx.stopping():
                        stopped = True
                        _tell(stream, "stop", id)
                    continue
                except (OSError, ValueError) as exc:
                    self._drop()
                    raise NodeError(f"lost {self.address} during the activation: {exc}") from None
                if message.get("id") != id:
                    continue                    # the end of an earlier activation that was abandoned
                if "emit" in message:
                    sent = ctx.emit(message["emit"], message.get("payload"))
                    if message.get("ref") is not None and isinstance(sent, str):
                        self._refs[str(message["ref"])] = sent
                        while len(self._refs) > 1024:
                            del self._refs[next(iter(self._refs))]
                elif "correct" in message:
                    ref = str(message["correct"])
                    if ref not in self._refs:
                        raise NodeError(f"{self.address} corrected a message it has not sent, or sent too long "
                                        f"ago: {ref}")
                    ctx.correct(self._refs[ref], message.get("payload"))
                elif "use" in message:
                    ctx.use(**message["use"])
                elif "error" in message:
                    raise NodeError(f"{self.address}: {message['error']}")
                elif message.get("done"):
                    returned = message.get("returned")
                    return returned if isinstance(returned, Mapping) else None


def _tell(stream, op: str, id: int) -> None:
    try:
        stream.send({"op": op, "id": id})
    except OSError:
        pass                            # a lost connection shows up at the next read


# ---- the far side ----------------------------------------------------------------------------------------

class _FarContext(Context):
    """What a served node is given for one activation."""

    def __init__(self, node: Node, round: int, key: str, send: Callable[[dict], None], activation: Any = 0):
        self.node, self.round, self.key = node.id, round, key
        self._activation, self._sent = activation, 0
        self.problems: list = []
        self.within: tuple = ()
        self._send = send
        self.stop = threading.Event()
        self.going = threading.Event()      # cleared while the node is paused
        self.going.set()

    def emit(self, port: str, payload: Any) -> str:
        self._sent += 1
        ref = f"{self._activation}.{self._sent}"
        self._send({"emit": port, "payload": payload, "ref": ref})
        return ref

    def correct(self, sent: str, payload: Any) -> str:
        self._send({"correct": str(sent), "payload": payload})
        return sent

    def call(self, port: str, payload: Any) -> Any:
        raise NodeError("a served node cannot make calls into the graph it serves")

    def stopping(self) -> bool:
        return self.stop.is_set()

    def paused(self) -> bool:
        return not self.going.is_set() and not self.stop.is_set()

    def checkpoint(self) -> bool:
        while self.paused():
            self.going.wait(0.1)
        return not self.stopping()

    async def acheckpoint(self) -> bool:
        while self.paused():
            await asyncio.sleep(0.05)
        return not self.stopping()

    def engine(self, graph: Any, protocols: Any = None, *, workers: int = 0, firm: bool = False) -> Any:
        from ..engine import Engine
        return Engine(graph, protocols, workers=workers, stopping=self.stopping, firm=firm)

    def use(self, **amounts: float) -> None:
        self._send({"use": amounts})


def _tagged(send: Callable[[dict], None], id: Any) -> Callable[[dict], None]:
    """``send`` with every message marked as belonging to activation ``id``."""
    def tagged(message: dict) -> None:
        send({"id": id, **message})
    return tagged


class NodeServer:
    """Serves one node to whoever connects. ``port=0`` picks a free port: ``address`` says which."""

    def __init__(self, node: Any, *, host: str = "127.0.0.1", port: int = 0, token: str | None = None):
        if host not in LOCAL and not token:
            raise NodeError(f"a server that listens on {host} needs a token; without one it listens on 127.0.0.1 only")
        self.node = as_node(node)
        self.token = token
        self._listener = socket.create_server((host, port))
        self._busy = threading.Lock()          # one activation at a time
        self._opened = False
        self._closed = threading.Event()
        self._connections: list[socket.socket] = []
        self._started = False
        host_, port_ = self._listener.getsockname()[:2]
        self.address = f"tcp://{host_}:{port_}"

    def describe(self) -> dict:
        return {"id": self.node.id, "kind": type(self.node).kind, "inputs": _ports(self.node.inputs.values()),
                "outputs": _ports(self.node.outputs.values())}

    def start(self) -> "NodeServer":
        """Serve in the background, and return at once."""
        if self._started:
            return self
        self._started = True
        threading.Thread(target=self.serve_forever, name="node-server", daemon=True).start()
        return self

    def serve_forever(self) -> None:
        while not self._closed.is_set():
            try:
                sock, _ = self._listener.accept()
            except OSError:
                break
            self._connections.append(sock)
            threading.Thread(target=self._serve, args=(sock,), name="node-connection", daemon=True).start()

    def close(self) -> None:
        self._closed.set()
        for sock in [self._listener, *self._connections]:
            try:
                sock.shutdown(socket.SHUT_RDWR)    # wakes a thread reading it; close alone would not
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if self._opened:
            try:
                self.node.close(_FarContext(self.node, 0, "", lambda message: None))
            except Exception:   # noqa: BLE001 - closing is best effort
                pass

    def __enter__(self) -> "NodeServer":
        return self.start()

    def __exit__(self, *_) -> None:
        self.close()

    def _serve(self, sock: socket.socket) -> None:
        stream = sock.makefile("rwb")
        writing = threading.Lock()
        running: dict[int, _FarContext] = {}

        def send(message: dict) -> None:
            with writing:
                _send(stream, message)
        try:
            hello = json.loads(stream.readline() or b"null")
            if not isinstance(hello, dict) or hello.get("op") != "hello":
                return send({"error": "expected hello"})
            if self.token and not hmac.compare_digest(str(hello.get("token") or ""), self.token):
                return send({"error": "wrong token"})
            send({"ok": True, "node": self.describe(), "version": VERSION})
            for line in stream:
                message = json.loads(line)
                if message.get("op") in ("stop", "pause", "resume"):
                    ctx = running.get(message.get("id"))
                    if ctx is not None:
                        if message["op"] == "stop":
                            ctx.stop.set()
                        elif message["op"] == "pause":
                            ctx.going.clear()
                        else:
                            ctx.going.set()
                elif message.get("op") == "run":
                    id = message.get("id")
                    ctx = running[id] = _FarContext(self.node, int(message.get("round", 0)), str(message.get("key", "")),
                                                    _tagged(send, id), message.get("key") or id)
                    threading.Thread(target=self._run, args=(id, ctx, message.get("inputs") or {}, send, running),
                                     daemon=True).start()
        except (OSError, ValueError):
            pass
        finally:
            for ctx in list(running.values()):
                ctx.stop.set()               # the client went away: what it asked for is stopped
            try:
                sock.close()
            except OSError:
                pass

    def _run(self, id: int, ctx: _FarContext, inputs: Mapping, send, running: dict) -> None:
        try:
            with self._busy:
                if not self._opened:
                    self._opened = True
                    waited(self.node.open(ctx))
                returned = waited(self.node.run(ctx, dict(inputs)))
            if returned is not None and not isinstance(returned, Mapping):
                raise NodeError("run must return a mapping of output port to payload, or None")
            send({"id": id, "done": True, "returned": dict(returned) if returned is not None else None})
        except BaseException as exc:   # noqa: BLE001 - whatever the node raises goes back as its failure
            try:
                send({"id": id, "error": f"{type(exc).__name__}: {exc}"})
            except (OSError, ValueError, TypeError):
                pass
        finally:
            running.pop(id, None)


def serve_node(node: Any, *, host: str = "127.0.0.1", port: int = 0, token: str | None = None) -> NodeServer:
    """Serve ``node`` (a node, or a plain function) in the background; ``address`` says where."""
    return NodeServer(node, host=host, port=port, token=token).start()
