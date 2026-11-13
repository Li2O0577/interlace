"""Room to move: a clock on every activation, someone watching, members that appear when called for, and
graphs that say only what is different about them."""
import json
import math
import threading
import time

import pytest

from indeterminate.base import GraphBuilder, GraphError, Port
from indeterminate.engine import Engine, Journal, RunError
from indeterminate.methods import Chain, Governed, Pool, Tree
from indeterminate.models import ChatResult, FakeProvider
from indeterminate.nodes import AgentNode, FunctionNode, HumanNode
from indeterminate.protocols import DEFAULT
from indeterminate.runs import Run
from indeterminate.spec import Catalog, SpecError, dump, load, read

HERE = __name__
RELEASE = threading.Event()


def fn(id, function, inputs=('x',), outputs=('y',), **options):
    ports = lambda names: [p if isinstance(p, Port) else Port(p) for p in names]   # noqa: E731
    return FunctionNode(id, function, inputs=ports(inputs), outputs=ports(outputs), **options)


def echo(x):
    return x


def stuck(x):
    RELEASE.wait(10)
    return x


def shorten(messages):
    return messages[-2:]


# ---- the clock ------------------------------------------------------------------------------------------

@pytest.mark.parametrize('workers', [0, 3])
def test_an_activation_that_overruns_fails_the_run_and_is_not_waited_for(tmp_path, workers):
    RELEASE.clear()
    graph = Chain(fn('a', echo), fn('slow', stuck), fn('c', echo)).build(DEFAULT)
    began = time.monotonic()
    with pytest.raises(RunError, match='node slow ran longer than 0.2 seconds in round 1') as failure:
        Engine(graph, DEFAULT, workers=workers, time_limit=0.2, journal=Journal(tmp_path / 'j')).run({'in': 1})
    assert failure.value.node == 'slow' and time.monotonic() - began < 5
    kinds = [json.loads(line) for line in (tmp_path / 'j').read_text().splitlines()]
    assert [r['node'] for r in kinds if r['kind'] == 'act'] == ['a'] and kinds[-1]['kind'] != 'end'
    RELEASE.set()                                                   # the stuck node comes back; nothing is made of it
    time.sleep(0.1)
    assert len((tmp_path / 'j').read_text().splitlines()) == len(kinds)
    assert Engine(graph, DEFAULT, journal=Journal(tmp_path / 'j')).resume().last('out') == 1     # and the run goes on


def test_a_node_may_have_a_limit_of_its_own_and_a_person_has_none():
    RELEASE.set()
    slow = fn('slow', lambda x: time.sleep(0.3) or x)
    slow.time_limit = 5
    assert Engine(Chain(slow).build(DEFAULT), DEFAULT, time_limit=0.05).run({'in': 1}).last('out') == 1
    person = HumanNode('person', lambda request: time.sleep(0.3) or 'yes')
    assert person.time_limit == math.inf
    assert Engine(Chain(person).build(DEFAULT), DEFAULT, time_limit=0.05).run({'in': 1}).last('out') == 'yes'
    quick = fn('quick', lambda x: time.sleep(0.3) or x)
    quick.time_limit = 0.05
    with pytest.raises(RunError, match='node quick ran longer'):
        Engine(Chain(quick).build(DEFAULT), DEFAULT).run({'in': 1})
    assert Chain(quick).build(DEFAULT).describe()['nodes'][0]['time_limit'] == 0.05
    assert 'time_limit' not in Chain(fn('plain', echo)).build(DEFAULT).describe()['nodes'][0]
    with pytest.raises(ValueError):
        Engine(Chain(fn('a', echo)).build(DEFAULT), DEFAULT, time_limit=0)


def test_the_order_of_a_run_is_the_same_with_and_without_a_limit():
    def trace(limit):
        seen = []
        graph = (GraphBuilder().add(fn('a', echo), fn('b', echo), fn('c', echo), fn('join', lambda x, z: [x, z], inputs=('x', 'z')))
                 .connect('a.y', 'b.x', 'value').connect('a.y', 'c.x', 'value').connect('b.y', 'join.x', 'value').connect('c.y', 'join.z', 'value')
                 .input('in', 'a.x').output('out', 'join.y').compile(DEFAULT))
        Engine(graph, DEFAULT, time_limit=limit, observe=lambda e: seen.append((e['event'], e.get('node')))).run({'in': 1})
        return seen
    assert trace(None) == trace(30)


# ---- someone watching -----------------------------------------------------------------------------------

