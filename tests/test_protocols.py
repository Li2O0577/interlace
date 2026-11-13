"""The protocols beyond ``value``, the transport, and the nodes that stand for things outside the graph."""
import sys
import threading
import time

import pytest

from indeterminate.base import Firing, GraphBuilder, GraphError, NodeError, Port, ProtocolError, Ref
from indeterminate.engine import Direct, Engine, RunError, Stream, Wire
from indeterminate.methods import Chain
from indeterminate.nodes import FunctionNode, HumanNode, ProcessNode, SinkNode, SourceNode
from indeterminate.protocols import DEFAULT, Artifact, DirectoryStore, MemoryStore, StoreError, standard

WORKERS = [0, 1, 4]


def fn(id, function, inputs=('x',), outputs=('y',), **options):
    ports = lambda names: [p if isinstance(p, Port) else Port(p) for p in names]   # noqa: E731
    return FunctionNode(id, function, inputs=ports(inputs), outputs=ports(outputs), **options)


def run(graph, inputs=None, workers=0, protocols=DEFAULT, **options):
    return Engine(graph, protocols, workers=workers, **options).run(inputs)


def burst(count):
    return lambda ctx: [ctx.emit('y', i) for i in range(count)] and None


# ---- state: read, never started by ---------------------------------------------------------------------

@pytest.mark.parametrize('workers', [0, 1])
def test_a_state_edge_is_read_whenever_the_node_runs_and_never_starts_it(workers):
    seen = []
    use = fn('use', lambda x, setting=None: (seen.append((x, setting)), x)[1], inputs=['x', Port('setting', required=False)])
    builder = GraphBuilder().add(fn('config', lambda: 'fast', inputs=()), fn('work', burst(3), inputs=(), context=True), use)
    graph = builder.connect('config.y', 'use.setting', 'state').connect('work.y', 'use.x', 'value').output('out', 'use.y').compile(DEFAULT)
    result = run(graph, workers=workers)
    assert seen == [(0, 'fast'), (1, 'fast'), (2, 'fast')] and result.rounds['use'] == 3     # the value stays there

    alone = GraphBuilder().add(fn('config', lambda: 'fast', inputs=()), fn('use', lambda setting=None: 1, inputs=[Port('setting', required=False)]))
    result = run(alone.connect('config.y', 'use.setting', 'state').compile(DEFAULT))
    assert result.rounds['use'] == 1                         # it started by itself once, not because the state arrived


def test_a_state_edge_needs_an_optional_port_of_its_own():
    required = GraphBuilder().add(fn('a', lambda: 1, inputs=()), fn('b', lambda x: x)).connect('a.y', 'b.x', 'state').compile(DEFAULT)
    with pytest.raises(RunError, match='b.x is fed by an edge that does not trigger'):
        Engine(required, DEFAULT)


# ---- signal --------------------------------------------------------------------------------------------

def test_a_signal_carries_nothing():
    got = []
    graph = GraphBuilder().add(fn('a', lambda: 'ignored', inputs=()), fn('b', lambda x: (got.append(x), 'went')[1])) \
        .connect('a.y', 'b.x', 'signal').output('out', 'b.y').compile(DEFAULT)
    assert run(graph).outputs == {'out': ['went']} and got == [None]
    with pytest.raises(Exception, match='must accept None to take a signal'):
        GraphBuilder().add(fn('a', lambda: 1, inputs=()), fn('b', lambda x: x, inputs=[Port('x', int)])).connect('a.y', 'b.x', 'signal').compile(DEFAULT)


# ---- stream --------------------------------------------------------------------------------------------

@pytest.mark.parametrize('workers', WORKERS)
def test_a_stream_is_one_sequence_per_sending_activation(workers):
    def speak(ctx, x):
        for word in x.split():
            ctx.emit('y', word)
    kinds = []
    listen = fn('listen', lambda x: (kinds.append(type(x)), ' '.join(w.upper() for w in x))[1])
    graph = GraphBuilder().add(fn('texts', lambda ctx: [ctx.emit('y', t) for t in ('a b c', 'd e')] and None, inputs=(), context=True),
                               fn('speak', speak, context=True), listen) \
        .connect('texts.y', 'speak.x', 'value').connect('speak.y', 'listen.x', 'stream').output('out', 'listen.y').compile(DEFAULT)
    result = run(graph, workers=workers)
    assert result.outputs == {'out': ['A B C', 'D E']} and kinds == [Stream, Stream]
    assert result.rounds == {'texts': 1, 'speak': 2, 'listen': 2}
    assert result.envelopes == 4                             # two texts, two streams: chunks are not envelopes


