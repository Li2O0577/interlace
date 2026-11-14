"""The block: a designed graph kept as a unit, to be used as often as it is needed.

A node is one thing in one graph: it has an id and a state of its own, so the same node cannot stand in two
places. A block is the design instead of the thing. It is a recipe that makes the graph anew each time, and
every use of it is a node of its own::

    review = Block(lambda: Loop(writer(), critic(), max_rounds=3), name="review")

    Chain(outline, review("first-pass"), review("second-pass"), publish)      # two reviews, each with its own state
    Fan(*review.times(4))                                                     # four side by side: review-1 .. review-4

A use of a block is a ``GraphNode``: outside, only the inner graph's inputs and outputs show. Inside, it is a
run of its own that belongs to the outer one (journaled, observed, timed node by node).

Blocks are joined into larger blocks without naming every node::

    pipeline = seq(clean, review, publish)        # one after another: a chain
    panel = par(review, review, review)           # the same input to each, their outputs as a list: a fan
    study = seq(clean, par(review, review), publish)

A part may be a block, or a function ``id -> Node`` that makes a node. A node that already exists may be used
too, but then the block can only be used once, because the node cannot be made again.
"""
from __future__ import annotations

from typing import Any, Callable

from ..base import Graph, GraphError, Method, Node, Protocols
from ..methods import Chain, Fan
from ..nodes import GraphNode
from ..protocols import DEFAULT

__all__ = ["Block", "par", "seq"]


class Block:
    """``recipe()`` returns a graph or a method, new each time it is called.

    ``block(id)`` is one use of the block: a node with that id. ``block.times(n)`` is n of them.
    ``protocols`` are the protocols its graphs are compiled and run with; ``workers`` the threads each use
    may run its nodes on.
    """

    def __init__(self, recipe: Callable[[], Graph | Method], *, name: str = "block", protocols: Protocols = DEFAULT,
                 workers: int = 0):
        if not callable(recipe):
            raise GraphError("a block is made from a function that returns a graph or a method")
        self.recipe, self.name, self.protocols, self.workers = recipe, name, protocols, workers
        self.uses = 0

    def __call__(self, id: str | None = None) -> GraphNode:
        self.uses += 1
        made = self.recipe()
        if not isinstance(made, (Graph, Method)):
            raise GraphError(f"the recipe of {self.name} must return a graph or a method, not {type(made).__name__}")
        return GraphNode(id or f"{self.name}-{self.uses}", made, self.protocols, workers=self.workers)

    def times(self, n: int, id: str | None = None) -> list[GraphNode]:
        """``n`` uses of the block, named ``<id>-1`` .. ``<id>-n`` (``id`` is the block's name unless given)."""
        if type(n) is not int or n < 1:
            raise GraphError("times needs a whole number of at least 1")
        return [self(f"{id or self.name}-{i}") for i in range(1, n + 1)]

    def graph(self) -> Graph:
        """The graph of one use, for looking at or for writing out."""
        made = self.recipe()
        return made.build(self.protocols) if isinstance(made, Method) else made

    def __repr__(self):
        return f"<Block {self.name}>"


def _part(part: Any, id: str, used: list) -> Node:
    """One part of a joined block, as a node with the given id."""
    if isinstance(part, Block):
        return part(id)
    if isinstance(part, Node):
        if part in used:
            raise GraphError(f"{part.id} is a node that already exists, so a block built on it can be used only once: "
                             f"give seq and par a function that makes the node, or a block")
        used.append(part)
        return part
    if callable(part):
        node = part(id)
        if not isinstance(node, Node):
            raise GraphError(f"a part must make a node, and {part!r} made {type(node).__name__}")
        return node
    raise GraphError(f"a part is a block, a function that makes a node, or a node; not {type(part).__name__}")


def _joined(method, parts: tuple, name: str, prefix: str, options: dict) -> Block:
    if not parts:
        raise GraphError(f"{name} needs at least one part")
    used: list = []
    return Block(lambda: method(*(_part(part, f"{prefix}{i}", used) for i, part in enumerate(parts, 1))), name=name, **options)


def seq(*parts: Any, name: str = "seq", **options) -> Block:
    """A block that runs its parts one after another, each taking what the one before gave. Inside, the
    parts are called ``s1``, ``s2`` and so on."""
    return _joined(Chain, parts, name, "s", options)


def par(*parts: Any, name: str = "par", **options) -> Block:
    """A block that gives the same input to every part and returns their outputs as a list. Inside, the
    parts are called ``p1``, ``p2`` and so on."""
    return _joined(Fan, parts, name, "p", options)
