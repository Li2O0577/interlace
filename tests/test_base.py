"""The abstract layer: schemas, ports, nodes, edges, protocols and the compiled graph. Nothing is executed here."""
import ast
from pathlib import Path
import sys

import pytest

from inter1ace.base import (ANY, Edge, EdgeError, Endpoint, Firing, GraphBuilder, GraphError, Is, ListOf, Node,
                                NodeError, Port, PortError, Protocol, ProtocolError, Protocols, Record, SchemaError,
                                digest, schema)

BASE = Path(__file__).resolve().parents[1] / 'src' / 'inter1ace' / 'base'


class Plain(Protocol):
    name = 'plain'


class Strict(Protocol):
    name = 'strict'

    def check(self, source, target):
        if source.schema != target.schema:
            raise ProtocolError('schemas differ')


PROTOCOLS = Protocols([Plain(), Strict()])


class Step(Node):
    """A node of no particular kind: one optional-by-default input ``in``, one output ``out``."""
    kind = 'step'
    inputs = (Port('in'),)
    outputs = (Port('out'),)

    def __init__(self, id, note='', **options):
        super().__init__(id, **options)
        self.note = note

    def config(self):
        return {'note': self.note}

    def run(self, ctx, inputs):
        return {'out': inputs.get('in')}


class Start(Node):
    kind = 'start'
    outputs = (Port('out'),)

    def run(self, ctx, inputs):
        return {'out': 1}


def chain(*ids):
    builder = GraphBuilder().add(Start(ids[0]), *[Step(i) for i in ids[1:]])
    for a, b in zip(ids, ids[1:]):
        builder.connect(f'{a}.out', f'{b}.in', 'plain')
    return builder


# ---- schemas ------------------------------------------------------------------------------------------

def test_schema_short_forms_and_checks():
    assert schema(None) is ANY and schema(int) == Is(int) and schema([str]) == ListOf(Is(str))
    record = schema({'name': str, 'tags': [str], 'score': (int, float)})
    record.check({'name': 'a', 'tags': ['x'], 'score': 1.5})
    with pytest.raises(SchemaError, match='tags: .1.: expected str'):
        record.check({'name': 'a', 'tags': ['x', 2], 'score': 1})
    with pytest.raises(SchemaError, match='missing'):
        record.check({'name': 'a'})
    with pytest.raises(SchemaError, match='unknown'):
        record.check({'name': 'a', 'tags': [], 'score': 1, 'extra': 0})
    with pytest.raises(SchemaError):
        Is(int).check(True)                      # a bool is not an int here
    with pytest.raises(SchemaError):
        schema(3)


def test_schema_accepts_is_about_every_value_the_other_side_can_send():
    assert ANY.accepts(Is(int)) and not Is(int).accepts(ANY)
    assert Is(int, float).accepts(Is(int)) and not Is(int).accepts(Is(int, float))
    assert ListOf(Is(int, str)).accepts(ListOf(Is(int))) and not ListOf(Is(int)).accepts(ListOf(ANY))
    wide, narrow = Record({'a': int, 'b': str}, optional={'b'}), Record({'a': int})
    assert wide.accepts(narrow)                  # b may be absent
    assert not narrow.accepts(wide)              # the sender may add b, which narrow refuses
    assert Record({'a': int}, open=True).accepts(wide)
    assert not Record({'a': int, 'b': str}).accepts(wide)      # b is required here, optional there
    assert Record({'a': int}) == Record({'a': int}) and hash(Record({'a': int})) == hash(Record({'a': int}))
    with pytest.raises(AttributeError):
        narrow.open = True


# ---- ports, nodes, edges ---------------------------------------------------------------------------------

def test_ports_and_nodes_validate_their_names():
    with pytest.raises(PortError):
        Port('Bad Name')
    with pytest.raises(NodeError):
        Step('Bad Id')
    with pytest.raises(NodeError, match='duplicate port'):
        Step('a', inputs=[Port('x'), Port('x')])

    class Nameless(Node):
        def run(self, ctx, inputs):
            return None
    with pytest.raises(NodeError, match='no kind'):
        Nameless('a')
    with pytest.raises(TypeError):
        Node('a')                                # run is abstract


def test_a_node_declares_ports_on_the_class_or_per_instance():
    a, b = Step('a'), Step('b', inputs=[Port('left', int), Port('right', int, required=False)])
    assert list(a.inputs) == ['in'] and list(b.inputs) == ['left', 'right'] and list(b.outputs) == ['out']
    assert Step.inputs == (Port('in'),)          # the class declaration is untouched
    with pytest.raises(TypeError):
        a.inputs['x'] = Port('x')
    assert a.describe()['config'] == {'note': ''} and a.describe()['kind'] == 'step'


