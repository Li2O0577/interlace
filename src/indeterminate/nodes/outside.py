"""Nodes that stand for something outside the graph: a source of readings, a place to send results, a person.

They are thin on purpose. A camera, a robot arm or an operator's console is reached through the functions
given here; what the node adds is a place in the graph, ports, and a time to connect and disconnect.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from ..base import ANY, Node, NodeError, Port
from .function import named


def _callable(id: str, what: str, value, optional: bool = False):
    if value is None and optional:
        return None
    if not callable(value):
        raise NodeError(f"{id}: {what} is not callable: {value!r}")
    return value


class _Outside(Node):
    def __init__(self, id, *, inputs, outputs, connect, disconnect):
        super().__init__(id, inputs=inputs, outputs=outputs)
        self.connect = _callable(id, "connect", connect, True)
        self.disconnect = _callable(id, "disconnect", disconnect, True)

    def _lifecycle(self) -> dict:
        return {"connect": named(self.connect) if self.connect else None,
                "disconnect": named(self.disconnect) if self.disconnect else None}

    def open(self, ctx) -> None:
        if self.connect:
            self.connect()

    def close(self, ctx) -> None:
        if self.disconnect:
            self.disconnect()


class SourceNode(_Outside):
    """Brings readings in. ``read()`` returns an iterable (a list, a generator that waits for a device); each
    item is sent out of ``out`` as it comes. The node starts by itself and ends when the readings end or the
    run is stopped. ``connect`` and ``disconnect`` are called once, before the first reading and at the end."""

    kind = "source"

    def __init__(self, id: str, read: Callable[[], Iterable], *, schema=ANY, connect: Callable | None = None,
                 disconnect: Callable | None = None):
        super().__init__(id, inputs=[], outputs=[Port("out", schema)], connect=connect, disconnect=disconnect)
        self.read = _callable(id, "read", read)

    def config(self) -> Mapping:
        return {"read": named(self.read), **self._lifecycle()}

    def run(self, ctx, inputs: Mapping[str, Any]):
        for item in self.read():
            if ctx.stopping():
                break
            ctx.emit("out", item)


class SinkNode(_Outside):
    """Sends results out. ``write(value)`` is called for everything that arrives on ``x``. With
    ``answers=True`` the node also has an output ``out`` carrying what ``write`` returns, so that a command to
    a device can be called and answered."""

    kind = "sink"

    def __init__(self, id: str, write: Callable[[Any], Any], *, schema=ANY, answers: bool = False,
                 connect: Callable | None = None, disconnect: Callable | None = None):
        super().__init__(id, inputs=[Port("x", schema)], outputs=[Port("out")] if answers else [],
                         connect=connect, disconnect=disconnect)
        self.write, self.answers = _callable(id, "write", write), bool(answers)

    def config(self) -> Mapping:
        return {"write": named(self.write), "answers": self.answers, **self._lifecycle()}

    def run(self, ctx, inputs: Mapping[str, Any]):
        answer = self.write(inputs["x"])
        return {"out": answer} if self.answers else None


class HumanNode(Node):
    """A person. What arrives on ``request`` is put to them through ``ask(request)``, which returns when they
    have answered; the answer leaves through ``answer``. How the person is reached (a console, a page, a
    queue) is the business of ``ask``. With ``ask=None`` the node is only a place in the graph: whoever runs
    the graph sets ``node.ask`` before the run."""

    kind = "human"

    def __init__(self, id: str, ask: Callable[[Any], Any] | None = None, *, request=ANY, answer=ANY):
        super().__init__(id, inputs=[Port("request", request)], outputs=[Port("answer", answer)])
        self.ask = _callable(id, "ask", ask, True)
        self._given = ask is not None

    def config(self) -> Mapping:
        return {"ask": named(self.ask) if self._given else None}

    def run(self, ctx, inputs: Mapping[str, Any]):
        if self.ask is None:
            raise NodeError("nobody is there to ask: this node was given no ask")
        return {"answer": self.ask(inputs["request"])}
