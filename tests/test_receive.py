"""A message in the middle of an activation: ctx.receive, which waits without holding a worker."""
import asyncio
import threading
import time

import pytest

from interlacing import Engine, FunctionNode, GraphBuilder, Journal, Port
from interlacing.base import NodeError


def talker(name, turns, said, first=False):
    """Says something, waits for an answer, and so on; ends when nothing more can come."""
    def run(ctx, start=None, heard=None):
        count = 0
        if first:
            ctx.emit('say', f'{name} 0')
        pending = {'heard': heard} if heard is not None else None    # it may have come with the start
        while True:
            got, pending = pending or ctx.receive(), None
            if got is None:
                said.append((name, 'end'))
                return None
            said.append((name, got['heard']))
            count += 1
            if count < turns:
                ctx.emit('say', f'{name} {count}')
    return FunctionNode(name, run, inputs=[Port('start', required=False), Port('heard', required=False)],
                        outputs=[Port('say')], context=True)


def conversation(workers):
    said = []
    a, b = talker('a', 3, said, first=True), talker('b', 3, said)
    graph = (GraphBuilder().add(a, b).connect('a.say', 'b.heard').connect('b.say', 'a.heard', back=True, max_rounds=10)
             .input('in', 'a.start').input('go', 'b.start').compile())
    result = Engine(graph, workers=workers).run({'in': 1, 'go': 1})
    return said, result


@pytest.mark.parametrize('workers', [0, 1, 4])
def test_two_nodes_talk_and_both_end_when_nothing_more_can_come(workers):
    said, result = conversation(workers)
    assert [s for s in said if s[0] == 'b'] == [('b', 'a 0'), ('b', 'a 1'), ('b', 'a 2'), ('b', 'end')]
    assert [s for s in said if s[0] == 'a'] == [('a', 'b 1'), ('a', 'b 2'), ('a', 'end')]
    assert result.complete and result.rounds == {'a': 1, 'b': 1}


def test_two_nodes_waiting_for_each_other_are_both_told_nothing_will_come():
    said = []
    a, b = talker('a', 1, said), talker('b', 1, said)
    graph = (GraphBuilder().add(a, b).connect('a.say', 'b.heard').connect('b.say', 'a.heard', back=True, max_rounds=1)
             .input('in', 'a.start').input('go', 'b.start').compile())
    result = Engine(graph, workers=2).run({'in': 1, 'go': 1})
    assert sorted(said) == [('a', 'end'), ('b', 'end')] and result.complete


def test_a_waiting_node_holds_no_worker():
    """With one worker, a node waiting for another's answer must not keep that other from running."""
    def asker(ctx, x):
        ctx.emit('ask', x)
        return {'out': ctx.receive('answer')['answer'] * 10}
    node = FunctionNode('asker', asker, inputs=[Port('x'), Port('answer', required=False)],
                        outputs=[Port('ask'), Port('out')], context=True)
    double = FunctionNode('double', lambda q: q * 2, inputs=[Port('q')], outputs=[Port('a')])
    graph = (GraphBuilder().add(node, double).connect('asker.ask', 'double.q').connect('double.a', 'asker.answer', back=True, max_rounds=1)
             .input('in', 'asker.x').output('out', 'asker.out').compile())
    for workers in (0, 1):
        assert Engine(graph, workers=workers).run({'in': 4}).last('out') == 80


def test_a_time_limit_does_not_count_the_waiting():
    def asker(ctx, x):
        ctx.emit('ask', x)
        return {'out': ctx.receive('answer')['answer']}

    def slow(q):
        time.sleep(0.6)
        return q
    node = FunctionNode('asker', asker, inputs=[Port('x'), Port('answer', required=False)],
                        outputs=[Port('ask'), Port('out')], context=True)
    graph = (GraphBuilder().add(node, FunctionNode('slow', slow, inputs=[Port('q')], outputs=[Port('a')]))
             .connect('asker.ask', 'slow.q').connect('slow.a', 'asker.answer', back=True, max_rounds=1)
             .input('in', 'asker.x').output('out', 'asker.out').compile())
    graph.node('slow').time_limit = 5
    assert Engine(graph, workers=2, time_limit=0.4).run({'in': 3}).last('out') == 3


