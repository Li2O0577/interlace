"""Plan entities: faithful round trip, same hash, and one counterexample per validation rule."""
import copy

import pytest

from indeterminate.graph import model
from indeterminate.graph.templates import starter
from indeterminate.plan import Block, Edge, GraphError, Node, Plan, fingerprint


@pytest.fixture
def plan():
    p = starter('整理资料')
    p['nodes'].append({'id': 'coord', 'name': '次主控', 'summary': '协调整理块', 'role': 'sublead', 'block': 'delivery',
                       'duty': '协调', 'accept': '按时', 'model': 'api:m', 'thinking': 'high', 'helpers': True,
                       'reads': ['problem/'], 'tools': ['fs.read']})
    p['blocks'][1]['coordinator'] = 'coord'
    return p


def test_round_trip_keeps_keys_order_and_hash(plan):
    entity = Plan.from_dict(plan)
    assert entity.to_dict() == plan
    assert [list(n) for n in entity.to_dict()['nodes']] == [list(n) for n in plan['nodes']]
    assert entity.hash == fingerprint(plan)
    assert Plan.from_dict(entity.to_dict()).hash == entity.hash


def test_typed_access(plan):
    entity = Plan.from_dict(plan)
    assert entity.lead.is_lead and entity.lead.block is None
    assert isinstance(entity.node('coord'), Node) and entity.node('coord').reads == ('problem/',)
    assert isinstance(entity.block('delivery'), Block) and entity.block('delivery').after == ('work',)
    assert isinstance(entity.edge('check'), Edge) and entity.edge('check').source == 'worker'
    assert [n.id for n in entity.members('work')] == ['worker', 'inspector']
    assert [e.id for e in entity.inputs('inspector', start=True)] == ['check']
    assert [e.id for e in entity.outputs('inspector')] == ['feedback', 'report', 'pack']
    assert entity.node('missing') is None


def test_reach_matches_dict_functions(plan):
    entity = Plan.from_dict(plan)
    for seed in ('worker', 'inspector', 'delivery', 'coord', 'lead'):
        assert entity.downstream({seed}) == model.downstream(plan, {seed})
    changed = copy.deepcopy(plan)
    changed['nodes'][1]['duty'] = '换一种做法'
    assert Plan.from_dict(changed).changed_from(entity) == ['coord', 'delivery', 'inspector', 'worker']
    retask = copy.deepcopy(plan)
    retask['task'] = '新任务'
    assert set(Plan.from_dict(retask).changed_from(entity)) == {n['id'] for n in plan['nodes']}
    assert Plan.from_dict(plan).changed_from(entity) == []


def _set(path, value):
    def apply(p):
        target = p
        for key in path[:-1]:
            target = target[key]
        if value is _DELETE:
            del target[path[-1]]
        else:
            target[path[-1]] = value
    return apply


_DELETE = object()

BAD = {
    'unknown plan field': (_set(['extra'], 1), '字段'),
    'nodes not list': (_set(['nodes'], {}), '列表'),
    'bad id': (_set(['nodes', 1, 'id'], 'Worker'), 'ID'),
    'reserved id': (_set(['nodes', 1, 'id'], 'con'), '保留'),
    'duplicate id': (_set(['nodes', 2, 'id'], 'worker'), '重复'),
    'no lead': (_set(['nodes', 0, 'role'], 'worker'), '主控'),
    'lead in block': (_set(['nodes', 0, 'block'], 'work'), '主控'),
    'worker posing as lead': (_set(['nodes', 1, 'role'], 'lead'), '冒充'),
    'worker without block': (_set(['nodes', 1, 'block'], 'nowhere'), '任务块'),
    'missing duty': (_set(['nodes', 1, 'duty'], ' '), '非空'),
    'blank model': (_set(['nodes', 1, 'model'], '  '), 'model'),
    'unknown thinking': (_set(['nodes', 1, 'thinking'], 'max'), '思考'),
    'helpers not bool': (_set(['nodes', 1, 'helpers'], 1), 'helpers'),
    'reads escapes': (_set(['nodes', 1, 'reads'], ['../x']), '公共输入'),
    'reads private': (_set(['nodes', 1, 'reads'], ['nodes/other']), '公共输入'),
    'tools not list': (_set(['nodes', 1, 'tools'], 'fs.read'), 'tools'),
    'coordinator not sublead': (_set(['blocks', 1, 'coordinator'], 'worker'), '协调者'),
    'coordinator wrong type': (_set(['blocks', 1, 'coordinator'], None), '协调者'),
    'unknown prior block': (_set(['blocks', 1, 'after'], ['nowhere']), '前置'),
    'empty block': (lambda p: p['blocks'].append({'id': 'empty', 'name': '空', 'after': []}), '不能为空'),
    'block cycle': (_set(['blocks', 0, 'after'], ['delivery']), '环'),
    'edge to self': (_set(['edges', 0, 'to'], 'worker'), '端点'),
    'edge to unknown': (_set(['edges', 0, 'to'], 'ghost'), '端点'),
    'edge kind': (_set(['edges', 0, 'kind'], 'magic'), '类型'),
    'start not bool': (_set(['edges', 0, 'start'], 1), '类型'),
    'launch cycle': (_set(['edges', 1, 'start'], True), '环'),
    'lead launched': (_set(['edges', 2, 'start'], True), '主控'),
    'blank task': (_set(['task'], ''), '非空'),
    'missing label': (_set(['edges', 0, 'label'], _DELETE), '字段'),
}


@pytest.mark.parametrize('name', list(BAD))
def test_each_rule_rejects_with_same_message_as_dict_validator(plan, name):
    change, match = BAD[name]
    bad = copy.deepcopy(plan)
    change(bad)
    with pytest.raises(GraphError, match=match) as entity_error:
        Plan.from_dict(bad)
    with pytest.raises(GraphError) as dict_error:
        model.validate(bad)
    assert str(entity_error.value) == str(dict_error.value)


def test_cross_block_launch_deadlock_rejected(plan):
    bad = copy.deepcopy(plan)
    # delivery waits for block work, and a worker in work waits on delivery's output: deadlock.
    bad['edges'].append({'id': 'back', 'from': 'delivery', 'to': 'worker', 'kind': 'deliverable', 'start': True, 'label': 'x'})
    with pytest.raises(GraphError, match='环'):
        Plan.from_dict(bad)


def test_entities_are_immutable(plan):
    entity = Plan.from_dict(plan)
    with pytest.raises(AttributeError):
        entity.lead.role = 'worker'
    assert model.validate(plan) is not plan
