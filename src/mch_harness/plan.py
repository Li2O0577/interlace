"""Plan entities: ``Plan`` holds ``Block``, ``Node`` and ``Edge``.

Each class checks its own fields; ``Plan`` checks what only the whole plan can know (references, launch and
block dependencies). An entity built from a stored definition writes back exactly that definition, so the plan
hash an operator confirmed stays the hash of the same plan. Feedback can cycle; launch dependencies cannot.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, fields
from pathlib import PurePosixPath

from .agents import kinds as agent_kinds

THINKING_LEVELS = ('', 'default', 'low', 'medium', 'high')
EDGE_KINDS = ('deliverable', 'review', 'feedback', 'rework')
STANCES = ('', 'proof')
LEAD = 'lead'  # the id of the one node the scheduler never starts: the plan's controller
LEAD_INSTANCE = LEAD + ':r1'  # the controller's single, permanent instance
HUMAN = 'human'  # the operator, as an actor
_RESERVED_NAMES = {'con', 'prn', 'aux', 'nul', *[f'com{i}' for i in range(1, 10)], *[f'lpt{i}' for i in range(1, 10)]}
_PRIVATE_ROOTS = ('nodes', 'state', 'agents', '.git')


class GraphError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise GraphError(message)


def fingerprint(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def check_keys(value, required, optional=()):
    require(isinstance(value, dict), '定义必须是对象')
    require(set(required) <= value.keys() and value.keys() <= set(required) | set(optional), '定义字段缺失或包含未知字段')


def check_id(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', value), '非法 ID')
    require(value.lower() not in _RESERVED_NAMES, '保留文件名不能用作 ID')


def check_text(value):
    require(isinstance(value, str) and value.strip(), '说明必须是非空文本')


def check_configuration(value):
    """Model and thinking level of a node or of a helper assignment."""
    require(isinstance(value.get('model', ''), str), 'model 必须为字符串')
    require(value.get('model', '') == '' or value['model'].strip(), 'model 不能为空白')
    require(value.get('thinking', '') in THINKING_LEVELS, '未知思考强度')


def check_acyclic(ids, edges):
    pending = set(ids)
    while pending:
        roots = {n for n in pending if not any(a in pending and b == n for a, b in edges)}
        require(roots, '启动依赖或任务块依赖存在环')
        pending -= roots


class Entity:
    """Shared by the plan's parts: the stored field names, and a faithful way back to the stored form."""

    REQUIRED: tuple = ()
    OPTIONAL: tuple = ()
    RENAMED: dict = {}  # stored key -> attribute name, where the key is not a usable attribute

    @classmethod
    def check(cls, data):
        raise NotImplementedError

    @classmethod
    def from_dict(cls, data, *, trusted=False):
        """``trusted`` skips the checks for a definition that was checked when it was stored."""
        if not trusted:
            cls.check(data)
        names = {f.name for f in fields(cls)}
        values = {}
        for key, value in data.items():
            name = cls.RENAMED.get(key, key)
            if name in names:
                values[name] = tuple(value) if isinstance(value, list) else value
        return cls(**values, given=tuple(data))

    def to_dict(self):
        """The definition as it was given: the same keys, in the same order, with the same values."""
        out = {}
        for key in self.given:
            value = getattr(self, self.RENAMED.get(key, key))
            out[key] = list(value) if isinstance(value, tuple) else value
        return out


