"""The methods beyond the chain (loop, fan, tree, star), the nodes that direct the flow, and a graph as a node."""
import threading
import time

import pytest

from interlace.base import Firing, GraphBuilder, GraphError, NodeError, Port
from interlace.engine import Engine, RunError
from interlace.methods import Chain, Fan, Loop, Star, Tree
from interlace.nodes import Branch, Collect, FunctionNode, GraphNode, Pass, Until
from interlace.protocols import DEFAULT

WORKERS = [0, 1, 4]


def fn(id, function, inputs=('x',), outputs=('y',), **options):
    ports = lambda names: [p if isinstance(p, Port) else Port(p) for p in names]   # noqa: E731
    return FunctionNode(id, function, inputs=ports(inputs), outputs=ports(outputs), **options)


def run(graph, inputs=None, workers=0):
    return Engine(graph, DEFAULT, workers=workers).run(inputs)


# ---- flow nodes ----------------------------------------------------------------------------------------

def test_branch_sends_each_value_one_way():
    builder = GraphBuilder().add(fn('count', lambda ctx: [ctx.emit('y', i) for i in range(6)] and None, inputs=(), context=True),
                                 Branch('even', lambda x: x % 2 == 0))
    graph = builder.connect('count.y', 'even.x', 'value').output('yes', 'even.yes').output('no', 'even.no').compile(DEFAULT)
    assert run(graph).outputs == {'yes': [0, 2, 4], 'no': [1, 3, 5]}
    with pytest.raises(NodeError, match='not callable'):
        Branch('b', 3)
    assert Branch('b', len).config() == {'condition': 'builtins.len'}


def test_until_stops_asking_at_its_limit():
    assert Until('u', bool, limit=2, strict=True).config() == {'condition': 'builtins.bool', 'limit': 2, 'strict': True}
    with pytest.raises(NodeError, match='at least 1'):
        Until('u', bool, limit=0)


# ---- loop ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize('workers', WORKERS)
def test_a_loop_with_until_goes_round_until_the_condition_holds(workers):
    graph = Loop(fn('double', lambda x: x * 2), fn('inc', lambda x: x + 1), until=lambda x: x > 50, max_rounds=10).build(DEFAULT)
    result = run(graph, {'in': 1}, workers)
    assert result.outputs == {'out': [63]}                  # 3, 7, 15, 31, 63
    assert result.rounds == {'double': 5, 'inc': 5, 'until': 5} and result.limited == ()
    assert [e.id for e in graph.edges if e.back] == ['again'] and graph.edge('again').max_rounds == 9


def test_a_loop_with_until_ends_at_its_round_limit_with_the_last_value_or_fails_when_strict():
    body = lambda: fn('inc', lambda x: x + 1)   # noqa: E731
    result = run(Loop(body(), until=lambda x: False, max_rounds=4).build(DEFAULT), {'in': 0})
    assert result.outputs == {'out': [4]} and result.rounds['inc'] == 4 and result.limited == ()
    with pytest.raises(RunError, match='node until failed in round 4: the condition did not hold after 4 rounds'):
        run(Loop(body(), until=lambda x: False, max_rounds=4, strict=True).build(DEFAULT), {'in': 0})
    once = Loop(body(), until=lambda x: False, max_rounds=1).build(DEFAULT)
    assert not any(e.back for e in once.edges) and run(once, {'in': 0}).outputs == {'out': [1]}


def test_a_loop_whose_last_node_decides():
    def review(x):
        return {'again': x + 1} if x < 3 else {'ok': x}
    loop = lambda rounds: Loop(fn('draft', lambda x: x), fn('review', review, outputs=('again', 'ok')), max_rounds=rounds).build(DEFAULT)  # noqa: E731
    result = run(loop(10), {'in': 0})
    assert result.outputs == {'out': [3]} and result.rounds == {'draft': 4, 'review': 4} and result.limited == ()
    limited = run(loop(2), {'in': 0})
    assert limited.outputs == {'out': []} and limited.rounds['draft'] == 2 and limited.limited == ('again',)