def test_a_stream_is_read_while_it_is_still_being_written():
    first_read, order = threading.Event(), []

    def write(ctx):
        ctx.emit('y', 1)
        assert first_read.wait(5), 'the reader never saw the first chunk while the writer was running'
        order.append('writer goes on')
        ctx.emit('y', 2)

    def read(x):
        total = 0
        for chunk in x:
            total += chunk
            order.append(f'read {chunk}')
            first_read.set()
        return total
    graph = GraphBuilder().add(fn('w', write, inputs=(), context=True), fn('r', read)).connect('w.y', 'r.x', 'stream').output('out', 'r.y').compile(DEFAULT)
    assert run(graph, workers=2).outputs == {'out': [3]}
    assert order.index('read 1') < order.index('writer goes on') < order.index('read 2')


def test_a_stream_ends_when_its_sender_fails():
    def write(ctx):
        ctx.emit('y', 1)
        raise ValueError('lost the device')
    got = []
    graph = GraphBuilder().add(fn('w', write, inputs=(), context=True), fn('r', lambda x: got.extend(x))).connect('w.y', 'r.x', 'stream').compile(DEFAULT)
    with pytest.raises(RunError, match='node w failed in round 1: lost the device'):
        run(graph, workers=2)


# ---- call ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize('workers', WORKERS)
def test_a_call_is_answered_within_the_callers_activation(workers):
    def agent(ctx, x):
        return ctx.call('tool', x) + ctx.call('tool', x * 10)
    graph = GraphBuilder().add(fn('agent', agent, outputs=['y', Port('tool', call=True)], context=True), fn('double', lambda x: x * 2)) \
        .connect('agent.tool', 'double.x', 'call').input('in', 'agent.x').output('out', 'agent.y').compile(DEFAULT)
    result = run(graph, {'in': 3}, workers)
    assert result.outputs == {'out': [66]} and result.rounds == {'agent': 1, 'double': 2}
    assert result.activations == 3 and result.envelopes == 3     # the input and the two requests


def test_a_called_node_runs_only_when_called_one_call_at_a_time_and_its_failure_reaches_the_caller():
    inside, most = [0], [0]

    def tool(x):
        inside[0] += 1
        most[0] = max(most[0], inside[0])
        time.sleep(0.01)
        inside[0] -= 1
        if x < 0:
            raise ValueError('negative')
        return x + 1

    def caller(ctx, x):
        try:
            return ctx.call('tool', x)
        except ValueError as exc:
            return f'refused: {exc}'
    builder = GraphBuilder().add(fn('split', lambda x: x), fn('tool', tool))
    for name in ('a', 'b', 'c'):
        builder.add(fn(name, caller, outputs=['y', Port('tool', call=True)], context=True)).connect('split.y', f'{name}.x', 'value')
        builder.connect(f'{name}.tool', 'tool.x', 'call').output(name, f'{name}.y')
    graph = builder.input('in', 'split.x').compile(DEFAULT)
    result = run(graph, {'in': 1}, workers=4)
    assert result.outputs == {'a': [2], 'b': [2], 'c': [2]} and most[0] == 1 and result.rounds['tool'] == 3
    assert run(graph, {'in': -1}).outputs == {'a': ['refused: negative'], 'b': ['refused: negative'], 'c': ['refused: negative']}