def test_a_timeout_and_a_choice_of_ports():
    seen = []

    def run(ctx, x, other=None):
        seen.append(ctx.receive(timeout=0.05))
        seen.append(ctx.receive('other', timeout=0))
        with pytest.raises(NodeError, match='no input port nope'):
            ctx.receive('nope')
    node = FunctionNode('n', run, inputs=[Port('x'), Port('other', required=False)], outputs=[Port('y')], context=True)
    other = FunctionNode('o', lambda: None, inputs=[], outputs=[Port('z')])
    graph = GraphBuilder().add(node, other).input('in', 'n.x').output('out', 'n.y').compile()
    assert Engine(graph, workers=2).run({'in': 1}).complete and seen == [None, None]


def test_stopping_a_waiting_node_puts_back_what_it_took():
    started = threading.Event()

    def run(ctx, x, more=None):
        started.set()
        ctx.receive()
        return {'y': x}
    node = FunctionNode('n', run, inputs=[Port('x'), Port('more', required=False)], outputs=[Port('y')], context=True)

    def tick(ctx):
        for _ in range(200):
            if not ctx.checkpoint():
                return
            time.sleep(0.01)
    keeper = FunctionNode('k', tick, inputs=[], outputs=[Port('z')], context=True)
    graph = GraphBuilder().add(node, keeper).input('in', 'n.x').output('out', 'n.y').compile()
    engine = Engine(graph, workers=2)
    box = {}
    thread = threading.Thread(target=lambda: box.update(result=engine.run({'in': 5})))
    thread.start()
    assert started.wait(5)
    time.sleep(0.1)
    assert engine.control('n', 'stop') == ['n']
    engine.control('k', 'stop')
    thread.join(10)
    assert box['result'].held == {'n': 'stopped', 'k': 'stopped'} and not box['result'].outputs['out']


def test_what_is_received_is_journaled_with_the_activation(tmp_path):
    def asker(ctx, x):
        ctx.emit('ask', x)
        return {'out': ctx.receive('answer')['answer']}
    node = FunctionNode('asker', asker, inputs=[Port('x'), Port('answer', required=False)],
                        outputs=[Port('ask'), Port('out')], context=True)
    graph = (GraphBuilder().add(node, FunctionNode('echo', lambda q: q, inputs=[Port('q')], outputs=[Port('a')]))
             .connect('asker.ask', 'echo.q').connect('echo.a', 'asker.answer', back=True, max_rounds=1)
             .input('in', 'asker.x').output('out', 'asker.out').compile())
    journal = Journal(tmp_path / 'j.jsonl')
    assert Engine(graph, workers=1, journal=journal).run({'in': 7}).last('out') == 7
    acts = [r for r in Journal(tmp_path / 'j.jsonl').read() if r['kind'] == 'act' and r['node'] == 'asker']
    assert len(acts) == 1 and any(took.startswith('echo.a>asker.answer#') for took in acts[0]['took'])


def test_a_coroutine_receives_without_holding_the_loop():
    async def asker(ctx, x):
        ctx.emit('ask', x)
        got = await ctx.areceive('answer')
        return {'out': got['answer'] + 1}
    node = FunctionNode('asker', asker, inputs=[Port('x'), Port('answer', required=False)],
                        outputs=[Port('ask'), Port('out')], context=True)
    graph = (GraphBuilder().add(node, FunctionNode('echo', lambda q: q, inputs=[Port('q')], outputs=[Port('a')]))
             .connect('asker.ask', 'echo.q').connect('echo.a', 'asker.answer', back=True, max_rounds=1)
             .input('in', 'asker.x').output('out', 'asker.out').compile())
    assert Engine(graph, workers=1).run({'in': 1}).last('out') == 2
    assert asyncio.run(Engine(graph).arun({'in': 2})).last('out') == 3
