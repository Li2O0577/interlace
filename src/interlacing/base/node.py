"""The node: anything that takes information in and gives information out.

A node knows its ports and how to run once. It does not know which graph it is in, who is upstream or
downstream, or how its outputs travel: everything it touches outside itself goes through the ``Context`` the
engine hands it. An agent, a function, an external program, a device, a person and a whole sub-graph are all
nodes, with no difference in standing.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from abc import ABC, abstractmethod
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Iterable, Mapping

from .port import Port

ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class NodeError(ValueError):
    pass


class Invalid(NodeError):
    """What a node sent, or was sent, did not pass a check. ``problem`` says what is wrong, in words meant
    to be read by whoever can put it right. ``again`` is true when the node may correct it and send again
    within the same activation; when it is false the node fails."""

    def __init__(self, port: str, problem: str, again: bool = False):
        super().__init__(f"{port}: {problem}")
        self.port, self.problem, self.again = port, problem, again


class Firing(Enum):
    """When a node is activated."""

    ALL = "all"   # once every required input has arrived
    ANY = "any"   # each time any input arrives


class Context(ABC):
    """What the engine gives a node for one activation."""

    node: str     # the node's id
    round: int    # which activation of this node this is, from 1
    problems: list  # when the activation is run again because what it sent was not accepted: what was wrong
    within: Any     # when the graph runs inside a node of another: (node id, round) of each, outermost first
    key: Any        # names this activation of this node in this run, and is the same when it runs again after
                    # an interruption: give it to an outside service so that it does not do the same thing twice

    @abstractmethod
    def emit(self, port: str, payload: Any) -> None:
        """Send ``payload`` out of an output port now (a node may emit many times during one activation).
        Raises ``Invalid`` when the payload does not pass the port's checks; a node that can put it right
        catches that, reads ``problem`` and, when ``again`` is true, sends a corrected payload."""

    @abstractmethod
    def call(self, port: str, payload: Any) -> Any:
        """Send ``payload`` out of an output port whose edge is a call, wait, and return the other node's answer."""

    @abstractmethod
    def stopping(self) -> bool:
        """True once the run is being stopped: a long activation should return soon."""

    @abstractmethod
    def engine(self, graph: Any, protocols: Any = None, *, workers: int = 0, firm: bool = False) -> Any:
        """An engine for a graph that runs inside this activation. It stops when the outer run stops, keeps
        its own journal beside the outer one when there is one, tells the outer observer what happens in it,
        and works under the same time limit. ``go(inputs)`` runs it, or takes it up where an interrupted
        earlier attempt at this same activation left it. ``firm=True`` is for a graph that is not trusted:
        none of its nodes may give itself more time than the run allows."""

    async def acall(self, port: str, payload: Any) -> Any:
        """``call``, for a node whose ``run`` is a coroutine: the event loop stays free while the other node
        answers."""
        return self.call(port, payload)

    def paused(self) -> bool:
        """True while this node is paused and has not yet been told to end. For a node that hands its work to
        something outside (a program, a server) and wants to pause that too; most nodes need only
        ``checkpoint``."""
        return False

    def checkpoint(self) -> bool:
        """Say that a step is done (a call to a model, a tool, one item of a long list). While this node is
        paused it waits here; it returns False when the activation should end now, because the run is stopping
        or this node was stopped or restarted. A node that never calls it is paused between activations."""
        return not self.stopping()

    async def acheckpoint(self) -> bool:
        """``checkpoint``, for a node whose ``run`` is a coroutine."""
        return not self.stopping()

    def control(self, nodes: Any, action: str, *, team: bool = False) -> list:
        """Pause, resume, stop or restart nodes this node manages (``GraphBuilder.manage``); ``team`` takes in
        those they manage. Returns the nodes whose switch changed."""
        raise NotImplementedError("this context cannot control other nodes")

    def once(self, name: str, do: Any) -> Any:
        """Do something outside the graph once for this activation, however often the activation runs.

        ``do()`` is called and what it returns is kept under ``name``, in the journal when the run has one.
        When this activation runs again (the run was interrupted and taken up, or what the node sent was not
        accepted) ``once`` returns what was kept and does not call ``do`` again. What ``do`` returns must be
        plain data. If the process dies after ``do`` has acted and before its result is kept, it is done
        again: for that last gap, give the outside service ``ctx.key`` as well."""
        return do()

    def change(self, change: Any) -> None:
        """Change the graph this node runs in (a ``Change``): checked like a graph being built, and either
        refused with ``GraphError`` (nothing changes) or made at once. With a journal it is kept with this
        activation and made again when the run is taken up."""
        raise NotImplementedError("this context cannot change its graph")

    def use(self, **amounts: float) -> None:
        """Report what this activation used up, under names of the node's choosing (``tokens=1200``,
        ``calls=1``, ``seconds=3.5``). The run adds it up; when a total goes over the run's budget for that
        name the run fails, and this raises so that the node stops at once."""