def test_an_observer_is_told_what_happens_as_it_happens():
    seen = []
    tool = fn('tool', lambda x: x * 2)
    caller = FunctionNode('caller', lambda ctx, x: ctx.call('double', x), context=True, inputs=[Port('x')],
                          outputs=[Port('y'), Port('double', call=True)])
    graph = (GraphBuilder().add(caller, tool).connect('caller.double', 'tool.x', 'call')
             .input('in', 'caller.x').output('out', 'caller.y').compile(DEFAULT))
    assert Engine(graph, DEFAULT, observe=seen.append).run({'in': 4}).last('out') == 8
    assert [(e['event'], e.get('node')) for e in seen] == [
        ('started', 'caller'), ('started', 'tool'), ('emitted', 'tool'), ('finished', 'tool'), ('emitted', 'caller'),
        ('finished', 'caller'), ('ended', None)]
    assert seen[1]['by'] == 'caller' and seen[4] == {'event': 'emitted', 'node': 'caller', 'round': 1, 'port': 'y', 'payload': 8}
    assert seen[0]['took'] == ['input:in#1'] and seen[-1] == {'event': 'ended', 'complete': True}


def test_an_observer_hears_of_a_failure_and_its_own_failure_is_not_lost():
    seen = []
    with pytest.raises(RunError, match='node a failed'):
        Engine(Chain(fn('a', lambda x: 1 / 0)).build(DEFAULT), DEFAULT, observe=seen.append).run({'in': 1})
    assert [e['event'] for e in seen] == ['started', 'failed', 'ended'] and 'division' in seen[1]['error']
    assert seen[-1]['complete'] is False and 'node a failed' in seen[-1]['error']
    with pytest.raises(RunError, match='the observer failed on started: no screen'):
        Engine(Chain(fn('a', echo)).build(DEFAULT), DEFAULT, observe=lambda e: (_ for _ in ()).throw(OSError('no screen'))).run({'in': 1})


def test_a_run_in_a_directory_can_be_watched(tmp_path):
    seen = []
    description = {'method': 'chain', 'nodes': [{'id': 'a', 'kind': 'function', 'function': f'{HERE}.echo', 'inputs': ['x'], 'outputs': ['y']}]}
    state = Run.create(tmp_path / 'run', description, allow=[HERE], observe=seen.append, time_limit=30).start({'in': 'x'})
    assert state['status'] == 'done' and [e['event'] for e in seen] == ['started', 'emitted', 'finished', 'ended']
    assert json.loads((tmp_path / 'run' / 'options.json').read_text())['time_limit'] == 30
    with pytest.raises(SpecError, match='positive number'):
        Run.create(tmp_path / 'r2', description, allow=[HERE], time_limit=-1)


# ---- members that appear when called for ----------------------------------------------------------------

def lead_of(plan):
    """A coordinator that sends what ``plan`` says after each thing it receives, and ends with all the replies."""
    replies = []

    def lead(ctx, task=None, reply=None):
        if reply is not None:
            replies.append(reply)
        for item in plan(task, replies):
            ctx.emit(*item)
    return FunctionNode('lead', lead, context=True, firing='any', inputs=[Port('task'), Port('reply', required=False)],
                        outputs=[Port('out'), Port('send')])


@pytest.mark.parametrize('workers', [0, 4])
def test_a_pool_has_ten_places_and_runs_the_three_that_were_called_up(workers):
    def plan(task, replies):
        if task is not None:
            return [('send', {'to': 'algebra', 'task': 'x^2'}), ('send', {'to': 'geometry', 'task': 'circle', 'role': 'draw'}), ('send', 'loose end')]
        if len(replies) == 3:
            return [('send', {'to': 'algebra', 'task': 'x^3'})]             # the same member again
        return [('out', sorted(replies, key=str))] if len(replies) == 4 else []
    made = []
    graph = Pool(lead_of(plan), lambda id: made.append(id) or fn(id, echo), size=10, max_rounds=3).build(DEFAULT)
    assert len(made) == 10 and len(graph.nodes) == 12
    result = Engine(graph, DEFAULT, workers=workers).run({'in': 'go'})
    assert result.last('out') == [{'from': 'algebra', 'reply': 'x^2'}, {'from': 'algebra', 'reply': 'x^3'},
                                  {'from': 'geometry', 'reply': {'task': 'circle', 'role': 'draw'}},
                                  {'from': 'member-3', 'reply': 'loose end'}]
    assert {id: n for id, n in result.rounds.items() if id.startswith('member-') and id != 'member-roster'} == \
        {'member-1': 2, 'member-2': 1, 'member-3': 1}                # seven places never ran