def test_edges_check_endpoints_and_back_edges_need_a_round_limit():
    edge = Edge('e', 'a.out', ('b', 'in'), 'plain')
    assert edge.source == Endpoint('a', 'out') and str(edge.target) == 'b.in'
    with pytest.raises(EdgeError):
        Edge('e', 'a', 'b.in', 'plain')
    with pytest.raises(EdgeError, match='max_rounds'):
        Edge('e', 'a.out', 'b.in', 'plain', back=True)
    with pytest.raises(EdgeError, match='only a back edge'):
        Edge('e', 'a.out', 'b.in', 'plain', max_rounds=3)
    with pytest.raises(ProtocolError):
        Protocols([Plain(), Plain()])


# ---- the graph ---------------------------------------------------------------------------------------------

def test_a_chain_compiles_with_its_indexes():
    graph = chain('a', 'b', 'c').output('result', 'c.out').compile(PROTOCOLS)
    assert graph.order == ('a', 'b', 'c') and graph.sources == ('a',) and len(graph) == 3 and 'b' in graph
    assert [e.id for e in graph.outgoing('a')] == ['a.out>b.in'] == [e.id for e in graph.incoming('b', 'in')]
    assert graph.outgoing('c') == () and graph.incoming('a') == ()
    assert [graph.needs(n) for n in graph.order] == [0, 1, 1]
    assert graph.outputs['result'] == Endpoint('c', 'out') and graph.node('b').id == 'b'
    with pytest.raises(GraphError):
        graph.node('zz')
    with pytest.raises(AttributeError):
        graph.extra = 1
    with pytest.raises(TypeError):
        graph.outputs['more'] = Endpoint('a', 'out')


def test_the_digest_depends_on_the_definition_and_not_on_the_order_of_building():
    one = chain('a', 'b', 'c').compile(PROTOCOLS)
    other = (GraphBuilder().add(Step('c'), Step('b'), Start('a'))
             .connect('b.out', 'c.in', 'plain').connect('a.out', 'b.in', 'plain').compile(PROTOCOLS))
    assert one.digest == other.digest == digest(one.describe()) and len(one.digest) == 64
    renamed = GraphBuilder().add(Start('a'), Step('b', note='x'), Step('c'))
    renamed.connect('a.out', 'b.in', 'plain').connect('b.out', 'c.in', 'plain')
    assert renamed.compile(PROTOCOLS).digest != one.digest          # a node's config is part of the definition
    strict = GraphBuilder().add(Start('a'), Step('b'), Step('c'))
    strict.connect('a.out', 'b.in', 'strict').connect('b.out', 'c.in', 'plain')
    assert strict.compile(PROTOCOLS).digest != one.digest           # so is an edge's protocol


def test_fan_out_and_fan_in():
    join = Step('join', inputs=[Port('parts', many=True)])
    builder = GraphBuilder().add(Start('a'), Step('left'), Step('right'), join)
    builder.connect('a.out', 'left.in', 'plain').connect('a.out', 'right.in', 'plain')
    builder.connect('left.out', 'join.parts', 'plain').connect('right.out', 'join.parts', 'plain')
    graph = builder.compile(PROTOCOLS)
    assert len(graph.outgoing('a', 'out')) == 2 and len(graph.incoming('join', 'parts')) == 2
    assert graph.order[0] == 'a' and graph.order[-1] == 'join' and graph.needs('join') == 1

    single = GraphBuilder().add(Start('a'), Start('b'), Step('c'))
    single.connect('a.out', 'c.in', 'plain').connect('b.out', 'c.in', 'plain')
    with pytest.raises(GraphError, match='takes one feed, 2'):
        single.compile(PROTOCOLS)


def test_required_inputs_need_a_feed_and_graph_inputs_count_as_one():
    with pytest.raises(GraphError, match='b.in is required and nothing feeds it'):
        GraphBuilder().add(Step('b')).compile(PROTOCOLS)
    graph = GraphBuilder().add(Step('b')).input('question', 'b.in').output('answer', 'b.out').compile(PROTOCOLS)
    assert graph.needs('b') == 1 and graph.sources == () and graph.inputs['question'] == Endpoint('b', 'in')
    optional = Step('b', inputs=[Port('in', required=False)])
    assert GraphBuilder().add(optional).compile(PROTOCOLS).sources == ('b',)
    with pytest.raises(GraphError, match='takes one feed'):
        chain('a', 'b').input('extra', 'b.in').compile(PROTOCOLS)


def test_a_node_that_fires_on_any_input_needs_one_arrival():
    merge = Step('m', inputs=[Port('x', required=False), Port('y', required=False)], firing=Firing.ANY)
    builder = GraphBuilder().add(Start('a'), Start('b'), merge)
    graph = builder.connect('a.out', 'm.x', 'plain').connect('b.out', 'm.y', 'plain').compile(PROTOCOLS)
    assert graph.needs('m') == 1 and graph.sources == ('a', 'b')
    with pytest.raises(GraphError, match='nothing feeds it'):
        GraphBuilder().add(Step('m', inputs=[Port('x', required=False)], firing=Firing.ANY)).compile(PROTOCOLS)


