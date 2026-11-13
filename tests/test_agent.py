"""A model as a node: tools are call ports, several outputs are the model's to choose, consent is a node."""
import pytest

from interlace.models import ChatResult, FakeProvider
from interlace.base import Firing, GraphBuilder, GraphError, NodeError, Port
from interlace.engine import Engine, Journal, RunError
from interlace.methods import Chain, Governed, Loop, Tree
from interlace.nodes import AgentNode, FunctionNode, HumanNode, SinkNode, Tool
from interlace.protocols import DEFAULT


def fn(id, function, inputs=('x',), outputs=('y',), **options):
    ports = lambda names: [p if isinstance(p, Port) else Port(p) for p in names]   # noqa: E731
    return FunctionNode(id, function, inputs=ports(inputs), outputs=ports(outputs), **options)


def say(text='', **calls):
    """A model's answer: text, and tool calls given as name=args."""
    return ChatResult(text=text, tool_calls=[{'id': f'c{i}', 'name': name, 'args': args} for i, (name, args) in enumerate(calls.items())],
                      input_tokens=10, output_tokens=5)


def scripted(*answers):
    """A model that gives its answers in order; an answer may be a function of the messages so far."""
    left = list(answers)
    seen = []

    def policy(system, messages, tools):
        seen.append((system, [dict(m) for m in messages], [t['name'] for t in tools]))
        answer = left.pop(0)
        return answer(messages) if callable(answer) else answer
    provider = FakeProvider(policy)
    provider.seen = seen
    return provider


def run(graph, inputs=None, **options):
    return Engine(graph, DEFAULT, **options).run(inputs)


def test_an_agent_answers_with_its_closing_text():
    model = scripted(say('四'))
    agent = AgentNode('solver', model, system='你是计算器')
    result = run(Chain(agent).build(DEFAULT), {'in': '二加二'})
    assert result.outputs == {'out': ['四']}
    assert model.seen == [('你是计算器', [{'role': 'user', 'content': '二加二'}], [])]
    assert agent.usage == {'calls': 1, 'input_tokens': 10, 'output_tokens': 5}
    assert agent.describe()['kind'] == 'agent' and agent.config()['system'] == '你是计算器'


def test_a_tool_is_a_call_port_and_the_graph_decides_what_answers_it():
    model = scripted(say(lookup={'word': 'graph'}, move={'by': 3}), lambda messages: say(f"{messages[-2]['content']} / {messages[-1]['content']}"))
    agent = AgentNode('agent', model, tools=[Tool('lookup', '查词', {'type': 'object', 'properties': {'word': {'type': 'string'}}}),
                                              Tool('move', '移动机械臂')])
    assert [name for name, port in agent.outputs.items() if port.call] == ['lookup', 'move']
    position = [10]

    def move(args):
        position[0] += args['by']
        return {'position': position[0]}
    graph = GraphBuilder().add(agent, fn('dictionary', lambda x: f"{x['word']}: 图"), SinkNode('arm', move, answers=True)) \
        .connect('agent.lookup', 'dictionary.x', 'call').connect('agent.move', 'arm.x', 'call') \
        .input('in', 'agent.task').output('out', 'agent.out').compile(DEFAULT)
    result = run(graph, {'in': 'go'})
    assert result.outputs == {'out': ['graph: 图 / {"position":13}']} and position == [13]
    assert result.rounds == {'agent': 1, 'dictionary': 1, 'arm': 1}
    assert model.seen[0][2] == ['lookup', 'move']
    roles = [m['role'] for m in agent.messages]
    assert roles == ['user', 'assistant', 'tool', 'tool', 'assistant']


def test_a_tool_that_fails_or_does_not_exist_answers_the_model_with_the_error():
    model = scripted(say(risky={}), say(nothing={}), lambda messages: say(' | '.join(m['content'] for m in messages if m['role'] == 'tool')))
    agent = AgentNode('agent', model, tools=[Tool('risky')])
    graph = GraphBuilder().add(agent, fn('risky', lambda x: 1 / 0)).connect('agent.risky', 'risky.x', 'call') \
        .input('in', 'agent.task').output('out', 'agent.out').compile(DEFAULT)
    assert run(graph, {'in': 'try'}).outputs == {'out': ['error: division by zero | error: there is no tool nothing']}