def test_a_full_pool_says_so_and_a_pool_survives_a_restart(tmp_path):
    def plan(task, replies):
        if task is not None:
            return [('send', {'to': name, 'task': name}) for name in ('a', 'b', 'c')]
        return [('out', replies[:])] if len(replies) == 3 else []
    graph = Pool(lead_of(plan), lambda id: fn(id, echo), size=2, max_rounds=1).build(DEFAULT)
    out = Engine(graph, DEFAULT).run({'in': 'go'}).last('out')
    assert {'from': 'c', 'error': 'all 2 places are taken (a, b); give the work to one of them'} in out
    assert {'from': 'a', 'reply': 'a'} in out and {'from': 'b', 'reply': 'b'} in out

    RELEASE.clear()
    def plan2(task, replies):
        if task is not None:
            return [('send', {'to': 'first', 'task': 1}), ('send', {'to': 'second', 'task': 2})]
        return [('out', [r['from'] for r in replies])] if len(replies) == 2 else []
    build = lambda: Pool(lead_of(plan2), lambda id: fn(id, stuck if id == 'member-2' else echo), size=3, max_rounds=1).build(DEFAULT)  # noqa: E731
    with pytest.raises(RunError, match='member-2 ran longer'):
        Engine(build(), DEFAULT, journal=Journal(tmp_path / 'j'), time_limit=0.2).run({'in': 'go'})
    RELEASE.set()
    time.sleep(0.05)
    assert Engine(build(), DEFAULT, journal=Journal(tmp_path / 'j')).resume().last('out') == ['first', 'second']


def test_a_pool_stands_where_a_tree_stands_and_says_what_it_lacks():
    def top(ctx, task=None, reply=None):
        ctx.emit('send', task) if task is not None else ctx.emit('out', reply)

    def sub(task, replies):
        return [('send', {'to': 'w', 'task': task})] if task is not None else [('out', replies[-1]['reply'])]
    root = FunctionNode('root', top, context=True, firing='any', inputs=[Port('task'), Port('reply', required=False)],
                        outputs=[Port('out'), Port('send')])
    graph = Tree(root, Pool(lead_of(sub), lambda id: fn(id, lambda x: x + 1), size=4, max_rounds=1, places='w'), max_rounds=1).build(DEFAULT)
    assert Engine(graph, DEFAULT).run({'in': 1}).last('out') == 2
    assert Governed.__mro__.index(Tree) and issubclass(Pool, Tree)
    with pytest.raises(GraphError, match='size of at least 1'):
        Pool(lead_of(sub), lambda id: fn(id, echo), size=0, max_rounds=1)
    with pytest.raises(GraphError, match='the id it was given'):
        Pool(lead_of(sub), lambda id: fn('other', echo), size=1, max_rounds=1).build(DEFAULT)
    with pytest.raises(GraphError, match="needs an output 'send'"):
        Pool(fn('lead', echo, inputs=('task', Port('reply', required=False)), outputs=('out',), firing='any'),
             lambda id: fn(id, echo), size=1, max_rounds=1).build(DEFAULT)


# ---- the agent's openings -------------------------------------------------------------------------------

def test_memory_may_be_a_function_that_keeps_the_conversation_in_hand():
    seen = []

    def policy(system, messages, tools):
        seen.append(len(messages))
        return ChatResult(text=f'answer {len(seen)}')
    agent = AgentNode('agent', FakeProvider(policy), memory=shorten)
    assert agent.config()['memory'] == f'{HERE}.shorten'
    for n in range(4):
        agent.run(type('Ctx', (), {'stopping': lambda self: False})(), {'task': f'question {n}'})
    assert seen == [1, 2, 2, 2] and [m['content'] for m in agent.messages] == ['answer 3', 'question 3', 'answer 4']
    from indeterminate.base import NodeError
    with pytest.raises(NodeError, match='memory must return the conversation'):
        AgentNode('bad', FakeProvider(policy), memory=lambda messages: None).run(type('Ctx', (), {'stopping': lambda self: False})(), {'task': 'x'})


def test_a_picture_that_arrives_is_shown_to_the_model_as_a_picture():
    seen = []
    model = FakeProvider(lambda system, messages, tools: seen.append(messages[-1]) or ChatResult(text='a cat'))
    picture = {'mime': 'image/png', 'data': 'aGVsbG8='}
    graph = Chain(AgentNode('eye', model)).build(DEFAULT)
    assert Engine(graph, DEFAULT).run({'in': {'frame': picture, 'note': 'what is this'}}).last('out') == 'a cat'
    assert seen[0]['image_data'] == [['image/png', 'aGVsbG8=']] and '[image 1]' in seen[0]['content'] and 'aGVsbG8=' not in seen[0]['content']
    Engine(Chain(AgentNode('eye', model)).build(DEFAULT), DEFAULT).run({'in': 'only words'})
    assert 'image_data' not in seen[1]