def test_what_a_call_port_and_a_called_node_may_not_do():
    def emits(ctx, x):
        ctx.emit('tool', x)
    graph = GraphBuilder().add(fn('a', emits, outputs=['y', Port('tool', call=True)], context=True), fn('t', lambda x: x)) \
        .connect('a.tool', 't.x', 'call').input('in', 'a.x').compile(DEFAULT)
    with pytest.raises(RunError, match='a.tool makes a call: use ctx.call, not emit'):
        run(graph, {'in': 1})
    with pytest.raises(RunError, match="no call port 'y'"):   # y is an ordinary output
        run(GraphBuilder().add(fn('a', lambda ctx, x: ctx.call('y', x), context=True)).input('in', 'a.x').compile(DEFAULT), {'in': 1})
    fed = GraphBuilder().add(fn('a', lambda ctx, x: 1, outputs=['y', Port('tool', call=True)], context=True), fn('s', lambda: 1, inputs=()),
                             fn('t', lambda x, extra=None: x, inputs=['x', Port('extra', required=False)])) \
        .connect('a.tool', 't.x', 'call').connect('s.y', 't.extra', 'value').input('in', 'a.x').compile(DEFAULT)
    with pytest.raises(RunError, match='t is called, so nothing else may feed it'):
        Engine(fed, DEFAULT)
    with pytest.raises(GraphError, match='tool is a call port and needs a call edge'):
        GraphBuilder().add(fn('a', lambda ctx, x: 1, outputs=['y', Port('tool', call=True)], context=True), fn('u', lambda x: x)) \
            .connect('a.tool', 'u.x', 'value').input('in', 'a.x').compile(DEFAULT)
    with pytest.raises(GraphError, match='a call edge must leave a port declared with call=True'):
        GraphBuilder().add(fn('a', lambda x: 1), fn('u', lambda x: x)).connect('a.y', 'u.x', 'call').input('in', 'a.x').compile(DEFAULT)
    two = GraphBuilder().add(fn('a', lambda ctx, x: 1, outputs=['y', Port('tool', call=True)], context=True), fn('t', lambda x: x), fn('u', lambda x: x)) \
        .connect('a.tool', 't.x', 'call').connect('a.tool', 'u.x', 'call').input('in', 'a.x').compile(DEFAULT)
    with pytest.raises(RunError, match='exactly one edge may leave it'):
        Engine(two, DEFAULT)


def test_a_called_node_with_several_outputs_answers_through_reply_and_may_send_elsewhere():
    log = []
    tool = fn('tool', lambda x: {'reply': x + 1, 'audit': f'asked {x}'}, outputs=['reply', 'audit'])
    graph = GraphBuilder().add(fn('a', lambda ctx, x: ctx.call('tool', x), outputs=['y', Port('tool', call=True)], context=True), tool,
                               fn('log', lambda x: log.append(x), outputs=())) \
        .connect('a.tool', 'tool.x', 'call').connect('tool.audit', 'log.x', 'value').input('in', 'a.x').output('out', 'a.y').compile(DEFAULT)
    assert run(graph, {'in': 1}).outputs == {'out': [2]} and log == ['asked 1']


# ---- artifact and stores -------------------------------------------------------------------------------

@pytest.mark.parametrize('payload', ['一段文字', b'\x00\x01raw', {'k': [1, 2.5, None]}])
def test_an_artifact_travels_as_a_reference_and_arrives_as_it_was_sent(payload):
    store = MemoryStore()
    protocols = standard(store)
    engine = Engine(GraphBuilder().add(fn('a', lambda: payload, inputs=()), fn('b', lambda x: x))
                    .connect('a.y', 'b.x', 'artifact').output('out', 'b.y').compile(protocols), protocols, transport=Wire())
    assert engine.run().outputs == {'out': [payload]} and len(store) == 1
    with pytest.raises(ProtocolError, match='an artifact arrives as a Ref'):
        Artifact(store).decode('text', Port('x'))
    with pytest.raises(ProtocolError, match='text, bytes or plain data'):
        Artifact(store).encode(object(), Port('y'))
    with pytest.raises(ProtocolError, match='not a store'):
        Artifact('here')