def test_loops_need_a_back_edge_with_a_limit_and_the_back_edge_must_close_a_loop():
    cyclic = chain('a', 'b', 'c').connect('c.out', 'b.in', 'plain', id='again')
    with pytest.raises(GraphError, match='takes one feed'):
        cyclic.compile(PROTOCOLS)                                  # as a forward edge it is a second feed of b.in
    work = Step('b', inputs=[Port('in'), Port('redo', required=False)])
    forward = GraphBuilder().add(Start('a'), work, Step('c'))
    forward.connect('a.out', 'b.in', 'plain').connect('b.out', 'c.in', 'plain').connect('c.out', 'b.redo', 'plain')
    with pytest.raises(GraphError, match='a loop needs a back edge: b, c'):
        forward.compile(PROTOCOLS)

    loop = chain('a', 'b', 'c').connect('c.out', 'b.in', 'plain', id='again', back=True, max_rounds=5)
    graph = loop.compile(PROTOCOLS)
    assert graph.order == ('a', 'b', 'c') and graph.needs('b') == 1           # the back edge is not a first feed
    assert graph.edge('again').max_rounds == 5 and len(graph.incoming('b', 'in')) == 2
    own = chain('a', 'b').connect('b.out', 'b.in', 'plain', back=True, max_rounds=2).compile(PROTOCOLS)
    assert len(own.incoming('b')) == 2                                      # a node may loop on itself

    with pytest.raises(GraphError, match='closes no loop'):
        chain('a', 'b', 'c').connect('b.out', 'c.in', 'plain', id='x', back=True, max_rounds=2).compile(PROTOCOLS)
    with pytest.raises(GraphError, match='max_rounds'):
        chain('a', 'b').connect('b.out', 'b.in', 'plain', back=True)


@pytest.mark.parametrize('build, message', [
    (lambda: GraphBuilder().compile(PROTOCOLS), 'at least one node'),
    (lambda: GraphBuilder().add(Start('a'), Start('a')), 'duplicate node'),
    (lambda: GraphBuilder().add('a'), 'not a node'),
    (lambda: chain('a', 'b').connect('a.out', 'b.in', 'plain'), 'duplicate edge'),
    (lambda: chain('a', 'b').connect('a.out', 'zz.in', 'plain').compile(PROTOCOLS), "no node 'zz'"),
    (lambda: chain('a', 'b').connect('a.nope', 'b.in', 'plain').compile(PROTOCOLS), "no output port 'nope'"),
    (lambda: chain('a', 'b').connect('b.out', 'a.out', 'plain').compile(PROTOCOLS), "no input port 'out'"),
    (lambda: GraphBuilder().add(Start('a'), Step('b')).connect('a.out', 'b.in', 'telepathy').compile(PROTOCOLS), 'unknown protocol'),
    (lambda: chain('a', 'b').output('r', 'b.in').compile(PROTOCOLS), "graph output r: b has no output port 'in'"),
    (lambda: chain('a', 'b').output('r', 'b.out').output('r', 'b.out'), 'duplicate graph port'),
    (lambda: chain('a', 'b').connect('a', 'b.in', 'plain'), "expected 'node.port'"),
])
def test_what_compile_refuses(build, message):
    with pytest.raises(GraphError, match=message):
        build()


def test_a_protocol_decides_which_ports_an_edge_may_join():
    numbers, text = Start('a', outputs=[Port('out', int)]), Step('b', inputs=[Port('in', str)])
    with pytest.raises(GraphError, match='edge a.out>b.in: in does not accept what out produces'):
        GraphBuilder().add(numbers, text).connect('a.out', 'b.in', 'plain').compile(PROTOCOLS)
    loose = Step('b', inputs=[Port('in', (int, float))])
    builder = GraphBuilder().add(Start('a', outputs=[Port('out', int)]), loose)
    assert builder.connect('a.out', 'b.in', 'plain').compile(PROTOCOLS)
    with pytest.raises(GraphError, match='schemas differ'):
        GraphBuilder().add(Start('a', outputs=[Port('out', int)]), Step('b', inputs=[Port('in', (int, float))])) \
            .connect('a.out', 'b.in', 'strict').compile(PROTOCOLS)
    assert Plain().encode(3, Port('out', int)) == 3
    with pytest.raises(SchemaError):
        Plain().encode('three', Port('out', int))


def test_ten_thousand_nodes_compile():
    ids = [f'n{i}' for i in range(10_000)]
    graph = chain(*ids).compile(PROTOCOLS)
    assert graph.order[0] == 'n0' and graph.order[-1] == 'n9999' and len(graph.edges) == 9_999


def test_the_abstract_layer_imports_only_itself_and_the_standard_library():
    for path in BASE.glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.ImportFrom):
                assert node.level == 1 or (node.level == 0 and node.module.split('.')[0] in sys.stdlib_module_names), \
                    f'{path.name}:{node.lineno}'
            elif isinstance(node, ast.Import):
                assert all(a.name.split('.')[0] in sys.stdlib_module_names for a in node.names), f'{path.name}:{node.lineno}'
