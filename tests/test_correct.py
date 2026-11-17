"""Corrections: a node takes back what it sent and puts something else in its place (ctx.correct)."""
import pytest

from interlacing import Engine, FunctionNode, GraphBuilder, Journal, Port
from interlacing.spec import dump, load


def sender(fix, *, wait=True):
    """Sends 1, waits until the receiver has read it (when ``wait``), then corrects it to ``fix``."""
    def run(ctx, x, ack=None):
        sent = ctx.emit('out', 1)
        if wait:
            ctx.receive('ack')
        for value in fix:
            assert ctx.correct(sent, value) == sent
        return None
    return FunctionNode('s', run, inputs=[Port('x'), Port('ack', required=False)], outputs=[Port('out')], context=True)


def doubler(rule, seen):
    def run(ctx, v):
        seen.append((v, ctx.replaces('v')))
        return {'out': v * 2, 'ack': True}
    node = FunctionNode('d', run, inputs=[Port('v')], outputs=[Port('out'), Port('ack')], context=True)
    node.on_correction = rule
    return node


def chain(rule, seen, fix=(5,), wait=True, sink=None):
    builder = (GraphBuilder().add(sender(fix, wait=wait), doubler(rule, seen))
               .connect('s.out', 'd.v').connect('d.ack', 's.ack', back=True, max_rounds=10)
               .input('in', 's.x').output('sent', 's.out').output('out', 'd.out'))
    if sink is not None:
        builder.add(sink).connect('d.out', 'k.v').output('total', 'k.out')
    return builder.compile()


def adder(seen):
    def run(v):
        seen.append(v)
        return v + 100
    return FunctionNode('k', run, inputs=[Port('v')], outputs=[Port('out')])


@pytest.mark.parametrize('workers', [0, 1, 4])
def test_a_message_not_yet_read_is_replaced(workers):
    seen = []
    result = Engine(chain('rerun', seen, wait=False), workers=workers).run({'in': 0})
    # read before the correction (several workers) or after it: either way the receiver ends on the correction
    assert seen[-1] == (5, None) and result.outputs['out'][-1] == 10
    assert result.outputs['sent'] == [5]


@pytest.mark.parametrize('workers', [0, 1, 4])
def test_a_program_that_read_it_runs_again_and_what_follows_is_corrected_too(workers):
    seen, sunk = [], []
    result = Engine(chain('rerun', seen, sink=adder(sunk)), workers=workers).run({'in': 0})
    assert seen == [(1, None), (5, None)]
    assert result.outputs['out'] == [10] and result.outputs['total'] == [110]      # in place, not one more
    assert sunk[-1] == 10 and result.complete


def test_a_node_that_is_told_gets_the_correction_as_a_message_of_its_own():
    seen = []
    result = Engine(chain('tell', seen), workers=1).run({'in': 0})
    assert seen[0] == (1, None) and seen[1][0] == 5 and seen[1][1].startswith('s.out>d.v#')
    assert result.outputs['out'] == [2, 10]


def test_a_node_that_ignores_corrections_is_left_as_it_was():
    seen = []
    result = Engine(chain('ignore', seen), workers=1).run({'in': 0})
    assert seen == [(1, None)] and result.outputs['out'] == [2] and result.outputs['sent'] == [5]


def test_the_last_correction_counts():
    seen = []
    result = Engine(chain('rerun', seen, fix=(5, 7)), workers=1).run({'in': 0})
    assert seen[0] == (1, None) and seen[-1] == (7, None) and result.outputs['out'] == [14]


def test_only_what_the_node_itself_sent_can_be_corrected():
    def run(ctx, x):
        ctx.correct('out#3', 1)
    node = FunctionNode('n', run, inputs=[Port('x')], outputs=[Port('out')], context=True)
    graph = GraphBuilder().add(node).input('in', 'n.x').compile()
    with pytest.raises(Exception, match='no message'):
        Engine(graph).run({'in': 1})


def test_a_correction_is_checked_like_any_message():
    def run(ctx, x):
        ctx.correct(ctx.emit('out', 1), 'not a number')
    node = FunctionNode('n', run, inputs=[Port('x')], outputs=[Port('out', int)], context=True)
    graph = GraphBuilder().add(node).input('in', 'n.x').output('out', 'n.out').compile()
    with pytest.raises(Exception, match='expected int'):
        Engine(graph).run({'in': 1})


def test_corrections_count_against_how_much_an_edge_may_carry():
    seen = []

    def double(v):
        seen.append(v)
        return v * 2
    receiver = FunctionNode('d', double, inputs=[Port('v', required=False)], outputs=[Port('out')])
    graph = (GraphBuilder().add(sender((5,), wait=False), receiver)
             .connect('s.out', 'd.v', back=True, max_rounds=1).connect('d.out', 's.ack')
             .input('in', 's.x').output('out', 'd.out').compile())
    result = Engine(graph).run({'in': 0})
    assert seen == [1] and result.limited == ('s.out>d.v',)


def test_a_correction_is_journaled_and_a_run_taken_up_keeps_it(tmp_path):
    seen = []
    path = tmp_path / 'j.jsonl'
    result = Engine(chain('rerun', seen), workers=1, journal=Journal(path)).run({'in': 0})
    assert result.outputs['out'] == [10]
    records = Journal(path).read()
    sender_acts = [r for r in records if r['kind'] == 'act' and r['node'] == 's']
    assert sender_acts[0]['emits'][-1][0] == 'out#1'
    again = Engine(chain('rerun', []), workers=1, journal=Journal(path)).resume()
    assert again.outputs['out'] == [10] and again.outputs['sent'] == [5]


