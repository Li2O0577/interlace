"""Rejections: a receiver says that what came is not what it expected, and its sender explains or corrects."""
import pytest

from interlacing import Engine, FunctionNode, GraphBuilder, Journal, Port
from interlacing.spec import dump, load


def doubter(verdicts, times=1):
    def run(ctx, v):
        for _ in range(times):
            verdict = ctx.reject('v', 'that is too big')
            verdicts.append(verdict)
            if verdict['answer'] == 'corrected':
                v = verdict['value']
        return v
    return FunctionNode('d', run, inputs=[Port('v')], outputs=[Port('out')], context=True)


def graph(sender, receiver, challenges=2):
    return (GraphBuilder().add(sender, receiver).connect('s.out', 'd.v', challenges=challenges)
            .input('in', 's.x').output('out', 'd.out').compile())


@pytest.mark.parametrize('workers', [0, 1, 4])
def test_a_program_that_is_rejected_says_its_result_stands(workers):
    verdicts = []
    sender = FunctionNode('s', lambda x: x * 100, inputs=[Port('x')], outputs=[Port('out')])
    result = Engine(graph(sender, doubter(verdicts)), workers=workers).run({'in': 3})
    assert verdicts[0]['answer'] == 'explained' and 'program' in verdicts[0]['text']
    assert result.outputs['out'] == [300] and result.rounds == {'s': 2, 'd': 1}


@pytest.mark.parametrize('workers', [0, 1, 4])
def test_a_sender_that_corrects_gives_the_receiver_the_new_value(workers):
    verdicts, asked = [], []
    sender = FunctionNode('s', lambda x: x * 100, inputs=[Port('x')], outputs=[Port('out')])

    def challenged(ctx, rejection):
        asked.append(rejection)
        ctx.correct(rejection['message'], 3)
    sender.challenged = challenged
    result = Engine(graph(sender, doubter(verdicts)), workers=workers).run({'in': 3})
    assert verdicts == [{'answer': 'corrected', 'value': 3}] and result.outputs['out'] == [3]
    assert asked[0]['reason'] == 'that is too big' and asked[0]['content'] == 300 and asked[0]['by'] == 'd'
    assert asked[0]['message'] == 'out#1' and asked[0]['id'] == 's.out>d.v!1'


def test_an_edge_takes_only_so_many_rejections():
    verdicts = []
    sender = FunctionNode('s', lambda x: x, inputs=[Port('x')], outputs=[Port('out')])
    result = Engine(graph(sender, doubter(verdicts, times=3), challenges=2)).run({'in': 1})
    assert [v['answer'] for v in verdicts] == ['explained', 'explained', 'refused']
    assert 'all the rejections it may (2)' in verdicts[2]['why']
    assert result.problems[-1]['handled'] == 'unanswered'


def test_an_edge_that_takes_no_rejections_says_so():
    sender = FunctionNode('s', lambda x: x, inputs=[Port('x')], outputs=[Port('out')])
    receiver = doubter([])
    plain = (GraphBuilder().add(sender, receiver).connect('s.out', 'd.v').input('in', 's.x')
             .output('out', 'd.out').compile())
    with pytest.raises(Exception, match='takes no rejections'):
        Engine(plain).run({'in': 1})


@pytest.mark.parametrize('workers', [0, 1, 4])
def test_a_running_sender_is_woken_and_answers_itself(workers):
    verdicts = []

    def talk(ctx, x):
        sent = ctx.emit('out', x)
        while True:
            got = ctx.receive()
            if got is None:
                return None
            for rejection in ctx.rejections():
                if rejection['reason'] == 'that is too big':
                    ctx.correct(sent, 1)
                else:
                    ctx.explain(rejection['id'], 'it is right')
    sender = FunctionNode('s', talk, inputs=[Port('x')], outputs=[Port('out')], context=True)
    result = Engine(graph(sender, doubter(verdicts)), workers=workers).run({'in': 50})
    assert verdicts == [{'answer': 'corrected', 'value': 1}] and result.outputs['out'] == [1]
    assert result.rounds == {'s': 1, 'd': 1}


