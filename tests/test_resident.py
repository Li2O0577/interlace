"""Resident agents: they stay while they have work, wait for each other, keep a task board, and finish."""
import threading
import time

import pytest

from interlacing import AgentNode, Engine, GraphBuilder, Journal, Port
from interlacing.models import ChatResult, FakeProvider


def call(name, **args):
    return {'id': f'c{time.monotonic_ns()}', 'name': name, 'args': args}


def last_tool(messages):
    return next((m['content'] for m in reversed(messages) if m['role'] == 'tool'), '')


def writer_policy(system, messages, tools):
    said = last_tool(messages)
    if not said:
        return ChatResult(tool_calls=[call('plan', items=['agree on a draft']), call('send_say', content='draft 1'),
                                      call('wait')])
    if '[heard]' in said and 'ok' in said:
        return ChatResult(tool_calls=[call('done', item=1), call('send_out', content='final'), call('finish')])
    if '[heard]' in said:
        return ChatResult(tool_calls=[call('send_say', content='draft 2'), call('wait')])
    return ChatResult(tool_calls=[call('wait')])


def critic_policy(system, messages, tools):
    said = last_tool(messages)
    if 'Nothing more can come' in said:
        return ChatResult(tool_calls=[call('finish')])
    text = said if said else messages[-1]['content']
    if 'draft 2' in text:
        return ChatResult(tool_calls=[call('send_say', content='ok'), call('wait')])
    if 'draft 1' in text:
        return ChatResult(tool_calls=[call('send_say', content='revise'), call('wait')])
    return ChatResult(tool_calls=[call('wait')])


def debate(workers):
    writer = AgentNode('writer', FakeProvider(writer_policy), resident=True,
                       inputs=[Port('task'), Port('heard', required=False)], outputs=[Port('say'), Port('out')])
    critic = AgentNode('critic', FakeProvider(critic_policy), resident=True, inputs=[Port('heard')], outputs=[Port('say')])
    graph = (GraphBuilder().add(writer, critic).connect('writer.say', 'critic.heard')
             .connect('critic.say', 'writer.heard', back=True, max_rounds=10)
             .input('in', 'writer.task').output('out', 'writer.out').compile())
    return writer, critic, Engine(graph, workers=workers).run({'in': 'write it'})


@pytest.mark.parametrize('workers', [0, 1, 2])
def test_two_resident_agents_talk_until_they_agree(workers):
    writer, critic, result = debate(workers)
    assert result.complete and result.outputs['out'] == ['final'] and result.rounds == {'writer': 1, 'critic': 1}
    assert writer.board['sent'] == {'say': 2, 'out': 1} and writer.board['items'] == [['agree on a draft', True]]
    assert critic.board['sent'] == {'say': 2}


def test_finishing_with_an_output_unsent_is_questioned_once():
    turns = []

    def policy(system, messages, tools):
        turns.append(last_tool(messages))
        return ChatResult(tool_calls=[call('finish')])
    agent = AgentNode('a', FakeProvider(policy), resident=True)
    result = Engine(GraphBuilder().add(agent).input('in', 'a.task').output('out', 'a.out').compile()).run({'in': 'go'})
    assert result.complete and result.outputs['out'] == [] and len(turns) == 2
    assert 'Nothing has been sent out of out' in turns[1]


def test_a_reply_without_tools_is_told_to_use_them():
    seen = []

    def policy(system, messages, tools):
        seen.append(messages[-1]['content'])
        if len(seen) == 1:
            return ChatResult(text='thinking aloud')
        return ChatResult(tool_calls=[call('send_out', content='x'), call('finish')])
    agent = AgentNode('a', FakeProvider(policy), resident=True)
    result = Engine(GraphBuilder().add(agent).input('in', 'a.task').output('out', 'a.out').compile()).run({'in': 'go'})
    assert result.outputs['out'] == ['x'] and 'Use your tools' in seen[1]
    assert {spec['name'] for spec in agent._specs} >= {'wait', 'note', 'plan', 'done', 'finish', 'send_out'}


def test_a_resident_agent_goes_on_after_the_process_ended(tmp_path):
    waiting = threading.Event()

    def before(system, messages, tools):
        if not last_tool(messages):
            return ChatResult(tool_calls=[call('note', text='halfway'), call('send_out', content='first half'),
                                          call('wait')])
        waiting.set()
        return ChatResult(tool_calls=[call('wait')])

    def keep_waiting(ctx, x):
        while ctx.checkpoint():
            time.sleep(0.01)
    from interlacing import FunctionNode

    def graph_with(agent):
        keeper = FunctionNode('k', keep_waiting, inputs=[Port('x')], outputs=[], context=True)
        return (GraphBuilder().add(agent, keeper).input('in', 'a.task').input('k', 'k.x')
                .output('out', 'a.out').compile())
    first = AgentNode('a', FakeProvider(before), resident=True)
    engine = Engine(graph_with(first), journal=Journal(tmp_path / 'j.jsonl'), workers=2)
    box = {}
    thread = threading.Thread(target=lambda: box.update(result=engine.run({'in': 'go', 'k': 1})))
    thread.start()
    deadline = time.monotonic() + 5
    while first.board['sent']['out'] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    engine.stop()                                         # as if the process ended while it waited
    thread.join(10)
    assert box['result'].outputs['out'] == ['first half']

    seen = []

    def after(system, messages, tools):
        seen.append(messages[-1]['content'])
        return ChatResult(tool_calls=[call('send_out', content='second half'), call('finish')])
    second = AgentNode('a', FakeProvider(after), resident=True)
    graph = graph_with(second)
    engine = Engine(graph, journal=Journal(tmp_path / 'j.jsonl'), workers=2)
    thread = threading.Thread(target=lambda: box.update(result=engine.resume()))
    thread.start()
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    engine.control('k', 'stop')
    thread.join(10)
    assert box['result'].outputs['out'] == ['first half', 'second half']
    assert 'taken up again' in seen[0] and 'halfway' in seen[0] and 'output out: sent 1 time' in seen[0]
    assert any(m.get('tool_calls') for m in second.messages)          # its conversation came back


def test_a_graph_file_makes_a_resident_agent():
    from interlacing.spec import Catalog, dump, load
    described = {'nodes': [{'id': 'a', 'kind': 'agent', 'model': 'm', 'resident': True}],
                 'inputs': {'in': 'a.task'}, 'outputs': {'out': 'a.out'}}
    graph = load(described, Catalog(models=lambda name: FakeProvider(lambda s, m, t: ChatResult(
        tool_calls=[call('send_out', content='hi'), call('finish')]))))
    assert graph.node('a').resident and dump(graph)['nodes'][0]['config']['resident'] is True
    assert Engine(graph).run({'in': 'x'}).outputs['out'] == ['hi']
