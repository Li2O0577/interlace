"""A tree: a coordinator hands work down to its children and takes their replies back up, level by level.
A star is a tree one level deep."""
from __future__ import annotations

from ..base import Firing, Graph, GraphBuilder, GraphError, Method, Node, Protocols
from ..nodes import as_node
from .chain import sole_input, sole_output


class Tree(Method):
    """``Tree(lead, a, Tree(b, b1, b2), max_rounds=3)``: lead coordinates a and b; b coordinates b1 and b2.

    A child is a node (a leaf: one input, one output) or another ``Tree``. A coordinator is a node that

    * fires on ``ANY``, so it takes its task and each reply one at a time, as they arrive;
    * has an optional input ``reply``, where its children's outputs come back, and one other input, its task;
    * sends work down through an output named after the child's id when it has one, otherwise through
      ``send``, which reaches every child that has no port of its own;
    * sends its result out of ``out``: to its own coordinator, or, for the root, out of the graph.

    Each child may reply at most ``max_rounds`` times; later replies are dropped and the run's ``limited``
    names the edge. The coordinator decides when it is done. The graph's input ``in`` is the root's task.
    """

    name = "tree"

    def __init__(self, root: Node, *children: "Node | Tree", max_rounds: int, reply: str = "reply",
                 send: str = "send", result: str = "out", protocol: str = "value"):
        root = as_node(root)
        children = tuple(child if isinstance(child, Tree) else as_node(child) for child in children)
        if not children:
            raise GraphError(f"{root.id}: a {self.name} needs at least one child")
        if type(max_rounds) is not int or max_rounds < 1:
            raise GraphError(f"a {self.name} needs max_rounds of at least 1")
        self.root, self.children, self.max_rounds = root, children, max_rounds
        self.reply, self.send, self.result, self.protocol = reply, send, result, protocol

    def build(self, protocols: Protocols | None = None) -> Graph:
        builder = GraphBuilder()
        task, result = self._wire(builder)
        return builder.input("in", task).output("out", result).compile(protocols)

    def _wire(self, builder: GraphBuilder):
        """Add this subtree; return where its task goes in and where its result comes out."""
        root, reply = self.root, self.reply
        builder.add(root)
        if root.firing is not Firing.ANY:
            raise GraphError(f"{root.id} coordinates others, so it must fire on any input (firing=Firing.ANY)")
        if reply not in root.inputs or root.inputs[reply].required:
            raise GraphError(f"{root.id} needs an optional input {reply!r} (required=False) for its children's replies")
        tasks = [name for name in root.inputs if name != reply]
        if len(tasks) != 1:
            raise GraphError(f"{root.id} needs exactly one input besides {reply!r} for its task, and it has {sorted(tasks)}")
        if self.result not in root.outputs:
            raise GraphError(f"{root.id} needs an output {self.result!r} for its result")
        for child in self.children:
            if isinstance(child, Tree):
                target, source = child._wire(builder)
            else:
                builder.add(child)
                port = sole_input(child)
                if port is None:
                    raise GraphError(f"{child.id} takes no input and cannot be given work by {root.id}")
                target, source = (child.id, port), (child.id, sole_output(child))
            down = target[0] if target[0] in root.outputs else self.send
            if down not in root.outputs:
                raise GraphError(f"{root.id} has no output for {target[0]}: it needs one named {target[0]!r} or {self.send!r}")
            builder.connect((root.id, down), target, self.protocol)
            builder.connect(source, (root.id, reply), self.protocol, back=True, max_rounds=self.max_rounds)
        return (root.id, tasks[0]), (root.id, self.result)


class Star(Tree):
    """``Star(hub, a, b, c, max_rounds=3)``: one coordinator and its direct children, as in ``Tree``."""

    name = "star"

    def __init__(self, hub: Node, *spokes: Node, **options):
        for spoke in spokes:
            if isinstance(spoke, Tree):
                raise GraphError("the spokes of a star are nodes; use Tree for more levels")
        super().__init__(hub, *spokes, **options)


class Governed(Tree):
    """A tree whose coordinators must ask a person before they act on certain things.

    ``Governed(lead, a, b, operator=person, max_rounds=3)`` is ``Tree(lead, a, b, max_rounds=3)`` with one
    addition: every coordinator that has a call port named ``approve`` is joined to ``operator`` through it.
    The coordinator asks with ``ctx.call("approve", request)`` (an agent: by using its ``approve`` tool) and
    gets the person's answer back before it goes on. Consent is a node in the graph like any other, so it is
    in the graph's digest and in the journal.
    """

    name = "governed"

    def __init__(self, root: Node, *children: "Node | Tree", operator: Node, approve: str = "approve", **options):
        super().__init__(root, *children, **options)
        self.operator, self.approve = operator, approve

    def build(self, protocols: Protocols | None = None) -> Graph:
        builder = GraphBuilder()
        task, result = self._wire(builder)
        builder.add(self.operator)
        target = sole_input(self.operator)
        if target is None:
            raise GraphError(f"{self.operator.id} takes no input and cannot be asked")
        asking = [tree.root for tree in self._trees() if tree.root.outputs.get(self.approve) and tree.root.outputs[self.approve].call]
        if not asking:
            raise GraphError(f"no coordinator has a call port {self.approve!r}: nothing would ever ask {self.operator.id}")
        for node in asking:
            builder.connect((node.id, self.approve), (self.operator.id, target), "call")
        return builder.input("in", task).output("out", result).compile(protocols)

    def _trees(self):
        stack = [self]
        while stack:
            tree = stack.pop()
            yield tree
            stack.extend(child for child in tree.children if isinstance(child, Tree))
