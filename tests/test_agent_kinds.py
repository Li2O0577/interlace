"""Agent kinds: the class hierarchy, reserved capabilities, per-engine registries, and no role literals."""
import ast
from pathlib import Path

import pytest

from mch_harness.agents import kinds
from mch_harness.agents.kinds import (Agent, AgentKinds, Coordinator, KindError, Keeper, Lead, Reviewer, SubLead, Worker,
                                      define)
from mch_harness.extensions import Extension, ExtensionError, register
from mch_harness.plan import GraphError, Plan
from mch_harness.graph.templates import starter

SRC = Path(__file__).resolve().parents[1] / 'src' / 'mch_harness'


def test_hierarchy_matches_the_design():
    assert issubclass(Lead, Coordinator) and issubclass(SubLead, Coordinator) and issubclass(Coordinator, Agent)
    assert all(issubclass(k, Agent) for k in kinds.DEFAULT.kinds())
    assert Lead.controller and not Lead.reports() and not Lead.step_limited() and not Lead.spawnable
    assert SubLead.coordinates and not SubLead.controller and SubLead.reports()
    assert Keeper.decides_files and Keeper.scheduler_only and not Keeper.spawnable
    assert not Reviewer.coordinates and 'code.run' not in Reviewer.tools


def test_registry_lookups():
    reg = AgentKinds([Keeper, Lead, Worker, Reviewer, SubLead])
    assert list(reg) == ['file_keeper', 'lead', 'worker', 'reviewer', 'sublead']
    assert reg.spawnable() == ['worker', 'reviewer', 'sublead']
    assert reg.assignable() == ['file_keeper', 'worker', 'reviewer', 'sublead']
    assert reg.title('lead') == '主控' and reg.title('ghost') == 'ghost'
    unknown = reg.of('ghost')
    assert issubclass(unknown, Agent) and list(unknown.tools) == reg.tools('ghost') == list(kinds.READ)
    assert not unknown.controller and not unknown.coordinates


def test_reserved_capabilities_cannot_be_claimed():
    class FakeLead(Worker):
        role = 'boss'
        controller = True

    class Spy(Worker):
        role = 'spy'
        decides_files = True

    class Overwrite(Worker):
        role = 'lead'

    reg = kinds.DEFAULT.copy()
    for bad in (FakeLead, Spy):
        with pytest.raises(KindError, match='保留'):
            reg.register(bad)
    with pytest.raises(KindError, match='保留'):
        reg.register(Overwrite, replace=True)
    with pytest.raises(KindError):
        reg.register(type('Bad', (Worker,), {'role': 'bad', 'thinking': 'max'}))
    with pytest.raises(KindError):
        reg.register(int)


def test_subclass_extends_and_registries_are_isolated():
    class Translator(Worker):
        role = 'translator'
        title = '翻译'
        tools = ('fs.read', 'fs.write')

    a, b = kinds.DEFAULT.copy(), kinds.DEFAULT.copy()
    a.register(Translator)
    assert 'translator' in a and 'translator' not in b and 'translator' not in kinds.DEFAULT
    assert a.of('translator') is Translator and 'translator' in a.spawnable()
    with pytest.raises(KindError, match='已经注册'):
        a.register(define('translator', ['fs.read']))
    a.register(define('worker', ['fs.read'], title='只读执行'), replace=True)
    assert a.tools('worker') == ['fs.read'] and list(a).index('worker') == list(b).index('worker')


def test_extensions_register_kinds_per_kernel():
    class Translator(Worker):
        role = 'translator'
        tools = ('fs.read',)

    tools = {'fs.read': object(), 'fs.write': object()}
    one = register(tools, [Extension('t', kinds=[Translator], roles={'summarizer': ['fs.read']})])
    two = register(tools, [])
    assert one.kinds.of('translator') is Translator and issubclass(one.kinds.of('summarizer'), Worker)
    assert 'translator' not in two.kinds and set(one.roles) == {'translator', 'summarizer'}
    for ext in (Extension('x', roles={'lead': ['fs.read']}), Extension('x', roles={'file_keeper': ['fs.read']}),
                Extension('x', kinds=[type('Boss', (Worker,), {'role': 'boss', 'controller': True})]),
                Extension('x', roles={'reader': ['nope.tool']}),
                Extension('x', kinds=[Translator], roles={'translator': ['fs.read']})):
        with pytest.raises(ExtensionError):
            register(tools, [ext])


def test_plan_resolves_roles_through_kinds():
    plan = starter('t')
    plan['nodes'].append({'id': 'coord', 'name': '协调', 'summary': '协调', 'role': 'sublead', 'block': 'work',
                          'duty': '协调', 'accept': '按时'})
    plan['blocks'][0]['coordinator'] = 'coord'
    Plan.from_dict(plan)
    plan['blocks'][0]['coordinator'] = 'worker'
    with pytest.raises(GraphError, match='协调者'):
        Plan.from_dict(plan)
    plan['blocks'][0]['coordinator'] = 'coord'
    reg = kinds.DEFAULT.copy()
    reg.register(define('sublead', ['fs.read'], base=Worker), replace=True)   # this engine's sublead does not coordinate
    with pytest.raises(GraphError, match='协调者'):
        Plan.from_dict(plan, kinds=reg)


ORCHESTRATION = ['graph/run.py', 'graph/runtime.py', 'graph/store.py', 'graph/scheduler.py', 'graph/access.py',
                 'graph/helpers.py', 'graph/coordination.py', 'agents/loop.py', 'agents/team.py', 'agents/tooling.py',
                 'agents/conversation.py', 'agents/runtime.py', 'kernel.py', 'tools/graph_tools.py']


@pytest.mark.parametrize('name', ORCHESTRATION)
def test_orchestration_layer_has_no_role_literals(name):
    """Capabilities come from kinds; the controller is named by a constant, not a string."""
    tree = ast.parse((SRC / name).read_text(encoding='utf-8'))
    roles = set(kinds.DEFAULT) | {'lead', 'sublead', 'file_keeper'}
    found = [(node.lineno, node.value) for node in ast.walk(tree)
             if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in roles]
    assert found == []