def test_an_agent_that_never_stops_calling_tools_is_stopped():
    agent = AgentNode('agent', FakeProvider(lambda s, m, t: say(echo={})), tools=[Tool('echo')], max_steps=3)
    graph = GraphBuilder().add(agent, fn('echo', lambda x: 'again')).connect('agent.echo', 'echo.x', 'call').input('in', 'agent.task').compile(DEFAULT)
    with pytest.raises(RunError, match='node agent failed in round 1: the model was still calling tools after 3 steps'):
        run(graph, {'in': 'go'})


def test_json_output_and_what_an_agent_refuses():
    agent = AgentNode('agent', scripted(say('{"score": 7}'), say('seven')), json=True)
    assert run(Chain(agent).build(DEFAULT), {'in': 'rate'}).outputs == {'out': [{'score': 7}]}
    with pytest.raises(RunError, match="the model's answer is not JSON"):
        run(Chain(AgentNode('agent', scripted(say('seven')), json=True, repairs=0)).build(DEFAULT), {'in': 'rate'})
    with pytest.raises(NodeError, match='a model needs a chat method'):
        AgentNode('agent', object())
    with pytest.raises(NodeError, match='max_steps must be at least 1'):
        AgentNode('agent', scripted(), max_steps=0)
    with pytest.raises(NodeError, match='as tools, not as outputs'):
        AgentNode('agent', scripted(), outputs=[Port('t', call=True)])
    with pytest.raises(NodeError, match='send_ are taken'):
        AgentNode('agent', scripted(), outputs=[Port('a'), Port('b')], tools=[Tool('send_x')])


def test_memory_carries_over_between_activations_unless_turned_off():
    remembers = AgentNode('agent', FakeProvider(lambda s, messages, t: say(str(sum(1 for m in messages if m['role'] == 'user')))))
    forgets = AgentNode('agent', FakeProvider(lambda s, messages, t: say(str(sum(1 for m in messages if m['role'] == 'user')))), memory=False)
    for agent, expected in ((remembers, ['1', '2', '3']), (forgets, ['1', '1', '1'])):
        source = fn('source', lambda ctx: [ctx.emit('y', i) for i in range(3)] and None, inputs=(), context=True)
        assert run(Chain(source, agent).build(DEFAULT)).outputs == {'out': expected}
    assert len(remembers.snapshot()['messages']) == 6 and 'messages' not in forgets.snapshot()


def test_with_several_outputs_the_model_chooses_where_to_send():
    def reviewer(system, messages, tools):
        draft = messages[-1]['content'] if messages[-1]['role'] == 'user' else None
        if draft is None:
            return say('done')
        return say(send_ok={'content': draft}) if len(draft) >= 3 else say(send_again={'content': draft + '!'})
    review = AgentNode('review', FakeProvider(reviewer), outputs=[Port('again'), Port('ok')])
    graph = Loop(fn('draft', lambda x: x), review, max_rounds=5).build(DEFAULT)
    result = run(graph, {'in': 'a'})
    assert result.outputs == {'out': ['a!!']} and result.rounds == {'draft': 3, 'review': 3}
    assert [t for t in review._specs] and [t['name'] for t in review._specs] == ['send_again', 'send_ok']


def lead_policy(members):
    """A coordinator: hands the task to everyone, asks the operator before releasing, then answers."""
    def policy(system, messages, tools):
        last = messages[-1]
        users = [m['content'] for m in messages if m['role'] == 'user']
        if last['role'] == 'tool':
            if last['name'] == 'approve':
                return say(send_out={'content': 'released' if '"approved":true' in last['content'] else 'held'})
            return say('waiting')
        if len(users) == 1:
            return say(send_send={'content': users[0].split('\n', 1)[1]})
        if len(users) == 1 + members:
            return say(approve={'summary': sorted(u.split('\n', 1)[1] for u in users[1:])})
        return say('waiting')
    return policy


def governed(ask, b=lambda x: f'b did {x}'):
    lead = AgentNode('lead', FakeProvider(lead_policy(2)), inputs=[Port('task'), Port('reply', required=False)],
                     outputs=[Port('out'), Port('send')], tools=[Tool('approve', '请操作者批准放行')], firing=Firing.ANY)
    graph = Governed(lead, fn('a', lambda x: f'a did {x}'), fn('b', b),
                     operator=HumanNode('operator', ask), max_rounds=2).build(DEFAULT)
    return lead, graph