def test_stores_keep_content_under_its_hash(tmp_path):
    for store in (MemoryStore(), DirectoryStore(tmp_path / 'store')):
        ref = store.put(b'hello', 'text/plain')
        assert ref == store.put(b'hello', 'text/plain') and ref.size == 5 and len(ref.digest) == 64
        assert store.get(ref) == b'hello'
        with pytest.raises(StoreError, match='no content'):
            store.get(Ref('0' * 64))
        with pytest.raises(TypeError):
            store.put('text')
    directory = DirectoryStore(tmp_path / 'store')
    path = tmp_path / 'store' / ref.digest[:2] / ref.digest[2:]
    assert path.read_bytes() == b'hello' and [p.name for p in path.parent.iterdir()] == [path.name]
    path.write_bytes(b'changed')
    with pytest.raises(StoreError, match='does not match its hash'):
        directory.get(ref)
    with pytest.raises(StoreError, match='not a content hash'):
        directory.get(Ref('../../etc/passwd'))


# ---- transport -----------------------------------------------------------------------------------------

def test_a_transport_stands_between_sending_and_receiving():
    shared = {'k': [1]}
    graph = lambda: Chain(fn('a', lambda x: shared), fn('b', lambda x: x)).build(DEFAULT)   # noqa: E731
    assert run(graph(), {'in': 0}).last('out') is shared
    assert run(graph(), {'in': 0}, transport=Direct()).last('out') is shared
    wire = Wire()
    arrived = run(graph(), {'in': 0}, transport=wire).last('out')
    assert arrived == shared and arrived is not shared and wire.sent == len('{"k":[1]}')
    with pytest.raises(RunError, match='only plain data can be carried'):
        run(Chain(fn('a', lambda x: object()), fn('b', lambda x: x)).build(DEFAULT), {'in': 0}, transport=Wire())


# ---- nodes for what is outside -------------------------------------------------------------------------

def test_a_source_and_a_sink_connect_once_and_disconnect_at_the_end():
    events, written = [], []
    camera = SourceNode('camera', lambda: iter(['f1', 'f2', 'f3']), schema=str,
                        connect=lambda: events.append('camera on'), disconnect=lambda: events.append('camera off'))
    arm = SinkNode('arm', written.append, connect=lambda: events.append('arm on'), disconnect=lambda: events.append('arm off'))
    graph = GraphBuilder().add(camera, fn('see', lambda x: x.upper()), arm).connect('camera.out', 'see.x', 'value') \
        .connect('see.y', 'arm.x', 'value').compile(DEFAULT)
    result = run(graph)
    assert written == ['F1', 'F2', 'F3'] and result.rounds == {'camera': 1, 'see': 3, 'arm': 3}
    assert events == ['camera on', 'arm on', 'camera off', 'arm off']
    assert camera.describe()['kind'] == 'source' and arm.describe()['kind'] == 'sink' and not arm.outputs
    with pytest.raises(NodeError, match='read is not callable'):
        SourceNode('s', [1, 2])


def test_a_device_that_answers_can_be_called():
    position = [0]

    def move(by):
        position[0] += by
        return position[0]

    def plan(ctx, x):
        return [ctx.call('arm', step) for step in x]
    graph = GraphBuilder().add(fn('plan', plan, outputs=['y', Port('arm', call=True)], context=True), SinkNode('arm', move, answers=True)) \
        .connect('plan.arm', 'arm.x', 'call').input('in', 'plan.x').output('out', 'plan.y').compile(DEFAULT)
    assert run(graph, {'in': [2, 3, -1]}).outputs == {'out': [[2, 5, 4]]}


def test_a_source_stops_reading_when_the_run_stops():
    read, failed = [], threading.Event()

    def readings():
        for i in range(10_000):
            read.append(i)
            if i == 100:
                failed.wait(10)                 # however fast the source is, the other node gets its turn
            yield i

    def fail(x):
        if x == 3:
            failed.set()
            raise ValueError('enough')
    with pytest.raises(RunError, match='enough'):
        run(GraphBuilder().add(SourceNode('s', readings), fn('f', fail, outputs=())).connect('s.out', 'f.x', 'value').compile(DEFAULT), workers=2)
    assert len(read) < 10_000