# ---- graphs that say only what is different -------------------------------------------------------------

def models(name):
    model = FakeProvider(lambda system, messages, tools: ChatResult(text=f'{name}|{system}|{messages[-1]["content"]}'))
    model.name = name
    return model


def test_defaults_fill_in_what_a_node_does_not_say_and_text_comes_from_files(tmp_path):
    (tmp_path / 'prompts').mkdir()
    (tmp_path / 'prompts' / 'lead.md').write_text('You lead.', encoding='utf-8')
    (tmp_path / 'secret.txt').write_text('no', encoding='utf-8')
    description = {'defaults': {'agent': {'model': 'fake:small', 'max_steps': 7, 'time_limit': 90}},
                   'method': 'chain', 'nodes': [{'id': 'lead', 'kind': 'agent', 'system': {'file': 'prompts/lead.md'}},
                                                {'id': 'check', 'kind': 'agent', 'model': 'fake:large', 'time_limit': 5}]}
    (tmp_path / 'g.json').write_text(json.dumps(description), encoding='utf-8')
    catalog = Catalog(models=models, files=tmp_path, defaults={'agent': {'model': 'fake:host', 'max_tokens': 99}})
    graph = load(description, catalog)
    lead, check = graph.node('lead'), graph.node('check')
    assert (lead.system, lead.max_steps, lead.max_tokens, lead.model.name, lead.time_limit) == ('You lead.', 7, 99, 'fake:small', 90)
    assert (check.system, check.max_steps, check.model.name, check.time_limit) == ('', 7, 'fake:large', 5)
    assert Engine(graph, DEFAULT).run({'in': 'hi'}).last('out') == 'fake:large||fake:small|You lead.|hi'
    written = dump(graph)
    assert 'defaults' not in written and {n['id']: n['config']['system'] for n in written['nodes']} == {'lead': 'You lead.', 'check': ''}
    assert load(written, Catalog(models=models)).digest == graph.digest       # the files are not needed again
    assert read(tmp_path / 'g.json', catalog).digest == graph.digest
    for name, why in (('../secret.txt', 'outside'), ('/etc/hostname', 'outside'), ('prompts/none.md', 'cannot be read')):
        with pytest.raises(SpecError, match=why):
            load({'nodes': [{'id': 'a', 'kind': 'agent', 'model': 'm', 'system': {'file': name}}]}, Catalog(models=models, files=tmp_path / 'prompts'))
    with pytest.raises(SpecError, match='allows no files'):
        load(description, Catalog(models=models))
    with pytest.raises(SpecError, match='time_limit must be a positive number'):
        load({'nodes': [{'id': 'a', 'kind': 'agent', 'model': 'm', 'time_limit': 'soon'}]}, Catalog(models=models))


def test_a_pool_and_an_agents_memory_can_be_written_as_data():
    description = {'method': 'pool', 'size': 5, 'max_rounds': 2, 'places': 'scout',
                   'root': {'id': 'lead', 'kind': 'function', 'function': f'{HERE}.pool_lead', 'context': True, 'firing': 'any',
                            'inputs': ['task', {'name': 'reply', 'required': False}], 'outputs': ['out', 'send']},
                   'member': {'kind': 'agent', 'model': 'fake:m', 'memory': f'{HERE}.shorten', 'system': 'scout'}}
    catalog = Catalog(models=models, allow=[HERE])
    graph = load(description, catalog)
    assert sorted(n.id for n in graph.nodes) == ['lead', 'scout-1', 'scout-2', 'scout-3', 'scout-4', 'scout-5', 'scout-roster']
    assert graph.node('scout-3').keep is shorten
    result = Engine(graph, DEFAULT).run({'in': 'north'})
    assert result.last('out') == {'from': 'one', 'reply': 'fake:m|scout|north'} and 'scout-2' not in result.rounds
    assert load(dump(graph), catalog).digest == graph.digest
    nested = {**description, 'member': {'method': 'chain', 'nodes': [{'id': 'a', 'kind': 'agent', 'model': 'fake:m'}]}}
    assert load(nested, catalog).node('scout-1').kind == 'graph'


def pool_lead(ctx, task=None, reply=None):
    ctx.emit('send', {'to': 'one', 'task': task}) if task is not None else ctx.emit('out', reply)