def test_a_sender_that_is_stopped_cannot_answer():
    verdicts = []
    sender = FunctionNode('s', lambda x: x, inputs=[Port('x')], outputs=[Port('out')])

    def run(ctx, v):
        ctx.control('s', 'stop')
        verdicts.append(ctx.reject('v', 'no'))
        return v
    receiver = FunctionNode('d', run, inputs=[Port('v')], outputs=[Port('out')], context=True)
    g = (GraphBuilder().add(sender, receiver).connect('s.out', 'd.v', challenges=1).manage('d', 's')
         .input('in', 's.x').output('out', 'd.out').compile())
    Engine(g).run({'in': 1})
    assert verdicts[0]['answer'] == 'refused' and 'stopped' in verdicts[0]['why']


def test_challenges_in_a_description_and_in_the_journal(tmp_path):
    sender = FunctionNode('s', lambda x: x, inputs=[Port('x')], outputs=[Port('out')])
    g = graph(sender, doubter([]), challenges=2)
    described = dump(g)
    assert described['edges'][0]['challenges'] == 2
    assert 'challenges' not in dump(GraphBuilder().add(sender).input('in', 's.x').compile()).get('edges', [{}])[0:1]
    with pytest.raises(Exception, match='challenges'):
        load({'nodes': [], 'edges': [{'from': 'a.b', 'to': 'c.d', 'challenges': 0}]})
    path = tmp_path / 'j.jsonl'
    Engine(g, journal=Journal(path)).run({'in': 1})
    assert any(r.get('rejects') == ['s.out>d.v'] for r in Journal(path).read() if r['kind'] == 'act')


# ---- agents ----------------------------------------------------------------------------------------------

def _call(name, **args):
    return {'id': f'c-{name}', 'name': name, 'args': args}


def _last_tool(messages):
    return next((m['content'] for m in reversed(messages) if m['role'] == 'tool'), '')


def test_an_agent_is_asked_to_judge_what_came_and_may_reject_it():
    from interlacing import AgentNode
    from interlacing.models import ChatResult, FakeProvider
    seen = {}

    def policy(system, messages, tools):
        said = _last_tool(messages)
        if not said:
            seen['asked'], seen['tools'] = messages[-1]['content'], [t['name'] for t in tools]
            return ChatResult(tool_calls=[_call('reject', port='task', reason='a number is missing')])
        seen['answer'], seen['tools_after'] = said, [t['name'] for t in tools]
        return ChatResult(text='fine now')
    sender = FunctionNode('s', lambda x: 'two and', inputs=[Port('x')], outputs=[Port('out')])
    sender.challenged = lambda ctx, r: ctx.correct(r['message'], 'two and three')
    agent = AgentNode('d', FakeProvider(policy))
    g = (GraphBuilder().add(sender, agent).connect('s.out', 'd.task', challenges=1).input('in', 's.x')
         .output('out', 'd.out').compile())
    result = Engine(g).run({'in': 1})
    assert result.last('out') == 'fine now'
    assert 'judge whether it is what you expected' in seen['asked'] and 'reject' in seen['tools']
    assert seen['answer'] == 'Its sender corrected it. It is now:\ntwo and three'
    assert 'reject' not in seen['tools_after']          # the edge has taken all it may


@pytest.mark.parametrize('how', ['explain', 'correct'])
def test_an_agent_whose_answer_is_rejected_explains_or_corrects(how):
    from interlacing import AgentNode
    from interlacing.models import ChatResult, FakeProvider
    verdicts, shown = [], []

    def policy(system, messages, tools):
        if '[rejected]' in messages[-1]['content']:
            shown.append((messages[-1]['content'], [t['name'] for t in tools]))
            if how == 'explain':
                return ChatResult(tool_calls=[_call('explain', text='it was asked for in words')])
            return ChatResult(tool_calls=[_call('correct', content='42')])
        return ChatResult(text='forty-two')
    agent = AgentNode('s', FakeProvider(policy), inputs=[Port('x')])
    g = (GraphBuilder().add(agent, doubter(verdicts)).connect('s.out', 'd.v', challenges=1)
         .input('in', 's.x').output('out', 'd.out').compile())
    result = Engine(g).run({'in': 'how many?'})
    assert 'that is too big' in shown[0][0] and shown[0][1] == ['explain', 'correct']
    if how == 'explain':
        assert verdicts == [{'answer': 'explained', 'text': 'it was asked for in words'}]
        assert result.outputs['out'] == ['forty-two']
    else:
        assert verdicts == [{'answer': 'corrected', 'value': '42'}] and result.outputs['out'] == ['42']


