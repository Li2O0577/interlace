"""The desk: a node that takes in from several places, sorts, and hands out to several."""
import pytest

from inter1ace import Block, Catalog, Chain, Desk, Engine, Fan, FunctionNode, GraphBuilder, GraphError, NodeError, Port, RunError, dump, load
from inter1ace.protocols import DEFAULT


def fn(id, function, inputs=('x',), outputs=('y',), **options):
    return FunctionNode(id, function, inputs=[Port(name) for name in inputs], outputs=[Port(name) for name in outputs], **options)


def run(graph, inputs, workers=0):
    return Engine(graph, DEFAULT, workers=workers).run(inputs)


def share(received):
    """A way in: the words of a task, the even ones to one member and the odd ones to the other."""
    words = received['x'].split()
    return {'left': words[0::2], 'right': words[1::2]}


def stitch(received):
    return {'y': ' '.join(received['left'] + received['right'])}


def test_a_desk_without_sort_gives_everything_to_everyone():
    one = GraphBuilder().add(Desk('d', gives=['a', 'b'])).input('in', 'd.x').output('a', 'd.a').output('b', 'd.b').compile(DEFAULT)
    assert run(one, {'in': 7}).outputs == {'a': [7], 'b': [7]}
    two = GraphBuilder().add(Desk('d', takes=['p', 'q'])).input('p', 'd.p').input('q', 'd.q').output('out', 'd.y').compile(DEFAULT)
    assert run(two, {'p': 1, 'q': 2}).outputs == {'out': [{'p': 1, 'q': 2}]}


def test_a_desk_sorts_what_it_took_among_what_it_gives():
    desk = Desk('d', takes=['p', 'q'], gives=['big', 'small'],
                sort=lambda got: {'big': max(got.values()), 'small': min(got.values())})
    graph = (GraphBuilder().add(desk).input('p', 'd.p').input('q', 'd.q').output('big', 'd.big').output('small', 'd.small')
             .compile(DEFAULT))
    assert run(graph, {'p': 3, 'q': 9}).outputs == {'big': [9], 'small': [3]}


def test_an_output_the_sort_leaves_out_sends_nothing():
    desk = Desk('d', gives=['odd', 'even'], sort=lambda got: {'odd' if got['x'] % 2 else 'even': got['x']})
    graph = GraphBuilder().add(desk).input('in', 'd.x').output('odd', 'd.odd').output('even', 'd.even').compile(DEFAULT)
    assert run(graph, {'in': 5}).outputs == {'odd': [5], 'even': []}


def test_a_desk_that_does_not_wait_handles_each_arrival_by_itself():
    seen = []

    def note(got):
        seen.append(dict(got))
        return {'y': sorted(got)}

    desk = Desk('d', takes=['p', 'q'], sort=note, wait=False)
    graph = GraphBuilder().add(desk).input('p', 'd.p').input('q', 'd.q').output('out', 'd.y').compile(DEFAULT)
    assert run(graph, {'p': 1}).outputs == {'out': [['p']]}
    assert seen == [{'p': 1}]


def test_a_desk_refuses_what_it_cannot_be():
    with pytest.raises(NodeError, match='at least one'):
        Desk('d', takes=[])
    with pytest.raises(NodeError, match='does not call'):
        Desk('d', gives=[Port('y', call=True)])
    with pytest.raises(NodeError, match='not callable'):
        Desk('d', sort='nothing')
    desk = Desk('d', sort=lambda got: {'elsewhere': 1})
    graph = GraphBuilder().add(desk).input('in', 'd.x').output('out', 'd.y').compile(DEFAULT)
    with pytest.raises(RunError, match='sort gave to elsewhere'):
        run(graph, {'in': 1})
    desk = Desk('d', sort=lambda got: [1])
    graph = GraphBuilder().add(desk).input('in', 'd.x').output('out', 'd.y').compile(DEFAULT)
    with pytest.raises(RunError, match='must return a mapping'):
        run(graph, {'in': 1})


def shout(x):
    return [word.upper() for word in x]


def mirror(x):
    return [word[::-1] for word in x]


def cell():
    """A block with a desk at the way in and a desk at the way out."""
    return Fan(fn('left', shout), fn('right', mirror),
               split=Desk('door', gives=['left', 'right'], sort=share),
               join=Desk('exit', takes=['left', 'right'], sort=stitch))


@pytest.mark.parametrize('workers', [0, 3])
def test_a_fan_joins_desks_to_its_branches_by_name(workers):
    result = run(cell().build(DEFAULT), {'in': 'one two three four'}, workers)
    assert result.outputs == {'out': ['ONE THREE owt ruof']}
    assert result.activations == 4


