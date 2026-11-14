"""ctx.once: what a node does outside the graph is done once, however often its activation runs."""
import pytest

from interlacing import DEFAULT, Chain, Engine, FunctionNode, GraphBuilder, GraphNode, Journal, Port, RunError


def fn(id, function, **options):
    return FunctionNode(id, function, inputs=[Port('x')], outputs=[Port('y')], context=True, **options)


def test_what_was_done_is_not_done_again_when_an_interrupted_run_is_taken_up(tmp_path):
    charged, fail = [], [True]

    def pay(ctx, x):
        receipt = ctx.once('charge', lambda: charged.append(x) or {'receipt': len(charged), 'amount': x})
        if fail.pop() if fail else False:
            raise OSError('the line dropped after the card was charged')
        return receipt

    build = lambda: Chain(fn('pay', pay)).build(DEFAULT)      # noqa: E731
    with pytest.raises(RunError, match='the line dropped'):
        Engine(build(), DEFAULT, journal=Journal(tmp_path / 'j')).run({'in': 30})
    result = Engine(build(), DEFAULT, journal=Journal(tmp_path / 'j')).resume()
    assert result.outputs == {'out': [{'receipt': 1, 'amount': 30}]} and charged == [30]


def test_each_activation_and_each_name_has_its_own():
    done = []

    def twice(ctx, x):
        first = ctx.once('a', lambda: done.append(('a', x)) or 'A')
        again = ctx.once('a', lambda: done.append(('a again', x)) or 'never')
        other = ctx.once('b', lambda: done.append(('b', x)) or 'B')
        return [first, again, other]

    source = FunctionNode('source', lambda ctx: [ctx.emit('y', i) for i in (1, 2)] and None, outputs=[Port('y')], context=True)
    result = Engine(Chain(source, fn('twice', twice)).build(DEFAULT), DEFAULT).run()
    assert result.outputs == {'out': [['A', 'A', 'B'], ['A', 'A', 'B']]}
    assert done == [('a', 1), ('b', 1), ('a', 2), ('b', 2)]


def test_an_activation_run_again_because_its_output_was_refused_does_not_repeat_it():
    sent, tries = [], []

    def notify(ctx, x):
        ctx.once('mail', lambda: sent.append(x))
        tries.append(1)
        return 'short' if len(tries) == 1 else 'long enough'

    node = FunctionNode('notify', notify, inputs=[Port('x')], outputs=[Port('y', check=lambda v: len(v) >= 6 or 'too short', repairs=1)],
                        context=True)
    result = Engine(Chain(node).build(DEFAULT), DEFAULT).run({'in': 'hello'})
    assert result.outputs == {'out': ['long enough']} and len(tries) == 2 and sent == ['hello']


def test_inside_a_block_it_holds_across_a_restart_too(tmp_path):
    done, fail = [], [True]

    def act(ctx, x):
        ctx.once('act', lambda: done.append(x))
        return x

    def breaks(ctx, x):
        if fail:
            fail.pop()
            raise OSError('not yet')
        return x + 1

    build = lambda: Chain(GraphNode('block', Chain(fn('act', act), fn('breaks', breaks)), DEFAULT)).build(DEFAULT)   # noqa: E731
    with pytest.raises(RunError, match='not yet'):
        Engine(build(), DEFAULT, journal=Journal(tmp_path / 'j')).run({'in': 1})
    assert Engine(build(), DEFAULT, journal=Journal(tmp_path / 'j')).resume().outputs == {'out': [2]} and done == [1]


def test_what_is_kept_must_be_plain_data_and_have_a_name(tmp_path):
    graph = lambda do: GraphBuilder().add(fn('n', do)).input('in', 'n.x').output('out', 'n.y').compile(DEFAULT)   # noqa: E731
    with pytest.raises(RunError, match="what once keeps under 'thing' must be plain data"):
        Engine(graph(lambda ctx, x: ctx.once('thing', lambda: {1, 2})), DEFAULT, journal=Journal(tmp_path / 'j')).run({'in': 1})
    with pytest.raises(RunError, match='once needs a name'):
        Engine(graph(lambda ctx, x: ctx.once('', lambda: 1)), DEFAULT).run({'in': 1})