@pytest.mark.parametrize('workers', [0, 1, 2])
def test_a_resident_agent_sees_a_rejection_while_it_waits(workers):
    from interlacing import AgentNode
    from interlacing.models import ChatResult, FakeProvider
    verdicts = []

    def policy(system, messages, tools):
        said = _last_tool(messages)
        if not said:
            return ChatResult(tool_calls=[_call('send_out', content='draft'), _call('wait')])
        if '[rejected]' in said:
            return ChatResult(tool_calls=[_call('correct', id='out#1', content='better draft'), _call('wait')])
        return ChatResult(tool_calls=[_call('finish')])
    agent = AgentNode('s', FakeProvider(policy), resident=True, inputs=[Port('x')])
    g = (GraphBuilder().add(agent, doubter(verdicts)).connect('s.out', 'd.v', challenges=1)
         .input('in', 's.x').output('out', 'd.out').compile())
    result = Engine(g, workers=workers).run({'in': 'go'})
    assert verdicts == [{'answer': 'corrected', 'value': 'better draft'}] and result.outputs['out'] == ['better draft']
    assert result.rounds == {'s': 1, 'd': 1}


# ---- meeting other features ------------------------------------------------------------------------------

@pytest.mark.parametrize('workers', [0, 1, 4])
def test_a_node_run_again_after_a_correction_may_reject_what_it_reads(workers):
    seen = []

    def sender(ctx, x):
        sent = ctx.emit('out', 1)
        ctx.receive()                          # until the receiver has read it
        ctx.correct(sent, 2)

    def receiver(ctx, v):
        seen.append(v)
        if v == 2:
            seen.append(ctx.reject('v', 'still wrong'))
        return v
    s = FunctionNode('s', sender, inputs=[Port('x')], outputs=[Port('out')], context=True)
    d = FunctionNode('d', receiver, inputs=[Port('v')], outputs=[Port('out')], context=True)
    g = (GraphBuilder().add(s, d).connect('s.out', 'd.v', challenges=1).input('in', 's.x')
         .output('out', 'd.out').compile())
    result = Engine(g, workers=workers).run({'in': 0})
    assert seen[:2] == [1, 2] and seen[2]['answer'] == 'explained' and result.outputs['out'] == [2]


@pytest.mark.parametrize('stopped', ['answering', 'running'])
def test_a_sender_stopped_after_it_was_rejected_leaves_the_receiver_refused_at_once(stopped):
    import threading
    import time
    verdicts, began = [], threading.Event()

    def busy(ctx, *_):
        began.set()
        while ctx.checkpoint():
            time.sleep(0.01)

    def talk(ctx, x):
        ctx.emit('out', x)
        busy(ctx)
    if stopped == 'running':
        sender = FunctionNode('s', talk, inputs=[Port('x')], outputs=[Port('out')], context=True)
    else:
        sender = FunctionNode('s', lambda x: x, inputs=[Port('x')], outputs=[Port('out')])
        sender.challenged = busy

    def run(ctx, v):
        verdicts.append(ctx.reject('v', 'no'))
        return v
    receiver = FunctionNode('d', run, inputs=[Port('v')], outputs=[Port('out')], context=True)
    engine = Engine(graph(sender, receiver, challenges=1), workers=2)
    done = {}
    thread = threading.Thread(target=lambda: done.setdefault('result', engine.run({'in': 1})))
    thread.start()
    assert began.wait(5)
    time.sleep(0.05)
    engine.control('s', 'stop')
    thread.join(5)
    assert not thread.is_alive()
    assert verdicts[0]['answer'] == 'refused' and 'stopped' in verdicts[0]['why']