def test_branch_ids_with_a_dash_are_matched_to_ports_with_an_underscore():
    graph = Fan(fn('part-1', lambda x: x + 1), fn('part-2', lambda x: x * 2),
                split=Desk('door', gives=['part_1', 'part_2'], sort=lambda got: {'part_1': got['x'], 'part_2': got['x'] + 10}),
                join=Desk('exit', takes=['part_1', 'part_2'])).build(DEFAULT)
    assert run(graph, {'in': 1}).outputs == {'out': [{'part_1': 2, 'part_2': 22}]}


def test_a_fan_shows_everything_its_desks_take_and_give():
    graph = Fan(fn('a', lambda x: x * 2), fn('b', lambda x: x * 3),
                split=Desk('door', takes=['north', 'south'], gives=['a', 'b'],
                           sort=lambda got: {'a': got['north'], 'b': got['south']}),
                join=Desk('exit', takes=['a', 'b'], gives=['sum', 'pair'],
                          sort=lambda got: {'sum': got['a'] + got['b'], 'pair': [got['a'], got['b']]})).build(DEFAULT)
    assert sorted(graph.inputs) == ['north', 'south'] and sorted(graph.outputs) == ['pair', 'sum']
    assert run(graph, {'north': 1, 'south': 2}).outputs == {'sum': [8], 'pair': [[2, 6]]}


def test_a_join_that_fits_neither_way_is_refused():
    with pytest.raises(GraphError, match='named after each branch'):
        Fan(fn('a', lambda x: x), fn('b', lambda x: x), join=Desk('exit', takes=['a', 'c'])).build(DEFAULT)


def test_blocks_with_desks_pass_work_from_one_to_the_next():
    block = Block(cell, name='cell')
    graph = Chain(block('first'), block('second')).build(DEFAULT)
    assert run(graph, {'in': 'ab cd'}).outputs == {'out': ['AB cd']}


def test_a_desk_takes_from_several_blocks():
    block = Block(cell, name='cell')
    gather = Fan(fn('count', lambda x: len(x)), fn('longest', lambda x: max(x, key=len)),
                 split=Desk('door', takes=['east', 'west'], gives=['count', 'longest'],
                            sort=lambda got: dict.fromkeys(['count', 'longest'], got['east'].split() + got['west'].split())),
                 join=Desk('exit', takes=['count', 'longest']))
    graph = (GraphBuilder().add(block('east'), block('west'), Block(lambda: gather, name='gather')('gather'))
             .connect('east.out', 'gather.east', 'value').connect('west.out', 'gather.west', 'value')
             .input('e', 'east.in').input('w', 'west.in').output('out', 'gather.out').compile(DEFAULT))
    result = run(graph, {'e': 'a bb', 'w': 'ccc d'}, workers=2)
    assert result.outputs == {'out': [{'count': 4, 'longest': 'CCC'}]}


def test_a_desk_is_written_out_and_read_back():
    graph = cell().build(DEFAULT)
    described = dump(graph)
    kinds = {node['id']: node['kind'] for node in described['nodes']}
    assert kinds['door'] == 'desk' and kinds['exit'] == 'desk'
    again = load(described, Catalog(allow=[__name__]))
    assert again.digest == graph.digest
    assert run(again, {'in': 'one two three four'}).outputs == {'out': ['ONE THREE owt ruof']}
    loose = load(dump(GraphBuilder().add(Desk('d', takes=['p', 'q'], wait=False)).input('p', 'd.p').input('q', 'd.q')
                      .output('out', 'd.y').compile(DEFAULT)), Catalog())
    assert run(loose, {'q': 4}).outputs == {'out': [{'q': 4}]}


def test_a_desk_in_a_graph_written_as_data():
    graph = load({
        "method": "fan",
        "nodes": [{"id": "left", "kind": "function", "function": f"{__name__}.shout", "inputs": ["x"], "outputs": ["y"]},
                  {"id": "right", "kind": "function", "function": f"{__name__}.mirror", "inputs": ["x"], "outputs": ["y"]}],
        "split": {"id": "door", "kind": "desk", "outputs": ["left", "right"], "sort": f"{__name__}.share"},
        "join": {"id": "exit", "kind": "desk", "inputs": ["left", "right"], "sort": f"{__name__}.stitch"},
    }, Catalog(allow=[__name__]))
    assert run(graph, {'in': 'ab cd'}).outputs == {'out': ['AB dc']}


def test_an_agent_stands_where_a_desk_does():
    from inter1ace import AgentNode, ChatResult, FakeProvider
    turns = [ChatResult(text='', tool_calls=[{'id': '1', 'name': 'send_left', 'args': {'content': ['a']}},
                                              {'id': '2', 'name': 'send_right', 'args': {'content': ['bc']}}]),
             ChatResult(text='shared out')]
    door = AgentNode('door', FakeProvider(lambda system, messages, tools: turns.pop(0)), outputs=[Port('left'), Port('right')])
    graph = Fan(fn('left', shout), fn('right', mirror), split=door, join=Desk('exit', takes=['left', 'right'], sort=stitch)).build(DEFAULT)
    assert run(graph, {'in': 'a bc'}).outputs == {'out': ['A cb']}