def test_a_loop_refuses_parts_it_cannot_join():
    with pytest.raises(GraphError, match='max_rounds of at least 1'):
        Loop(fn('a', lambda x: x), max_rounds=0)
    with pytest.raises(GraphError, match='strict only applies'):
        Loop(fn('a', lambda x: x), max_rounds=2, strict=True)
    with pytest.raises(GraphError, match="needs an output 'again' and one other output"):
        Loop(fn('a', lambda x: x), max_rounds=2).build(DEFAULT)
    with pytest.raises(GraphError, match='nothing can be sent back'):
        Loop(fn('a', lambda: 1, inputs=()), until=bool, max_rounds=2).build(DEFAULT)
    with pytest.raises(GraphError, match='duplicate node: until'):
        Loop(fn('until', lambda x: x), until=bool, max_rounds=2).build(DEFAULT)


# ---- fan -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize('workers', WORKERS)
def test_a_fan_gives_every_branch_the_input_and_joins_their_outputs_in_order(workers):
    slow = lambda x: (time.sleep(0.02), x + 1)[1]   # noqa: E731
    graph = Fan(fn('a', slow), fn('b', lambda x: x * 10), fn('c', lambda x: -x)).build(DEFAULT)
    result = run(graph, {'in': 4}, workers)
    assert result.outputs == {'out': [[5, 40, -4]]} and result.rounds['join'] == 1


def test_a_fan_takes_its_own_split_and_join():
    join = fn('total', lambda parts: sum(parts), inputs=[Port('parts', int, many=True)])
    whole = [Port('y', int)]
    graph = Fan(fn('a', lambda x: x + 1, outputs=whole), fn('b', lambda x: x * 10, outputs=whole),
                split=fn('prepare', lambda x: x * 2), join=join).build(DEFAULT)
    assert run(graph, {'in': 3}).outputs == {'out': [67]}
    with pytest.raises(GraphError, match='one input port declared many'):
        Fan(fn('a', lambda x: x), join=fn('j', lambda x: x)).build(DEFAULT)
    with pytest.raises(GraphError, match='cannot be a branch'):
        Fan(fn('a', lambda: 1, inputs=())).build(DEFAULT)
    with pytest.raises(GraphError, match='at least one branch'):
        Fan()


def test_pass_and_collect_carry_a_schema():
    graph = Fan(fn('a', lambda x: x, outputs=[Port('y', int)]), split=Pass('split', schema=int), join=Collect('join', schema=int)).build(DEFAULT)
    assert run(graph, {'in': 1}).outputs == {'out': [[1]]}
    with pytest.raises(RunError, match='graph input in: expected int'):
        run(graph, {'in': 'one'})


# ---- tree and star -------------------------------------------------------------------------------------

def coordinator(id, children, combine, addressed=False):
    """Sends its task to its children, waits for one reply from each, and sends out ``combine`` of the replies."""
    replies = []

    def act(ctx, task=None, reply=None):
        if task is not None:
            for port in (children if addressed else ['send']):      # send reaches every child at once
                ctx.emit(port, task)
        else:
            replies.append(reply)
            if len(replies) == len(children):
                ctx.emit('out', combine(replies))
    outputs = ['out'] + (list(children) if addressed else ['send'])
    return fn(id, act, inputs=['task', Port('reply', required=False)], outputs=outputs, firing=Firing.ANY, context=True)


@pytest.mark.parametrize('workers', WORKERS)
def test_a_star_hands_work_to_its_spokes_and_takes_their_replies(workers):
    graph = Star(coordinator('hub', ['a', 'b', 'c'], sorted), fn('a', lambda x: x + 1), fn('b', lambda x: x + 2),
                 fn('c', lambda x: x + 3), max_rounds=1).build(DEFAULT)
    result = run(graph, {'in': 10}, workers)
    assert result.outputs == {'out': [[11, 12, 13]]}
    assert result.rounds == {'hub': 4, 'a': 1, 'b': 1, 'c': 1} and result.limited == ()
    assert sorted(e.id for e in graph.edges if e.back) == ['a.y>hub.reply', 'b.y>hub.reply', 'c.y>hub.reply']