@dataclass(frozen=True)
class Node(Entity):
    """One position in the plan. ``role`` names what runs there; ``block`` is None only for the controller."""

    id: str
    name: str
    summary: str
    role: str
    duty: str = ''
    block: str | None = None
    accept: str = ''
    stance: str = ''
    reads: tuple = ()
    tools: tuple | None = None
    model: str = ''
    thinking: str = ''
    helpers: bool = False
    given: tuple = field(default=(), repr=False)

    REQUIRED = ('id', 'name', 'summary', 'role')
    OPTIONAL = ('duty', 'block', 'accept', 'stance', 'reads', 'tools', 'model', 'thinking', 'helpers')

    @property
    def is_lead(self):
        return self.id == LEAD

    @classmethod
    def check(cls, data, blocks=None, kinds=None):
        """``blocks``: ids of the plan's blocks, when the node is checked as part of a plan."""
        check_keys(data, cls.REQUIRED, cls.OPTIONAL)
        check_configuration(data)
        for name in ('name', 'summary', 'role'):
            check_text(data[name])
        if data['id'] == LEAD:
            require(data.get('block') is None, '主控不属于任务块')
        else:
            block = data.get('block')
            require(not (kinds or agent_kinds.DEFAULT).of(data['role']).controller and isinstance(block, str) and (blocks is None or block in blocks),
                    '工作节点必须属于有效任务块且不能冒充主控')
            check_text(data.get('duty'))
            check_text(data.get('accept'))
        require(type(data.get('helpers', False)) is bool, 'helpers 必须为布尔值')
        require(data.get('stance', '') in STANCES, '未知 stance')
        reads = data.get('reads', [])
        require(isinstance(reads, list), 'reads 必须为列表')
        for path in reads:
            require(isinstance(path, str) and path and '\\' not in path and ':' not in path, '公共输入路径无效')
            p = PurePosixPath(path)
            require(not p.is_absolute() and '..' not in p.parts and p.parts and p.parts[0] not in _PRIVATE_ROOTS,
                    '公共输入不能扩大节点或内部状态的访问权限')
        tools = data.get('tools')
        require(tools is None or isinstance(tools, list) and all(isinstance(t, str) and t for t in tools), 'tools 必须为空或工具列表')


@dataclass(frozen=True)
class Block(Entity):
    """A group of nodes released together. It may start once every block in ``after`` is released."""

    id: str
    name: str
    after: tuple = ()
    coordinator: str = ''
    given: tuple = field(default=(), repr=False)

    REQUIRED = ('id', 'name', 'after')
    OPTIONAL = ('coordinator',)

    @classmethod
    def check(cls, data):
        check_keys(data, cls.REQUIRED, cls.OPTIONAL)
        check_text(data['name'])
        coordinator = data.get('coordinator', '')
        if coordinator:
            require(isinstance(coordinator, str), '任务块协调者必须是已有次主控节点')
        else:
            require(coordinator == '', '协调者应为空或次主控节点 ID')
        require(isinstance(data['after'], list) and all(isinstance(a, str) for a in data['after']), 'after 必须是 ID 列表')
        require(len(data['after']) == len(set(data['after'])), '无效前置任务块')


@dataclass(frozen=True)
class Edge(Entity):
    """An information flow from ``source`` to ``target``. A ``start`` edge is also a launch dependency."""

    id: str
    source: str
    target: str
    kind: str
    start: bool
    label: str
    given: tuple = field(default=(), repr=False)

    REQUIRED = ('id', 'from', 'to', 'kind', 'start', 'label')
    RENAMED = {'from': 'source', 'to': 'target'}

    @classmethod
    def check(cls, data):
        check_keys(data, cls.REQUIRED)
        require(isinstance(data['from'], str) and isinstance(data['to'], str) and data['from'] != data['to'], '无效信息流端点')
        require(data['kind'] in EDGE_KINDS and type(data['start']) is bool, '无效信息流类型')
        check_text(data['label'])