def test_a_person_is_a_node():
    asked = []

    def ask(request):
        asked.append(request)
        return {'approved': request['cost'] < 100}
    human = HumanNode('operator', ask, request={'cost': int}, answer={'approved': bool})
    graph = Chain(fn('propose', lambda x: {'cost': x}, outputs=[Port('y', {'cost': int})]), human,
                  fn('act', lambda x: 'done' if x['approved'] else 'held')).build(DEFAULT)
    assert run(graph, {'in': 30}).last('out') == 'done' and asked == [{'cost': 30}]
    assert run(Chain(fn('propose', lambda x: {'cost': x}, outputs=[Port('y', {'cost': int})]),
                     HumanNode('operator', ask, request={'cost': int}), fn('act', lambda x: x)).build(DEFAULT), {'in': 500}).last('out') == {'approved': False}
    assert human.describe()['kind'] == 'human' and human.firing is Firing.ALL


# ---- process -------------------------------------------------------------------------------------------

PY = sys.executable


def test_a_program_is_a_node():
    upper = ProcessNode('upper', [PY, '-c', 'import sys; sys.stdout.write(sys.stdin.read().upper())'])
    assert run(Chain(fn('a', lambda x: x + '!'), upper).build(DEFAULT), {'in': 'hi'}).last('out') == 'HI!'
    total = ProcessNode('total', [PY, '-c', 'import sys, json; print(json.dumps({"sum": sum(json.load(sys.stdin))}))'], format='json')
    assert run(Chain(total).build(DEFAULT), {'in': [1, 2, 3]}).last('out') == {'sum': 6}
    raw = ProcessNode('raw', [PY, '-c', 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])'], format='bytes')
    assert run(Chain(raw).build(DEFAULT), {'in': b'\x00\x01\x02'}).last('out') == b'\x02\x01\x00'
    alone = ProcessNode('alone', [PY, '-c', 'print("ready")'], takes_input=False)
    assert not alone.inputs and run(Chain(alone).build(DEFAULT)).last('out') == 'ready\n'
    assert upper.config() == {'command': upper.command, 'format': 'text', 'lines': False, 'timeout': None, 'cwd': None}


def test_a_program_can_send_its_output_line_by_line():
    count = ProcessNode('count', [PY, '-u', '-c', 'import sys\nfor i in range(int(sys.stdin.read())): print(i * i)'], format='json', lines=True)
    result = run(Chain(count, fn('b', lambda x: x + 1)).build(DEFAULT), {'in': 4})
    assert result.outputs == {'out': [1, 2, 5, 10]} and result.rounds == {'count': 1, 'b': 4}


def test_a_program_that_fails_runs_too_long_or_does_not_exist_fails_the_node():
    bad = ProcessNode('bad', [PY, '-c', 'import sys; sys.stderr.write("no such table"); sys.exit(3)'], takes_input=False)
    with pytest.raises(RunError, match='node bad failed in round 1: .* exited with 3: no such table'):
        run(Chain(bad).build(DEFAULT))
    slow = ProcessNode('slow', [PY, '-c', 'import time; time.sleep(30)'], takes_input=False, timeout=0.3)
    began = time.perf_counter()
    with pytest.raises(RunError, match='ran longer than 0.3 seconds'):
        run(Chain(slow).build(DEFAULT))
    assert time.perf_counter() - began < 10
    with pytest.raises(RunError, match='cannot start no-such-program-here'):
        run(Chain(ProcessNode('none', ['no-such-program-here'], takes_input=False)).build(DEFAULT))
    for wrong in ('echo hi', [], [PY, 3]):
        with pytest.raises(NodeError, match='command is a list'):
            ProcessNode('p', wrong)
    with pytest.raises(NodeError, match='format is one of'):
        ProcessNode('p', [PY], format='xml')
    with pytest.raises(NodeError, match='lines needs text or json'):
        ProcessNode('p', [PY], format='bytes', lines=True)


def test_a_program_is_killed_when_the_run_stops():
    slow = ProcessNode('slow', [PY, '-c', 'import time; time.sleep(30)'], takes_input=False)

    def fail():
        time.sleep(0.2)
        raise ValueError('stop everything')
    graph = GraphBuilder().add(slow, fn('fail', fail, inputs=(), outputs=())).compile(DEFAULT)
    began = time.perf_counter()
    with pytest.raises(RunError, match='stop everything'):
        run(graph, workers=2)
    assert time.perf_counter() - began < 10