def _ports(given: Iterable[Port] | Mapping[str, Port] | None, declared: Iterable[Port] | Mapping[str, Port]) -> Mapping[str, Port]:
    ports = list(given.values() if isinstance(given, Mapping) else given) if given is not None else list(declared)
    out: dict[str, Port] = {}
    for port in ports:
        if not isinstance(port, Port):
            raise NodeError(f"not a port: {port!r}")
        if port.name in out:
            raise NodeError(f"duplicate port: {port.name}")
        out[port.name] = port
    return MappingProxyType(out)


class Node(ABC):
    """Subclass, set ``kind``, declare ports (on the class or per instance) and implement ``run``.

    ``run`` receives the inputs that have arrived, by port name (a list for a ``many`` port). It may return a
    mapping of output port to payload, call ``ctx.emit`` any number of times, or both. Returning ends the
    activation.
    """

    kind: ClassVar[str] = ""                 # the name this kind of node is registered and stored under
    # A class may declare its ports as a tuple of Ports; on an instance they are a mapping from name to Port.
    inputs: Mapping[str, Port] = ()     # type: ignore[assignment]
    outputs: Mapping[str, Port] = ()    # type: ignore[assignment]
    firing: Firing = Firing.ALL
    repairs: int = 0                         # how often an output may be corrected, for ports that do not say
    time_limit: float | None = None          # the longest one activation may take, in seconds; None: the engine's

    def __init__(self, id: str, *, inputs: Iterable[Port] | None = None, outputs: Iterable[Port] | None = None,
                 firing: Firing | None = None):
        if not isinstance(id, str) or not ID.fullmatch(id):
            raise NodeError(f"invalid node id: {id!r}")
        if not type(self).kind:
            raise NodeError(f"{type(self).__name__} has no kind")
        self.id = id
        self.inputs = _ports(inputs, type(self).inputs)
        self.outputs = _ports(outputs, type(self).outputs)
        if firing is not None:
            self.firing = Firing(firing)

    def config(self) -> Mapping:
        """Plain data that sets this node apart from another of the same kind (it goes into the graph digest)."""
        return {}

    def describe(self) -> dict:
        own = self.__dict__.get("time_limit")
        return {"id": self.id, "kind": type(self).kind, "firing": self.firing.value,
                "inputs": [p.describe() for p in self.inputs.values()],
                "outputs": [p.describe() for p in self.outputs.values()], "config": dict(self.config()),
                **({"time_limit": own} if own is not None else {})}

    # ---- lifecycle: only run is required --------------------------------------------------------------

    def open(self, ctx: Context) -> Any:
        """Acquire what the node needs, once, before its first activation. May be ``async def``."""

    @abstractmethod
    def run(self, ctx: Context, inputs: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """One activation. It may be ``async def``: the engine then awaits it on its event loop, so that many
        such activations wait at once without a thread each, and one that overruns its time is cancelled."""

    @property
    def asynchronous(self) -> bool:
        """Whether an activation is a coroutine to be awaited. True when ``run`` is ``async def``."""
        return inspect.iscoroutinefunction(self.run)

    def close(self, ctx: Context) -> Any:
        """Release what ``open`` acquired. May be ``async def``."""

    def reset(self) -> None:
        """Forget what the node holds from earlier activations: it is being restarted."""

    def snapshot(self) -> Mapping:
        """Plain data from which ``restore`` can rebuild the node's own state after a restart."""
        return {}

    def restore(self, state: Mapping) -> None:
        pass

    def __repr__(self):
        return f"<{type(self).__name__} {self.id}>"


def waited(value: Any) -> Any:
    """``value``, or, when it is something to await (what an ``async def`` ``run`` or ``open`` gives), what it
    comes to: run to its end on an event loop of its own. Where a loop is already running in this thread that
    cannot be done, and the node must be awaited instead (``await ctx.acall``, ``await engine.arun``)."""
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_awaiting(value))
    close = getattr(value, "close", None)
    if close is not None:
        close()                             # it will never run: say so quietly
    raise NodeError("a coroutine cannot be waited for here, inside a running event loop: await it "
                    "(ctx.acall, engine.arun)")


async def _awaiting(value: Any) -> Any:
    return await value