def test_a_correction_waiting_unread_when_the_run_stopped_is_what_is_found(tmp_path):
    path = tmp_path / 'j.jsonl'
    seen = []
    graph = chain('rerun', seen, wait=False)
    engine = Engine(graph, workers=0, journal=Journal(path))
    engine.run({'in': 0})
    records = Journal(path).read()
    assert any(r.get('fixed') for r in records if r['kind'] == 'act')


def test_on_correction_in_a_description():
    seen = []
    graph = chain('tell', seen)
    described = dump(graph)
    node = next(n for n in described['nodes'] if n['id'] == 'd')
    assert node['on_correction'] == 'tell'
    with pytest.raises(Exception, match='on_correction'):
        load({'nodes': [{'id': 'a', 'kind': 'agent', 'model': 'x', 'on_correction': 'sometimes'}]})


# ---- agents ----------------------------------------------------------------------------------------------

def _call(name, **args):
    return {'id': f'c{name}{len(args)}', 'name': name, 'args': args}


def _last_tool(messages):
    return next((m['content'] for m in reversed(messages) if m['role'] == 'tool'), '')


def test_an_agent_is_told_the_id_of_what_it_sent_and_corrects_it():
    from interlacing import AgentNode
    from interlacing.models import ChatResult, FakeProvider
    answers = []

    def policy(system, messages, tools):
        said = _last_tool(messages)
        answers.append(said)
        if not said:
            return ChatResult(tool_calls=[_call('send_out', content='wrong')])
        if said.startswith('sent, as '):
            return ChatResult(tool_calls=[_call('correct', id=said[len('sent, as '):], content='right')])
        if said.startswith('corrected'):
            return ChatResult(tool_calls=[_call('finish')])
        return ChatResult(tool_calls=[_call('correct', id='out#9', content='?'), _call('finish')])
    agent = AgentNode('a', FakeProvider(policy), resident=True, inputs=[Port('task')], outputs=[Port('out')])
    graph = GraphBuilder().add(agent).input('in', 'a.task').output('out', 'a.out').compile()
    result = Engine(graph).run({'in': 'go'})
    assert result.outputs['out'] == ['right'] and 'sent, as out#1' in answers and 'corrected out#1' in answers
    assert agent.board['fixed'] == {'out': 1} and 'out: sent 1 time, 1 correction' in agent._shown_board()


@pytest.mark.parametrize('workers', [0, 1, 2])
def test_an_agent_that_read_a_message_is_told_it_was_corrected(workers):
    from interlacing import AgentNode
    from interlacing.models import ChatResult, FakeProvider
    heard = []

    def writer(system, messages, tools):
        said = _last_tool(messages)
        if not said:
            return ChatResult(tool_calls=[_call('send_say', content='draft'), _call('wait')])
        if 'seen' in said:
            return ChatResult(tool_calls=[_call('correct', id='say#1', content='draft, fixed'), _call('wait')])
        return ChatResult(tool_calls=[_call('finish')])

    def critic(system, messages, tools):
        said = _last_tool(messages) or messages[-1]['content']
        heard.append(said)
        if 'Nothing more can come' in said:
            return ChatResult(tool_calls=[_call('finish')])
        if 'fixed' in said:
            return ChatResult(tool_calls=[_call('wait')])
        return ChatResult(tool_calls=[_call('send_say', content='seen'), _call('wait')])
    w = AgentNode('w', FakeProvider(writer), resident=True, inputs=[Port('task'), Port('heard', required=False)],
                  outputs=[Port('say')])
    c = AgentNode('c', FakeProvider(critic), resident=True, inputs=[Port('heard')], outputs=[Port('say')])
    graph = (GraphBuilder().add(w, c).connect('w.say', 'c.heard').connect('c.say', 'w.heard', back=True, max_rounds=5)
             .input('in', 'w.task').compile())
    result = Engine(graph, workers=workers).run({'in': 'go'})
    assert result.complete
    told = [h for h in heard if 'draft, fixed' in h]
    assert told and 'this corrects what came here before' in told[0]


def test_a_port_that_takes_many_runs_again_with_only_the_corrected_one_changed():
    seen = []

    def first(ctx, x):
        sent = ctx.emit('out', 1)
        ctx.receive()                      # until the join has read it
        ctx.correct(sent, 10)

    def join(xs):
        seen.append(list(xs))
        return sum(xs)
    a = FunctionNode('a', first, inputs=[Port('x')], outputs=[Port('out')], context=True)
    b = FunctionNode('b', lambda x: 2, inputs=[Port('x')], outputs=[Port('out')])
    j = FunctionNode('j', join, inputs=[Port('xs', many=True)], outputs=[Port('out')])
    graph = (GraphBuilder().add(a, b, j).connect('a.out', 'j.xs').connect('b.out', 'j.xs')
             .input('in', 'a.x').input('go', 'b.x').output('sum', 'j.out').compile())
    result = Engine(graph, workers=2).run({'in': 0, 'go': 0})
    assert seen == [[1, 2], [10, 2]] and result.outputs['sum'] == [12]
