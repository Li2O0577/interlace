"""The run's picture of a graph: a mailbox for every edge, a count of what each node still waits for.

``wire`` turns a compiled graph into these structures once, at the start of a run, and checks what can only
be checked against the protocols it is run with. Nothing here decides when a node runs; that is the
scheduler's business, and it works on what is built here.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any

from ..base import Delivery, Edge, Firing, Graph, Node, Protocols
from .result import RunError

INPUT = "input:"   # prefix of the pseudo edge id of a graph input
ACTIVE, PAUSED, STOPPED = "active", "paused", "stopped"     # what a node's switch is set to


class Feed:
    """One edge (or one graph input) as the run sees it: its mailbox and how it delivers."""

    __slots__ = ("id", "port", "back", "latest", "stream", "passive", "reply", "protocol", "box", "sent", "limit", "guard",
                 "gone")

    def __init__(self, id, port, back, protocol, limit):
        self.id, self.port, self.back, self.protocol, self.limit = id, port, back, protocol, limit
        delivery = protocol.delivery if protocol is not None else Delivery.QUEUE
        self.latest = delivery is Delivery.LATEST
        self.stream = delivery is Delivery.STREAM
        self.passive = protocol is not None and not protocol.triggers
        self.reply: str | None = None     # CALL only: the output port of the called node that holds the answer
        self.box: deque = deque()
        self.sent = 0
        self.guard: Any = None            # the receiving port's schema, when what arrives must be checked against it
        self.gone = False                 # the edge was taken out of the graph while the run went on


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
                 "inner", "status", "halt", "holding", "paused_at", "waiting", "late", "resuming", "resumed", "opening")

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
        self.status = ACTIVE                      # its switch: active, paused or stopped
        self.halt: str | bool = False             # this activation is to end at its next step: "stop" or "restart"
        self.holding: list = []                   # this activation: (feed, envelope) it took, to give back if it is cut
        self.paused_at = 0.0                      # when it was paused: that time does not count against its limit
        self.waiting = False                      # the running activation is waiting for a message (ctx.receive)
        self.late: deque = deque()                # feeds that received while it ran, in the order they did
        self.resuming = False                     # an activation that saved its progress is to go on from there
        self.resumed = False                      # the running activation goes on from saved progress
        self.opening: list = []                   # this activation: (feed, envelope) it started with

    def begin(self) -> None:
        """A new activation starts: forget what the last one did."""
        self.queued, self.running, self.started = False, True, True
        self.round += 1
        if self.rejected:
            self.rejected = {}
        self.delivered = False
        self.halt = False
        self.resumed = False
        if self.late:
            self.late.clear()
        if self.inner:
            self.inner = []


class Wiring:
    """Every node's state, every edge's feed and every graph input's feed, by id or name. Built once at the
    start of a run (``wire``) and changed only through the methods below, when the graph changes while it runs."""

    __slots__ = ("states", "feeds", "inputs", "protocols")

    def __init__(self, protocols: Protocols):
        self.states: dict[str, State] = {}
        self.feeds: dict[str, Feed] = {}      # edges and graph inputs
        self.inputs: dict[str, Feed] = {}
        self.protocols = protocols

    def add_node(self, node: Node) -> State:
        state = self.states[node.id] = State(node)
        state.ports = {name: InPort(port, state) for name, port in node.inputs.items()}
        state.missing = sum(1 for p in state.ports.values() if p.port.required)
        return state

    def add_edge(self, edge: Edge) -> Feed:
        protocol = self.protocols.get(edge.protocol)
        if protocol is None:
            raise RunError(f"edge {edge.id}: unknown protocol {edge.protocol!r}")
        target = self.states[edge.target.node]
        port = target.ports[edge.target.port]
        feed = self.feeds[edge.id] = Feed(edge.id, port, edge.back, protocol, edge.max_rounds)
        source = self.states[edge.source.node].node.outputs[edge.source.port]
        if not feed.stream and protocol.guarded(source, port.port):
            feed.guard = port.port.schema
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
            refresh(port)
        return feed

    def add_input(self, name: str, end) -> Feed:
        port = self.states[end.node].ports[end.port]
        feed = self.inputs[name] = self.feeds[INPUT + name] = Feed(INPUT + name, port, False, None, None)
        port.forward.append(feed)
        refresh(port)
        return feed

    def remove_edge(self, id: str) -> int:
        """Take an edge's feed out. Returns how many envelopes were waiting on it, which are dropped."""
        feed = self.feeds.pop(id)
        feed.gone = True
        port, waiting = feed.port, len(feed.box)
        for kind in (port.forward, port.back, port.passive):
            if feed in kind:
                kind.remove(feed)
        if feed.box and not feed.passive and (feed.protocol is None or feed.protocol.delivery is not Delivery.CALL):
            if feed.back:
                port.back_full -= 1
            else:
                port.forward_full -= 1
        feed.box.clear()
        state = port.state
        if state.arrivals:
            state.arrivals = deque(f for f in state.arrivals if f is not feed)
        if port in state.passive and not port.passive:
            state.passive.remove(port)
        refresh(port)
        return waiting

    def remove_node(self, id: str) -> None:
        del self.states[id]

    def settle(self, graph: Graph) -> None:
        """After nodes or edges came or went: check each node's feeds and work out where its outputs go."""
        exposed: dict[tuple, list[str]] = {}
        for name, end in graph.outputs.items():
            exposed.setdefault(tuple(end), []).append(name)
        feeds = self.feeds
        for state in self.states.values():
            node = state.node
            state.passive = []
            for name, port in state.ports.items():
                if port.passive:
                    if port.forward or port.back or port.port.required:
                        raise RunError(f"{node.id}.{name} is fed by an edge that does not trigger: it must be optional "
                                       f"and take nothing else")
                    state.passive.append(port)
                if state.called and (port.forward or port.back or port.passive):
                    raise RunError(f"{node.id} is called, so nothing else may feed it ({name} is fed)")
            calls: dict[str, Feed] = {}
            outs: dict[str, tuple] = {}
            for name in node.outputs:
                out = tuple((feeds[e.id], node.outputs[name]) for e in graph.outgoing(node.id, name))
                called = [feed for feed, _ in out if feed.protocol.delivery is Delivery.CALL]
                if called:
                    if len(out) != 1:
                        raise RunError(f"{node.id}.{name} makes a call, so exactly one edge may leave it")
                    calls[name] = called[0]
                    out = ()
                outs[name] = (out, tuple(exposed.get((node.id, name), ())))
            state.calls, state.outs = calls, outs     # whole new mappings: a node emitting now sees one or the other


def refresh(port: InPort) -> None:
    """Work out again whether ``port`` is ready, and change its node's counts when that changed."""
    ready = (bool(port.forward) and port.forward_full == len(port.forward)) or port.back_full > 0
    if ready != port.ready:
        port.ready = ready
        state = port.state
        if port.port.required:
            state.missing += -1 if ready else 1
        else:
            state.optional_ready += 1 if ready else -1


def wire(graph: Graph, protocols: Protocols) -> Wiring:
    wiring = Wiring(protocols)
    for node in graph.nodes:
        wiring.add_node(node)
    for edge in graph.edges:
        wiring.add_edge(edge)
    for name, end in graph.inputs.items():
        wiring.add_input(name, end)
    wiring.settle(graph)
    return wiring
