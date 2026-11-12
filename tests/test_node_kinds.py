"""The node protocol: a host-registered kind runs a node through the same gates as a model node.

The kernel ships only the ``agent`` kind. The ``stamp`` kind here exists only in this test: it shows that a
node need not be a model, that its delivery is checked like any other, and that it releases nothing.
"""
import threading
import time

import pytest

from indeterminate.extensions import Extension, ExtensionError, TaskType
from indeterminate.graph.model import GraphError, fingerprint
from indeterminate.graph.nodes import AGENT, AgentNode, NodeKind, NodeKindError, NodeKinds
from indeterminate.graph.recovery import STATE_TEMPLATE
from indeterminate.mcp.daemon import Engine
from indeterminate.plan import Plan


def plan_with(kind):
    return {
        'task': '记录一次读数',
        'nodes': [
            {'id': 'lead', 'name': '主控', 'summary': '验收放行', 'role': 'lead'},
            {'id': 'probe', 'name': '读数', 'summary': '采集一次读数', 'role': 'worker', 'block': 'measure',
             'duty': '写出读数', 'accept': '有读数文件', 'kind': kind},
        ],
        'edges': [{'id': 'reading', 'from': 'probe', 'to': 'lead', 'kind': 'deliverable', 'start': False, 'label': '读数'}],
        'blocks': [{'id': 'measure', 'name': '采集', 'after': []}],
    }


class StampNode(NodeKind):
    """Writes one file and delivers it; no model involved."""
    kind = 'stamp'

    def __init__(self):
        self.calls = []

    def prepare(self, graph, node, instance):
        self.calls.append(('prepare', node.id, instance['id']))
        return 'stamp:' + instance['id']

    def activate(self, graph, handle):
        instance = handle.split(':', 1)[1]
        if ('activate', instance) in self.calls:
            return
        self.calls.append(('activate', instance))

        def work():
            node, round_ = instance.split(':r')
            ref = f'nodes/{node}/r{round_}/work/reading.txt'
            (graph.store.root / ref).write_text('42\n', encoding='utf-8')
            graph.deliver(instance, summary='读数为 42', files=[ref], state=STATE_TEMPLATE.replace('（无事项时写明无；只保存可核查工作记录）', '无'))
        threading.Thread(target=work, daemon=True).start()

    def rework(self, graph, handle, reason):
        self.calls.append(('rework', handle, reason))

    def stop(self, graph, handle, reason):
        self.calls.append(('stop', handle, reason))


def make(tmp_path, *extensions, name='w'):
    return Engine(tmp_path / name, tmp_path / (name + '-home'), demo=True, extensions=list(extensions))


def confirm(engine, pid):
    doc = engine.manager.graph(pid).store.load()
    engine._operator({'schema': 1, 'id': 'confirm-' + pid, 'command': 'confirm', 'args': {'project': pid, 'hash': fingerprint(doc['plan'])}})


def wait(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.03)
    return predicate()


def test_registry_keeps_agent_and_refuses_bad_kinds():
    reg = NodeKinds()
    assert list(reg) == [AGENT] and isinstance(reg.get(None), AgentNode) and isinstance(reg.get(AGENT), AgentNode)
    with pytest.raises(NodeKindError):
        reg.register(type('Fake', (NodeKind,), {'kind': AGENT})())
    with pytest.raises(NodeKindError):
        reg.register(type('Bad', (NodeKind,), {'kind': 'Bad Name'})())
    with pytest.raises(NodeKindError):
        reg.register(object())
    reg.register(StampNode())
    assert 'stamp' in reg and 'stamp' not in NodeKinds()


def test_plan_kind_field_is_optional_and_hash_neutral():
    plan = plan_with('stamp')
    del plan['nodes'][1]['kind']
    entity = Plan.from_dict(plan)
    assert entity.node('probe').kind == AGENT and entity.to_dict() == plan
    with pytest.raises(GraphError, match='节点种类'):
        Plan.from_dict(plan_with('Not A Kind'))
    bad = plan_with('stamp')
    bad['nodes'][0]['kind'] = 'stamp'
    with pytest.raises(GraphError, match='主控'):
        Plan.from_dict(bad)


def test_host_kind_runs_through_the_same_gates(tmp_path):
    stamp = StampNode()
    ext = Extension('stamping', nodes=[stamp], task_types=[TaskType('stamped', lambda task: plan_with('stamp'))])
    with make(tmp_path, ext) as engine:
        pid = engine.create_project('读数', '记录一次读数', task_type='stamped')['project']['id']
        assert stamp.calls == []                       # nothing runs before the operator confirms
        confirm(engine, pid)
        runtime = engine.manager.graph(pid)
        assert wait(lambda: runtime.run.state()['submissions'].get('probe', {}).get('status') == 'submitted')
        sub = runtime.run.state()['submissions']['probe']
        assert sub['files'] == ['nodes/probe/r1/work/reading.txt'] and sub['hashes']
        assert ('prepare', 'probe', 'probe:r1') in stamp.calls
        doc = runtime.store.load()
        assert not doc['run']['blocks']['measure'].get('released')   # delivering never releases a block
        assert not engine.manager.exists(pid, 'stamp:probe:r1')     # no model was created for the node
        with pytest.raises(GraphError):                              # a second delivery outside the round is refused
            runtime.deliver('probe:r1', summary='再交一次', files=['nodes/probe/r1/work/reading.txt'], state='x')
        runtime.rework('lead', 'probe', '再测一次')
        assert stamp.calls[-1] == ('rework', 'stamp:probe:r1', '再测一次')


def test_unknown_kind_is_not_started_and_kinds_do_not_leak_between_kernels(tmp_path):
    other_stamp = StampNode()
    with make(tmp_path, Extension('a', nodes=[other_stamp]), name='a') as with_stamp, make(tmp_path, name='b') as plain:
        assert 'stamp' in with_stamp.manager.node_kinds and 'stamp' not in plain.manager.node_kinds
        pid = plain.create_project('读数', '记录一次读数')['project']['id']
        runtime = plain.manager.graph(pid)
        runtime.store.draft('human', plan_with('stamp'))
        confirm(plain, pid)
        blocked = lambda: [e for e in plain.events_since(pid, 0, 500)['events'] if e['type'] == 'graph.start_blocked']
        assert wait(lambda: blocked())
        assert '未注册节点种类 stamp' in blocked()[0]['data']['error']
        assert other_stamp.calls == []
        assert 'probe' not in runtime.run.state()['submissions']


def test_extension_node_kind_conflicts_are_refused(tmp_path):
    with pytest.raises(ExtensionError):
        make(tmp_path, Extension('x', nodes=[StampNode(), StampNode()]))
    with pytest.raises(ExtensionError):
        make(tmp_path, Extension('x', nodes=[type('Agentish', (NodeKind,), {'kind': AGENT})()]), name='y')