def test_a_governed_tree_asks_a_person_before_it_releases():
    asked = []
    lead, graph = governed(lambda request: (asked.append(request), {'approved': True})[1])
    result = run(graph, {'in': 'the job'})
    assert result.outputs == {'out': ['released']}
    assert asked == [{'summary': ['a did the job', 'b did the job']}]
    assert result.rounds == {'lead': 3, 'a': 1, 'b': 1, 'operator': 1}
    assert [e.id for e in graph.edges if e.protocol == 'call'] == ['lead.approve>operator.request']
    assert run(governed(lambda request: {'approved': False})[1], {'in': 'the job'}).outputs == {'out': ['held']}


def test_a_governed_tree_needs_someone_who_asks():
    hub = fn('hub', lambda **k: None, inputs=['task', Port('reply', required=False)], outputs=['out', 'send'], firing=Firing.ANY)
    with pytest.raises(GraphError, match="no coordinator has a call port 'approve'"):
        Governed(hub, fn('a', lambda x: x), operator=HumanNode('operator', lambda r: r), max_rounds=1).build(DEFAULT)
    assert Tree(hub, fn('a', lambda x: x), max_rounds=1).build(DEFAULT)


def test_an_agents_conversation_survives_a_restart(tmp_path):
    path = tmp_path / 'run.jsonl'
    asked, down = [], [True]

    def b(x):
        if down[0]:
            raise OSError('b lost its machine')
        return f'b did {x}'
    ask = lambda request: (asked.append(request), {'approved': True})[1]   # noqa: E731
    with pytest.raises(RunError, match='node b failed'):
        Engine(governed(ask, b)[1], DEFAULT, journal=Journal(path)).run({'in': 'the job'})
    down[0] = False
    lead, graph = governed(ask, b)                       # a new process: a new lead, with nothing in its head
    result = Engine(graph, DEFAULT, journal=Journal(path)).resume()
    assert result.outputs == {'out': ['released']} and result.complete
    assert asked == [{'summary': ['a did the job', 'b did the job']}]     # it still knew what a had replied before the restart
    assert result.rounds == {'lead': 3, 'a': 1, 'b': 1, 'operator': 1}
    assert [m['role'] for m in lead.messages][:2] == ['user', 'assistant'] and lead.usage['calls'] == 6


def test_a_turn_that_fails_leaves_nothing_in_the_conversation_and_fenced_json_is_read():
    replies = [ChatResult(tool_calls=[{'id': 'c1', 'name': 'look', 'args': {}}]), ChatResult(text='done')]
    agent = AgentNode('a', FakeProvider(lambda system, messages, tools: replies.pop(0)), tools=[Tool('look', 'Look.')])
    broken = [True]

    def look(request):
        if broken:
            raise KeyboardInterrupt()           # the process is interrupted in the middle of the turn
        return 'seen'
    graph = (GraphBuilder().add(agent, FunctionNode('look', look, inputs=[Port('request')], outputs=[Port('y')]))
             .connect('a.look', 'look.request', 'call').input('in', 'a.task').output('out', 'a.out').compile(DEFAULT))
    with pytest.raises(KeyboardInterrupt):
        Engine(graph, DEFAULT).run({'in': 'go'})
    assert agent.messages == []                                # no question without its answer was left behind
    broken.clear()
    replies[:] = [ChatResult(tool_calls=[{'id': 'c1', 'name': 'look', 'args': {}}]), ChatResult(text='done')]
    assert Engine(graph, DEFAULT).run({'in': 'go'}).last('out') == 'done'
    assert [m['role'] for m in agent.messages] == ['user', 'assistant', 'tool', 'assistant']

    fenced = AgentNode('j', FakeProvider(lambda system, messages, tools: ChatResult(text='```json\n{"n": 1}\n```\n')), json=True, repairs=0)
    assert Engine(GraphBuilder().add(fenced).input('in', 'j.task').output('out', 'j.out').compile(DEFAULT), DEFAULT).run({'in': 'x'}).last('out') == {'n': 1}
