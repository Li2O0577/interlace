"""The short ways: plain functions as nodes, ``@node``, protocols left out, ``run`` in one step, and a port
that says nothing of what it produces feeding one that does."""
import pytest

from interlacing import (Chain, Engine, Fan, Firing, FunctionNode, GraphBuilder, GraphNode, Loop, NodeError, Port,
                         RunError, Star, as_node, node, run)
from interlacing.base import GraphError
from interlacing.nodes import Pass


def double(x: int) -> int:
    return x * 2


def show(x: int) -> str:
    return f"={x}"


def test_a_function_says_what_its_node_is():
    def add(x: int, y: float = 1.0, *, scale: list[float] | None = None) -> float:
        return x + y
    made = as_node(add)
    assert made.id == 'add' and list(made.inputs) == ['x', 'y', 'scale'] and list(made.outputs) == ['out']
    assert made.inputs['x'].required and not made.inputs['y'].required and not made.inputs['scale'].required
    made.inputs['x'].schema.check(3)
    with pytest.raises(ValueError):
        made.inputs['x'].schema.check('3')
    made.inputs['y'].schema.check(2)              # a whole number is a number
    made.inputs['scale'].schema.check('anything')  # a union is not checked by shape
    made.outputs['out'].schema.check(1.5)


def test_what_a_function_cannot_be():
    with pytest.raises(NodeError, match='needs an id'):
        as_node(lambda x: x)
    assert as_node(lambda x: x, 'same').id == 'same'

    def spread(*xs):
        return xs
    with pytest.raises(NodeError, match=r'\*xs cannot be an input'):
        as_node(spread)

    def Shout(X):
        return X
    with pytest.raises(NodeError):
        as_node(Shout)
    with pytest.raises(NodeError, match='not a node or a function'):
        as_node(3)


def test_ctx_and_no_output():
    seen = []

    def counts(ctx, n: int) -> None:
        for i in range(n):
            seen.append((ctx.node, i))
    made = as_node(counts)
    assert list(made.inputs) == ['n'] and not made.outputs and made.context
    assert run(made, {'n': 2}).outputs == {} and seen == [('counts', 0), ('counts', 1)]


def test_node_written_over_a_definition_is_still_the_function():
    @node
    def triple(x: int) -> int:
        return x * 3

    @node(id='halves', outputs=['low', 'high'], firing=Firing.ALL)
    def split(x: int):
        return {'low': x // 2, 'high': x - x // 2}

    assert isinstance(triple, FunctionNode) and triple(2) == 6 and triple.id == 'triple'
    assert split.id == 'halves' and list(split.outputs) == ['low', 'high']
    result = run(split, {'x': 5})
    assert result.last('low') == 2 and result.last('high') == 3
    assert FunctionNode('again', triple, inputs=[Port('x')], outputs=[Port('y')]).function is triple.function


def test_a_check_written_on_the_node():
    def positive(value):
        return value > 0 or 'not positive'

    @node(check=positive)
    def minus(x: int) -> int:
        return -x
    assert run(minus, {'x': -2}).last('out') == 2
    with pytest.raises(RunError, match='not positive'):
        run(minus, {'x': 2})


def test_methods_take_plain_functions():
    def inc(x: int) -> int:
        return x + 1

    def neg(x: int) -> int:
        return -x
    assert run(Chain(double, show), {'in': 21}).last('out') == '=42'
    assert run(Fan(inc, neg), {'in': 1}).last('out') == [2, -1]
    assert run(Loop(inc, until=lambda v: v > 5, max_rounds=10), {'in': 0}).last('out') == 6

    def hub(ctx, task=None, reply=None):
        if task is not None:
            ctx.emit('send', task)
        else:
            ctx.emit('out', reply)
    lead = FunctionNode('lead', hub, inputs=[Port('task', required=False), Port('reply', required=False)],
                        outputs=[Port('send'), Port('out')], firing=Firing.ANY, context=True)
    assert run(Star(lead, inc, max_rounds=1), {'in': 4}).last('out') == 5


def test_protocols_can_be_left_out():
    graph = Chain(double, show).build()
    assert graph.protocols is not None
    assert Engine(graph).run({'in': 5}).last('out') == '=10'
    built = GraphBuilder().add(as_node(double), as_node(show)).connect('double.out', 'show.x') \
        .input('in', 'double.x').output('out', 'show.out').compile()
    assert Engine(built).run({'in': 1}).last('out') == '=2'
    inner = GraphNode('inner', Chain(double, double))
    assert run(Chain(inner, show), {'in': 1}).last('out') == '=4'


def test_a_port_that_promises_nothing_may_feed_one_that_asks():
    loose = Pass('loose')            # produces anything
    graph = Chain(loose, double).build()
    assert Engine(graph).run({'in': 3}).last('out') == 6
    with pytest.raises(RunError, match='expected int'):
        Engine(graph).run({'in': 'three'})          # checked when it arrives
    warned = FunctionNode('warned', lambda x: x, inputs=[Port('x', int, invalid='warn')], outputs=[Port('y')])
    result = Engine(Chain(Pass('loose'), warned).build()).run({'in': 'three'})
    assert result.last('out') == 'three' and result.problems[0]['handled'] == 'warned'
    with pytest.raises(GraphError, match='does not accept'):  # a promise that cannot be kept is still refused
        Chain(as_node(show), as_node(double)).build()


def test_run_takes_a_graph_a_method_a_node_or_a_function():
    assert run(double, {'x': 4}).last('out') == 8
    assert run(as_node(double), {'x': 4}).last('out') == 8
    assert run(Chain(double), {'in': 4}).last('out') == 8
    assert run(Chain(double).build(), {'in': 4}, workers=2).last('out') == 8


def test_a_method_inside_a_method_is_a_node():
    def inc(x: int) -> int:
        return x + 1
    nested = Chain(inc, Fan(inc, double), Chain(inc, inc), Chain(inc))
    assert [n.id for n in nested.nodes] == ['inc', 'fan', 'chain', 'chain_2']
    assert run(Chain(inc, Fan(inc, double)), {'in': 1}).last('out') == [3, 4]
