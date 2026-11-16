"""The run's picture of a graph: a mailbox for every edge, a count of what each node still waits for.

``wire`` turns a compiled graph into these structures once, at the start of a run, and checks what can only
be checked against the protocols it is run with. Nothing here decides when a node runs; that is the
scheduler's business, and it works on what is built here.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any

from ..base import Delivery, Firing, Graph, Node, Protocols
from .result import RunError

INPUT = "input:"   # prefix of the pseudo edge id of a graph input


class Feed:
    """One edge (or one graph input) as the run sees it: its mailbox and how it delivers."""

    __slots__ = ("id", "port", "back", "latest", "stream", "passive", "reply", "protocol", "box", "sent", "limit")

    def __init__(self, id, port, back, protocol, limit):
        self.id, self.port, self.back, self.protocol, self.limit = id, port, back, protocol, limit
        delivery = protocol.delivery if protocol is not None else Delivery.QUEUE
        self.latest = delivery is Delivery.LATEST
        self.stream = delivery is Delivery.STREAM
        self.passive = protocol is not None and not protocol.triggers
        self.reply: str | None = None     # CALL only: the output port of the called node that holds the answer
        self.box: deque = deque()
        self.sent = 0


class InPort:
    """One input port of one node: the feeds that reach it and whether it is ready."""

    __slots__ = ("port", "state", "forward", "back", "passive", "forward_full", "back_full", "ready")

    def __init__(self, port, state):
        self.port, self.state = port, state
        self.forward: list[Feed] = []
        self.back: list[Feed] = []
        self.passive: list[Feed] = []    # feeds that do not trigger: read, never taken
        self.forward_full = 0     # forward feeds holding an envelope
        self.back_full = 0
        self.ready = False


class State:
    """One node as the run sees it."""

    __slots__ = ("node", "ports", "any", "missing", "optional_ready", "arrivals", "running", "queued", "started",
                 "opened", "round", "outs", "calls", "passive", "streams", "called", "since", "time_limit", "rejected", "delivered",
                 "inner")

    def __init__(self, node: Node):
        self.node = node
        self.ports: dict[str, InPort] = {}
        self.any = node.firing is Firing.ANY
        self.missing = 0            # required ports not ready
        self.optional_ready = 0
        self.arrivals: deque = deque()    # ANY only: feeds in the order their envelopes arrived
        self.running = self.queued = self.started = self.opened = False
        self.round = 0
        self.outs: dict[str, tuple] = {}  # output port -> (feeds, graph output names)
        self.calls: dict[str, Feed] = {}          # output port -> its call edge
        self.passive: list[InPort] = []           # input ports fed by edges that do not trigger
        self.streams: dict[str, Any] = {}         # feed id -> the stream this activation is writing
        self.called: threading.Lock | None = None  # set when the node is the target of a call: one call at a time
        self.since = 0.0                          # when the running activation began
        self.time_limit: float | None = None      # how long an activation may take, in seconds
        self.rejected: dict[str, int] = {}        # this activation: output port -> times a payload was given back
        self.delivered = False                    # this activation has sent something on
        self.inner: list = []                     # this activation: the journals of the runs it made inside itself

    def begin(self) -> None:
        """A new activation starts: forget what the last one did."""
        self.queued, self.running, self.started = False, True, True
        self.round += 1
        if self.rejected:
            self.rejected = {}
        self.delivered = False
        if self.inner:
            self.inner = []


class Wiring:
    """Every node's state, every edge's feed and every graph input's feed, by id or name."""

    __slots__ = ("states", "feeds", "inputs")

    def __init__(self, states: dict[str, State], feeds: dict[str, Feed], inputs: dict[str, Feed]):
        self.states, self.feeds, self.inputs = states, feeds, inputs


def wire(graph: Graph, protocols: Protocols) -> Wiring:
    states = {node.id: State(node) for node in graph.nodes}
    for state in states.values():
        state.ports = {name: InPort(port, state) for name, port in state.node.inputs.items()}
    feeds: dict[str, Feed] = {}
    for edge in graph.edges:
        protocol = protocols.get(edge.protocol)
        if protocol is None:
            raise RunError(f"edge {edge.id}: unknown protocol {edge.protocol!r}")
        target = states[edge.target.node]
        port = target.ports[edge.target.port]
        feed = feeds[edge.id] = Feed(edge.id, port, edge.back, protocol, edge.max_rounds)
        if protocol.delivery is Delivery.CALL:
            if edge.back:
                raise RunError(f"edge {edge.id}: a call cannot be a back edge")
            outputs = [name for name, out in target.node.outputs.items() if not out.call]
            feed.reply = outputs[0] if len(outputs) == 1 else "reply" if "reply" in outputs else None
            target.called = target.called or threading.Lock()
        elif feed.passive:
            if not feed.latest or edge.back:
                raise RunError(f"edge {edge.id}: a protocol that does not trigger must deliver the latest value, forward")
            port.passive.append(feed)
        else:
            (port.back if edge.back else port.forward).append(feed)
    inputs: dict[str, Feed] = {}
    for name, end in graph.inputs.items():
        port = states[end.node].ports[end.port]
        feed = inputs[name] = Feed(INPUT + name, port, False, None, None)
        port.forward.append(feed)
    exposed: dict[tuple, list[str]] = {}
    for name, end in graph.outputs.items():
        exposed.setdefault(tuple(end), []).append(name)
    for state in states.values():
        node = state.node
        state.missing = sum(1 for p in state.ports.values() if p.port.required)
        for name, port in state.ports.items():
            if port.passive:
                if port.forward or port.back or port.port.required:
                    raise RunError(f"{node.id}.{name} is fed by an edge that does not trigger: it must be optional "
                                   f"and take nothing else")
                state.passive.append(port)
            if state.called and (port.forward or port.back or port.passive):
                raise RunError(f"{node.id} is called, so nothing else may feed it ({name} is fed)")
        for name in node.outputs:
            out = tuple((feeds[e.id], node.outputs[name]) for e in graph.outgoing(node.id, name))
            calls = [feed for feed, _ in out if feed.protocol.delivery is Delivery.CALL]
            if calls:
                if len(out) != 1:
                    raise RunError(f"{node.id}.{name} makes a call, so exactly one edge may leave it")
                state.calls[name] = calls[0]
                out = ()
            state.outs[name] = (out, tuple(exposed.get((node.id, name), ())))
    return Wiring(states, {**feeds, **{feed.id: feed for feed in inputs.values()}}, inputs)