def test_a_coordinator_may_address_each_child_through_a_port_of_its_name():
    def hub(ctx, task=None, reply=None):
        if task is not None:
            ctx.emit('a', task)                 # only a gets the first task
        elif reply < 100:
            ctx.emit('b', reply)                # a's reply goes on to b
        else:
            ctx.emit('out', reply)
    node = fn('hub', hub, inputs=['task', Port('reply', required=False)], outputs=['out', 'a', 'b'], firing=Firing.ANY, context=True)
    graph = Star(node, fn('a', lambda x: x + 1), fn('b', lambda x: x * 100), max_rounds=2).build(DEFAULT)
    result = run(graph, {'in': 1})
    assert result.outputs == {'out': [200]} and result.rounds == {'hub': 3, 'a': 1, 'b': 1}


@pytest.mark.parametrize('workers', WORKERS)
def test_a_tree_nests_coordinators(workers):
    tree = Tree(coordinator('lead', ['a', 'mid'], sum, addressed=True),
                fn('a', lambda x: x + 1),
                Tree(coordinator('mid', ['m1', 'm2'], sum), fn('m1', lambda x: x * 2), fn('m2', lambda x: x * 3), max_rounds=1),
                max_rounds=1)
    graph = tree.build(DEFAULT)
    assert len(graph) == 5 and len(graph.edges) == 8
    result = run(graph, {'in': 2}, workers)
    assert result.outputs == {'out': [13]}                 # (2 + 1) + (2 * 2 + 2 * 3)
    assert result.rounds == {'lead': 3, 'a': 1, 'mid': 3, 'm1': 1, 'm2': 1}


def test_replies_beyond_the_limit_are_dropped_and_named():
    def hub(ctx, task=None, reply=None):
        ctx.emit('send', 0 if task is not None else reply)      # never done by itself
    node = fn('hub', hub, inputs=['task', Port('reply', required=False)], outputs=['out', 'send'], firing=Firing.ANY, context=True)
    result = run(Star(node, fn('a', lambda x: x + 1), max_rounds=3).build(DEFAULT), {'in': 0})
    assert result.rounds == {'hub': 4, 'a': 4} and result.limited == ('a.y>hub.reply',) and result.outputs == {'out': []}


def test_a_tree_says_what_a_coordinator_lacks():
    leaf = lambda: fn('a', lambda x: x)   # noqa: E731
    ports = dict(inputs=['task', Port('reply', required=False)], outputs=['out', 'send'], firing=Firing.ANY)
    with pytest.raises(GraphError, match='must fire on any input'):
        Star(fn('hub', lambda **k: 1, **{**ports, 'firing': Firing.ALL}), leaf(), max_rounds=1).build(DEFAULT)
    with pytest.raises(GraphError, match="optional input 'reply'"):
        Star(fn('hub', lambda **k: 1, **{**ports, 'inputs': ['task']}), leaf(), max_rounds=1).build(DEFAULT)
    with pytest.raises(GraphError, match="optional input 'reply'"):
        Star(fn('hub', lambda **k: 1, **{**ports, 'inputs': ['task', 'reply']}), leaf(), max_rounds=1).build(DEFAULT)
    with pytest.raises(GraphError, match='exactly one input besides'):
        Star(fn('hub', lambda **k: 1, **{**ports, 'inputs': ['task', 'more', Port('reply', required=False)]}), leaf(), max_rounds=1).build(DEFAULT)
    with pytest.raises(GraphError, match="needs an output 'out'"):
        Star(fn('hub', lambda **k: 1, **{**ports, 'outputs': ['send']}), leaf(), max_rounds=1).build(DEFAULT)
    with pytest.raises(GraphError, match="named 'a' or 'send'"):
        Star(fn('hub', lambda **k: 1, **{**ports, 'outputs': ['out']}), leaf(), max_rounds=1).build(DEFAULT)
    with pytest.raises(GraphError, match='at least one child'):
        Star(fn('hub', lambda **k: 1, **ports), max_rounds=1)
    with pytest.raises(GraphError, match='use Tree for more levels'):
        Star(fn('hub', lambda **k: 1, **ports), Tree(fn('mid', lambda **k: 1, **ports), leaf(), max_rounds=1), max_rounds=1)
    with pytest.raises(GraphError, match='max_rounds of at least 1'):
        Tree(fn('hub', lambda **k: 1, **ports), leaf(), max_rounds=0)


# ---- a graph as a node ---------------------------------------------------------------------------------

