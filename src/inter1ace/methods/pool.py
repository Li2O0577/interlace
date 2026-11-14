"""A pool: a coordinator with members that come into being when it calls for them."""
from __future__ import annotations

from typing import Callable

from ..base import Firing, GraphBuilder, GraphError, Node
from ..nodes import Roster
from .chain import sole_input, sole_output
from .tree import Tree


class Pool(Tree):
    """``Pool(lead, member, size=10, max_rounds=3)``: lead may call up as many as ten members, and usually
    calls up fewer.

    ``member(id)`` makes one member: a node with one input and one output (a method wrapped as a
    ``GraphNode`` will do). The graph holds ``size`` places, but a place does nothing until the coordinator
    gives it work: no activation, no request to a model, no conversation, nothing in the journal. A run that
    used three members ran three.

    The coordinator is as in ``Tree``: it fires on ``ANY``, takes its task on one input and replies on an
    optional ``reply``, sends work out of ``send`` and its result out of ``out``. What it sends decides who
    gets it:

    * ``{"to": "geometry", "task": ...}`` goes to the member called ``geometry``; a name not used before
      calls up a new member. Anything else in the mapping, a role for instance, reaches the member with the
      task, so one template serves members that do different things;
    * anything else calls up a new member each time.

    Replies arrive as ``{"from": name, "reply": ...}``. When every place is taken, a new name is answered
    with ``{"from": name, "error": ...}``. Each member may reply at most ``max_rounds`` times. A pool can
    stand where a tree can: as the child of a ``Tree`` or under ``Governed``.
    """

    name = "pool"

    def __init__(self, root: Node, member: Callable[[str], Node], *, size: int, max_rounds: int, places: str = "member",
                 reply: str = "reply", send: str = "send", result: str = "out", protocol: str = "value"):
        if not callable(member):
            raise GraphError(f"{root.id}: member must be a function that makes a node from an id")
        if type(size) is not int or size < 1:
            raise GraphError("a pool needs a size of at least 1")
        if type(max_rounds) is not int or max_rounds < 1:
            raise GraphError("a pool needs max_rounds of at least 1")
        self.root, self.children, self.max_rounds = root, (), max_rounds
        self.member, self.size, self.places = member, size, places
        self.reply, self.send, self.result, self.protocol = reply, send, result, protocol

    def _wire(self, builder: GraphBuilder):
        root, reply = self.root, self.reply
        builder.add(root)
        if root.firing is not Firing.ANY:
            raise GraphError(f"{root.id} coordinates others, so it must fire on any input (firing=Firing.ANY)")
        if reply not in root.inputs or root.inputs[reply].required:
            raise GraphError(f"{root.id} needs an optional input {reply!r} (required=False) for its members' replies")
        tasks = [name for name in root.inputs if name != reply]
        if len(tasks) != 1:
            raise GraphError(f"{root.id} needs exactly one input besides {reply!r} for its task, and it has {sorted(tasks)}")
        for name in (self.send, self.result):
            if name not in root.outputs:
                raise GraphError(f"{root.id} needs an output {name!r}")
        roster = Roster(f"{self.places}-roster", self.size)
        builder.add(roster)
        builder.connect((root.id, self.send), (roster.id, "task"), self.protocol)
        builder.connect((roster.id, "reply"), (root.id, reply), self.protocol, back=True,
                        max_rounds=self.size * (self.max_rounds + 1))
        for i in range(1, self.size + 1):
            node = self.member(f"{self.places}-{i}")
            if not isinstance(node, Node) or node.id != f"{self.places}-{i}":
                raise GraphError(f"member must return a node with the id it was given ({self.places}-{i})")
            target = sole_input(node)
            if target is None:
                raise GraphError(f"{node.id} takes no input and cannot be given work")
            builder.add(node)
            builder.connect((roster.id, f"m{i}"), (node.id, target), self.protocol)
            builder.connect((node.id, sole_output(node)), (roster.id, f"r{i}"), self.protocol, back=True,
                            max_rounds=self.max_rounds)
        return (root.id, tasks[0]), (root.id, self.result)
