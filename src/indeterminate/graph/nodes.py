"""The node protocol: how a plan node runs, independent of what runs there.

The scheduler decides *when* a node may run (inputs submitted, block open, plan confirmed) and reserves an
instance for it: a numbered round with its own ``work/``, ``submit/`` and ``state/`` folders. It then hands the
instance to the node's kind, which decides *what* runs:

    prepare(graph, node, instance) -> handle   create the runner, not running yet; the handle is recorded
    activate(graph, handle)                     let it run (also called again after a crash, so: idempotent)
    rework(graph, handle, reason)               the controller sent the work back
    stop(graph, handle, reason)                 the controller ended the instance

A runner delivers through the same gate for every kind: ``GraphRun.submit`` checks the files, snapshots them
into ``submit/`` and records hashes. Submitting never releases a block; only the controller does.

The kernel ships one kind, ``agent``: a model instance whose role decides its tools. A host may register
others (a program run, an external input, a device) by subclassing ``NodeKind``; a plan names a node's kind
in the optional ``kind`` field, and a node of a kind this engine does not know is not started.
"""
from __future__ import annotations

import re

from ..plan import LEAD, Node

AGENT = "agent"


class NodeKindError(ValueError):
    pass


class NodeKind:
    kind: str = ""

    def check(self, node: Node) -> None:
        """Further checks of a node of this kind when its plan is drafted (raise GraphError to refuse)."""

    def prepare(self, graph, node: Node, instance: dict) -> str:
        raise NotImplementedError

    def activate(self, graph, handle: str) -> None:
        pass

    def rework(self, graph, handle: str, reason: str) -> None:
        raise NotImplementedError

    def stop(self, graph, handle: str, reason: str) -> None:
        raise NotImplementedError


class AgentNode(NodeKind):
    """A model instance. Its role (an agent kind) gives its default tools; the plan may narrow them."""

    kind = AGENT

    def prepare(self, graph, node, instance):
        manager = graph.manager
        tools = manager.graph_tools_for(node.role, list(node.tools) if node.tools is not None else None)
        extra = {'graph': {'node': node.id, 'round': instance['round'], 'plan_version': instance['plan_version']}}
        if node.thinking:
            extra['thinking'] = node.thinking
        return graph._spawn(instance['id'], node.role, node.duty, name=node.name, tools=tools, model=node.model, extra=extra)

    def activate(self, graph, handle):
        st = graph.manager._get(graph.pid, handle)
        st.rec['graph_start_ready'] = True
        graph.manager._save(st)

    def rework(self, graph, handle, reason):
        graph.manager.send(graph.pid, LEAD, handle, '图节点返工：' + reason)

    def stop(self, graph, handle, reason):
        graph.manager.control(graph.pid, LEAD, handle, 'stop', reason=reason)


class NodeKinds:
    """The node kinds one engine can run. ``agent`` is always there and cannot be replaced."""

    def __init__(self, kinds=()):
        self._kinds: dict[str, NodeKind] = {AGENT: AgentNode()}
        for kind in kinds:
            self.register(kind)

    def register(self, kind: NodeKind) -> NodeKind:
        if not isinstance(kind, NodeKind) or not isinstance(kind.kind, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', kind.kind):
            raise NodeKindError(f'{kind!r} 不是带合法名称的节点种类')
        if kind.kind in self._kinds:
            raise NodeKindError(f'节点种类 {kind.kind} 由内核保留或已经注册过')
        self._kinds[kind.kind] = kind
        return kind

    def get(self, name: str) -> NodeKind | None:
        return self._kinds.get(name or AGENT)

    def __contains__(self, name) -> bool:
        return name in self._kinds

    def __iter__(self):
        return iter(list(self._kinds))

    def copy(self) -> NodeKinds:
        out = NodeKinds()
        out._kinds = dict(self._kinds)
        return out


DEFAULT = NodeKinds()