@dataclass(frozen=True)
class Plan(Entity):
    nodes: tuple = ()
    edges: tuple = ()
    blocks: tuple = ()
    task: str = ''
    given: tuple = field(default=(), repr=False)

    REQUIRED = ('nodes', 'edges', 'blocks')
    OPTIONAL = ('task',)
    PARTS = {'nodes': Node, 'edges': Edge, 'blocks': Block}

    # ------------------------------------------------------------------ stored form

    @classmethod
    def from_dict(cls, data, *, trusted=False, kinds=None):
        """``kinds``: the agent kinds the roles are resolved against (default: the process default)."""
        if not trusted:
            cls.check(data, kinds)
        parts = {name: tuple(kind.from_dict(item, trusted=True) for item in data[name]) for name, kind in cls.PARTS.items()}
        return cls(**parts, task=data.get('task', ''), given=tuple(data))

    def to_dict(self):
        out = {}
        for key in self.given:
            out[key] = [item.to_dict() for item in getattr(self, key)] if key in self.PARTS else getattr(self, key)
        return out

    @property
    def hash(self):
        return fingerprint(self.to_dict())

    @classmethod
    def check(cls, data, kinds=None):
        kinds = kinds or agent_kinds.DEFAULT
        check_keys(data, cls.REQUIRED, cls.OPTIONAL)
        for name in cls.PARTS:
            require(isinstance(data[name], list), name + ' 必须是列表')
            ids = []
            for item in data[name]:
                require(isinstance(item, dict) and 'id' in item, '缺少 ID')
                check_id(item['id'])
                ids.append(item['id'])
            require(len(ids) == len(set(ids)), 'ID 重复')
        nodes = {n['id']: n for n in data['nodes']}
        blocks = {b['id']: b for b in data['blocks']}
        require(LEAD in nodes and kinds.of(nodes[LEAD].get('role')).controller, '必须有内部主控 lead')
        for node in nodes.values():
            Node.check(node, blocks, kinds)
        for block in blocks.values():
            Block.check(block)
            if block.get('coordinator'):
                coordinator = block['coordinator'] in nodes and kinds.of(nodes[block['coordinator']]['role'])
                require(coordinator and coordinator.coordinates and not coordinator.controller,
                        '任务块协调者必须是已有次主控节点')
            require(set(block['after']) <= blocks.keys(), '无效前置任务块')
            require(any(n.get('block') == block['id'] for n in nodes.values()), '任务块不能为空')
        check_acyclic(blocks, [(a, b['id']) for b in blocks.values() for a in b['after']])
        startup = []
        for edge in data['edges']:
            Edge.check(edge)
            require(edge['from'] in nodes and edge['to'] in nodes, '无效信息流端点')
            if edge['start']:
                require(edge['to'] != LEAD, '主控不由图调度启动')
                startup.append((edge['from'], edge['to']))
        check_acyclic(nodes, startup)
        # Combine block gates with launch inputs to catch cross-block deadlocks.
        effective = list(startup)
        for block in blocks.values():
            for prior in block['after']:
                effective.extend((a['id'], z['id']) for a in nodes.values() for z in nodes.values()
                                 if a.get('block') == prior and z.get('block') == block['id'])
        check_acyclic(nodes, effective)
        if 'task' in data:
            check_text(data['task'])

    # ------------------------------------------------------------------ lookups

    def node(self, node_id):
        return next((n for n in self.nodes if n.id == node_id), None)

    def block(self, block_id):
        return next((b for b in self.blocks if b.id == block_id), None)

    def edge(self, edge_id):
        return next((e for e in self.edges if e.id == edge_id), None)

    @property
    def lead(self):
        return self.node(LEAD)

    def members(self, block_id):
        """The nodes of a block, in plan order."""
        return tuple(n for n in self.nodes if n.block == block_id)

    def inputs(self, node_id, *, start=None):
        """Edges that end at a node; ``start`` narrows to launch dependencies (True) or the rest (False)."""
        return tuple(e for e in self.edges if e.target == node_id and (start is None or e.start is start))

    def outputs(self, node_id):
        return tuple(e for e in self.edges if e.source == node_id)

    # ------------------------------------------------------------------ reach

    def downstream(self, seeds):
        """Every node that runs after any of ``seeds``: through launch edges and through later blocks."""
        out = set(seeds)
        while True:
            old = set(out)
            out |= {e.target for e in self.edges if e.start and e.source in out}
            touched = {n.block for n in self.nodes if n.id in out}
            later = {b.id for b in self.blocks if set(b.after) & touched}
            out |= {n.id for n in self.nodes if n.block in later}
            if old == out:
                return out

    def changed_from(self, before):
        """Sorted ids of the nodes a change from ``before`` to this plan touches, with all that runs after them."""
        seeds = set()
        for name in self.PARTS:
            a, b = ({x.id: x for x in getattr(plan, name)} for plan in (before, self))
            for key in a.keys() | b.keys():
                if a.get(key) == b.get(key):
                    continue
                for item, plan in ((a.get(key), before), (b.get(key), self)):
                    if not item:
                        continue
                    if name == 'nodes':
                        seeds.add(item.id)
                    elif name == 'edges':
                        seeds |= {item.source, item.target}
                    else:
                        seeds |= {n.id for n in plan.members(item.id)}
                        if item.coordinator:
                            seeds.add(item.coordinator)
        if (before.task if 'task' in before.given else None) != (self.task if 'task' in self.given else None):
            seeds |= {n.id for n in before.nodes + self.nodes}
        return sorted(before.downstream(seeds) | self.downstream(seeds))