@pytest.mark.parametrize('workers', WORKERS)
def test_methods_nest_through_graph_nodes(workers):
    refine = GraphNode('refine', Loop(fn('double', lambda x: x * 2), until=lambda x: x >= 20, max_rounds=10), DEFAULT)
    views = GraphNode('views', Fan(fn('a', lambda x: x + 1), fn('b', lambda x: x - 1)), DEFAULT)
    graph = Chain(fn('start', lambda x: x + 1), refine, views, fn('end', lambda x: sum(x))).build(DEFAULT)
    assert list(refine.inputs) == ['in'] and list(refine.outputs) == ['out']
    result = run(graph, {'in': 2}, workers)
    assert result.outputs == {'out': [48]}                  # 3 -> 24 -> [25, 23] -> 48
    assert result.rounds == {'start': 1, 'refine': 1, 'views': 1, 'end': 1}
    assert refine.last.rounds == {'double': 3, 'until': 3}


def test_every_activation_of_a_graph_node_is_a_fresh_inner_run():
    inner = GraphNode('inner', Loop(fn('inc', lambda x: x + 1), until=lambda x: x % 5 == 0, max_rounds=5), DEFAULT)
    source = fn('source', lambda ctx: [ctx.emit('y', i) for i in (1, 11, 21)] and None, inputs=(), context=True)
    assert run(Chain(source, inner).build(DEFAULT)).outputs == {'out': [5, 15, 25]}


def test_a_graph_node_keeps_the_inner_ports_and_the_inner_digest():
    inner = GraphBuilder().add(fn('a', lambda x, hint=None: {'big': x} if x > 9 else {'small': x},
                                  inputs=[Port('x', int), Port('hint', str, required=False)], outputs=[Port('big', int), Port('small', int)]))
    graph = inner.input('value', 'a.x').input('hint', 'a.hint').output('big', 'a.big').output('small', 'a.small').compile(DEFAULT)
    node = GraphNode('sub', graph, DEFAULT)
    assert node.describe()['inputs'] == [Port('value', int).describe(), Port('hint', str, required=False).describe()]
    assert [p.name for p in node.outputs.values()] == ['big', 'small'] and node.config() == {'graph': graph.digest}
    outer = GraphBuilder().add(node).input('in', 'sub.value').output('big', 'sub.big').output('small', 'sub.small').compile(DEFAULT)
    assert run(outer, {'in': 12}).outputs == {'big': [12], 'small': []}
    with pytest.raises(RunError, match='graph input in: expected int'):
        run(outer, {'in': 'x'})
    with pytest.raises(NodeError, match='not a graph or a method'):
        GraphNode('sub', 'graph', DEFAULT)
    odd = GraphBuilder().add(fn('a', lambda x: x)).input('Not A Port', 'a.x').output('out', 'a.y').compile(DEFAULT)
    with pytest.raises(NodeError, match='become port names'):
        GraphNode('sub', odd, DEFAULT)


def test_a_failure_inside_a_graph_node_names_both_nodes():
    inner = GraphNode('inner', Chain(fn('ok', lambda x: x), fn('bad', lambda x: 1 / x)), DEFAULT)
    with pytest.raises(RunError, match='node inner failed in round 1: node bad failed in round 1: division by zero') as caught:
        run(Chain(fn('a', lambda x: x), inner).build(DEFAULT), {'in': 0})
    assert caught.value.node == 'inner'


def test_stopping_the_outer_run_stops_the_inner_one():
    started, saw_stop = threading.Event(), threading.Event()

    def wait(ctx, x):
        started.set()
        for _ in range(500):
            if ctx.stopping():
                saw_stop.set()
                return None
            time.sleep(0.01)
        return x

    def fail(x):
        started.wait(5)
        raise ValueError('boom')
    inner = GraphNode('inner', Chain(fn('wait', wait, context=True), fn('never', lambda x: x)), DEFAULT)
    graph = Fan(inner, fn('fail', fail)).build(DEFAULT)
    began = time.perf_counter()
    with pytest.raises(RunError, match='node fail failed'):
        run(graph, {'in': 1}, workers=2)
    assert saw_stop.is_set() and time.perf_counter() - began < 3
    assert inner.last is None or 'never' not in inner.last.rounds
