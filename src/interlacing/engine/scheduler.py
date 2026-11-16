"""When a node may run: envelopes go into mailboxes, ready nodes go into a queue.

An input port is ready when all its forward feeds hold an envelope, or when one of its back feeds does. A
node keeps a count of its required ports that are not ready; an arriving envelope changes that count for one
node and nothing else, so scheduling costs the same per message whatever the size of the graph.

Every method here is called with the engine's lock held.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Callable

from ..base import Envelope, Ref, digest
from .wiring import Feed, InPort, State

# how one envelope taken from a feed is turned into what the node sees: (feed, envelope body) -> value
Read = Callable[[Feed, Any], Any]


class Scheduler:
    __slots__ = ("ready", "stopped", "envelopes")

    def __init__(self):
        self.ready: deque[State] = deque()
        self.stopped = False      # once set nothing new is queued
        self.envelopes = 0        # how many envelopes the run has made

    def consider(self, state: State) -> None:
        """Queue the node if it can be activated now."""
        if state.running or state.queued or self.stopped or state.called:
            return
        if state.any:
            arrivals = state.arrivals
            while arrivals and not arrivals[0].box:   # a latest feed that was overwritten and already read
                arrivals.popleft()
            can = bool(arrivals)
        elif state.missing:
            can = False
        else:
            can = bool(state.optional_ready) or not state.started or any(p.port.required for p in state.ports.values())
        if can:
            state.queued = True
            self.ready.append(state)

    @staticmethod
    def refresh(port: InPort) -> None:
        ready = (bool(port.forward) and port.forward_full == len(port.forward)) or port.back_full > 0
        if ready != port.ready:
            port.ready = ready
            state = port.state
            if port.port.required:
                state.missing += -1 if ready else 1
            else:
                state.optional_ready += 1 if ready else -1

    def envelope(self, feed: Feed, body: Any, round: int, cause: tuple) -> Envelope:
        """The next envelope on ``feed``."""
        feed.sent += 1
        if isinstance(body, Ref):
            body_digest = body.digest
        else:
            try:
                body_digest = digest(body)
            except (TypeError, ValueError):
                body_digest = ""    # not plain data: it can travel inside the process, but it has no content hash
        self.envelopes += 1
        return Envelope(f"{feed.id}#{feed.sent}", feed.id, feed.sent, round, body, body_digest, cause)

    def place(self, feed: Feed, envelope: Envelope) -> None:
        """Put an envelope in its feed's mailbox and see whether the receiving node can now run."""
        box, port = feed.box, feed.port
        if feed.passive:
            box.clear()
            box.append(envelope)
            return
        if box and feed.latest:
            box[0] = envelope
        else:
            box.append(envelope)
            if len(box) == 1:
                if feed.back:
                    port.back_full += 1
                else:
                    port.forward_full += 1
                self.refresh(port)
        state = port.state
        if state.any:
            state.arrivals.append(feed)
        self.consider(state)

    def pop(self, feed: Feed) -> Envelope:
        envelope = feed.box.popleft()
        if not feed.box:
            port = feed.port
            if feed.back:
                port.back_full -= 1
            else:
                port.forward_full -= 1
            self.refresh(port)
        return envelope

    def take(self, state: State, read: Read) -> tuple[dict, tuple, tuple]:
        """The inputs of one activation, the ids of the envelopes they came in, and which of those were used up."""
        inputs: dict[str, Any] = {}
        cause: list[str] = []

        def pop(feed):
            envelope = self.pop(feed)
            cause.append(envelope.id)
            return read(feed, envelope.body)

        if state.any:
            while state.arrivals:
                feed = state.arrivals.popleft()
                if feed.box:    # a latest feed may have been overwritten and already read
                    value = pop(feed)
                    inputs[feed.port.port.name] = [value] if feed.port.port.many else value
                    break
        else:
            for name, port in state.ports.items():
                if not port.ready:
                    continue
                if port.forward and port.forward_full == len(port.forward):
                    values = [pop(feed) for feed in port.forward]
                else:
                    values = [pop(next(feed for feed in port.back if feed.box))]
                inputs[name] = values if port.port.many else values[0]
        took = tuple(cause)
        for port in state.passive:      # the newest value of each edge that does not trigger: read, and left there
            values = []
            for feed in port.passive:
                if feed.box:
                    cause.append(feed.box[0].id)
                    values.append(read(feed, feed.box[0].body))
            if values:
                inputs[port.port.name] = values if port.port.many else values[0]
        return inputs, tuple(cause), took
