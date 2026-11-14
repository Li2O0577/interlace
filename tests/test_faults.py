"""Runs broken at random places, with the journal cut short as a dying process would leave it, come out the
same as runs that were never broken; many threads give what one gives."""
import random
import sys
import threading
import time

import pytest

from inter1ace.base import GraphBuilder, Port
from inter1ace.engine import Engine, Journal, RunError
from inter1ace.methods import Chain, Fan, Loop
from inter1ace.nodes import FunctionNode, GraphNode, ProcessNode
from inter1ace.protocols import DEFAULT

LOCK = threading.Lock()
FUSE = {'left': None}


class Blown(Exception):
    pass


def tick():
    """Every node passes through here; when the fuse runs out, the node that is passing fails."""
    with LOCK:
        if FUSE['left'] is not None:
            FUSE['left'] -= 1
            if FUSE['left'] < 0:
                raise Blown('the fuse blew')


def mix(id):
    def run(**inputs):
        tick()
        return f"{id}({','.join(str(inputs[name]) for name in sorted(inputs))})"
    return run


def layered(seed: int):
    """A graph of layers in which every node takes from one to three nodes of earlier layers; one node is a
    block with a loop inside it, one a fan."""
    rng = random.Random(seed)
    builder, earlier, last = GraphBuilder(), [], []
    for layer in range(rng.randint(3, 6)):
        last = []
        for index in range(rng.randint(1, 5)):
            id = f'n{layer}_{index}'
            sources = rng.sample(earlier, min(len(earlier), rng.randint(1, 3))) if earlier else []
            ports = [Port(f'p{i}') for i in range(max(1, len(sources)))]
            shape = rng.random()
            if len(ports) == 1 and shape < 0.15:
                step = FunctionNode('step', lambda x: (tick(), str(x) + '+')[1], inputs=[Port('x')], outputs=[Port('y')])
                node = GraphNode(id, Loop(step, until=lambda x: x.endswith('+++'), max_rounds=5).build(DEFAULT), DEFAULT)
                ports, out = [Port('in')], 'out'
            elif len(ports) == 1 and shape < 0.3:
                parts = [FunctionNode(f'f{i}', mix(f'{id}.f{i}'), inputs=[Port('x')], outputs=[Port('y')]) for i in range(3)]
                node = GraphNode(id, Fan(*parts).build(DEFAULT), DEFAULT, workers=2)
                ports, out = [Port('in')], 'out'
            else:
                node, out = FunctionNode(id, mix(id), inputs=ports, outputs=[Port('y')]), 'y'
            builder.add(node)
            for port, source in zip(ports, sources):
                builder.connect(source, f'{id}.{port.name}', 'value')
            if not sources:
                builder.input(f'in_{id}', f'{id}.{ports[0].name}')
            last.append(f'{id}.{out}')
        earlier += last
    for end in earlier:
        builder.output('out_' + end.replace('.', '_'), end)
    graph = builder.compile(DEFAULT)
    return graph, {name: name for name in graph.inputs}


def cut(path, rng):
    """Leave the journal as a process that died while writing its last record would."""
    data = path.read_bytes()
    if data and rng.random() < 0.5:
        end = data.rstrip(b'\n').rfind(b'\n') + 1
        path.write_bytes(data[:end + rng.randint(0, len(data) - end - 1)])


@pytest.mark.parametrize('workers', [0, 4])
def test_runs_broken_at_random_places_end_as_unbroken_ones_do(tmp_path, workers):
    for seed in range(40):
        FUSE['left'] = None
        graph, inputs = layered(seed)
        whole = Engine(graph, DEFAULT).run(inputs)
        rng, path, breaks = random.Random(seed * 7 + workers), tmp_path / f'{workers}-{seed}.jsonl', 0
        FUSE['left'] = rng.randint(0, 6)
        try:
            result = Engine(layered(seed)[0], DEFAULT, workers=workers, journal=Journal(path)).run(inputs)
        except RunError:
            result = None
        while result is None:
            breaks += 1
            assert breaks < 200, f'seed {seed} never finished'
            cut(path, rng)
            FUSE['left'] = rng.randint(0, 8)
            try:
                if not Journal(path).read():                       # it died before the first record was whole
                    result = Engine(layered(seed)[0], DEFAULT, workers=workers, journal=Journal(path)).run(inputs)
                else:
                    result = Engine(layered(seed)[0], DEFAULT, workers=workers, journal=Journal(path)).resume()
            except RunError as exc:
                assert 'the fuse blew' in str(exc), f'seed {seed}: {exc}'
        FUSE['left'] = None
        assert result.outputs == whole.outputs, f'seed {seed} after {breaks} breaks'
        assert result.rounds == whole.rounds, f'seed {seed} after {breaks} breaks'
        assert not list(tmp_path.glob(f'{workers}-{seed}.jsonl.d/*')), 'journals of inner runs were left behind'
    FUSE['left'] = None


def test_many_threads_give_what_one_thread_gives():
    FUSE['left'] = None
    for seed in range(40, 70):
        graph, inputs = layered(seed)
        one = Engine(graph, DEFAULT).run(inputs)
        for workers in (2, 8):
            many = Engine(layered(seed)[0], DEFAULT, workers=workers).run(inputs)
            assert many.outputs == one.outputs and many.rounds == one.rounds, f'seed {seed}, {workers} workers'


def test_a_long_fan_under_many_threads_loses_nothing():
    seen, lock = [], threading.Lock()

    def note(x):
        with lock:
            seen.append(x)
        return x
    parts = [FunctionNode(f'w{i}', note, inputs=[Port('x')], outputs=[Port('y')]) for i in range(200)]
    source = FunctionNode('source', lambda ctx, x: [ctx.emit('y', n) for n in range(x)] and None, context=True, inputs=[Port('x')], outputs=[Port('y')])
    result = Engine(Chain(source, GraphNode('fan', Fan(*parts).build(DEFAULT), DEFAULT, workers=16)).build(DEFAULT), DEFAULT, workers=4).run({'in': 25})
    assert len(seen) == 200 * 25 and sorted(set(seen)) == list(range(25))
    assert result.rounds['fan'] == 25 and len(result.outputs['out']) == 25 and all(len(o) == 200 for o in result.outputs['out'])


def test_a_program_that_overruns_the_engines_limit_is_killed(tmp_path):
    marker = tmp_path / 'alive'
    slow = ProcessNode('slow', [sys.executable, '-c', f'import time, pathlib\ntime.sleep(3)\npathlib.Path({str(marker)!r}).write_text("x")'],
                       takes_input=False)
    graph = GraphBuilder().add(slow).output('out', 'slow.out').compile(DEFAULT)
    began = time.monotonic()
    with pytest.raises(RunError, match='node slow ran longer than 0.3 seconds'):
        Engine(graph, DEFAULT, time_limit=0.3).run()
    assert time.monotonic() - began < 2.5
    time.sleep(3.2)
    assert not marker.exists()                  # the program did not live to write it


def test_a_node_that_was_told_to_stop_is_not_on_record_and_runs_again(tmp_path):
    ran, gate = [], threading.Event()

    def patient(ctx, x):
        ran.append('patient')
        gate.wait(5)
        if ctx.stopping():
            return None                         # cut short: nothing was sent
        return x * 2

    def breaks(x):
        if not ran.count('breaks'):
            ran.append('breaks')
            gate.set()
            raise OSError('once')
        return x + 1
    build = lambda: (GraphBuilder().add(FunctionNode('patient', patient, context=True, inputs=[Port('x')], outputs=[Port('y')]),    # noqa: E731
                                        FunctionNode('breaks', breaks, inputs=[Port('x')], outputs=[Port('y')]))
                     .input('a', 'patient.x').input('b', 'breaks.x').output('doubled', 'patient.y').output('next', 'breaks.y').compile(DEFAULT))
    seen = []
    with pytest.raises(RunError, match='node breaks failed in round 1: once'):
        Engine(build(), DEFAULT, workers=2, journal=Journal(tmp_path / 'j'), observe=seen.append).run({'a': 4, 'b': 4})
    assert ('stopped', 'patient') in [(e['event'], e.get('node')) for e in seen]
    result = Engine(build(), DEFAULT, workers=2, journal=Journal(tmp_path / 'j')).resume()
    assert result.outputs == {'doubled': [8], 'next': [5]} and ran == ['patient', 'breaks', 'patient']


def count(x):
    return len(x)


def test_a_run_that_died_before_its_first_record_starts_over_from_its_inputs(tmp_path):
    from inter1ace.runs import Run
    description = {'method': 'chain', 'nodes': [{'id': 'a', 'kind': 'function', 'function': f'{__name__}.count', 'inputs': ['x'], 'outputs': ['y']}]}
    run = Run.create(tmp_path / 'run', description, allow=[__name__])
    assert run.start({'in': 'four'})['outputs'] == {'out': [4]}
    journal = tmp_path / 'run' / 'journal.jsonl'
    journal.write_bytes(journal.read_bytes()[:20])          # as if the process had died while writing the first record
    assert Run(tmp_path / 'run').resume()['outputs'] == {'out': [4]}


def test_the_clock_is_watched_however_the_threads_fall():
    # the watcher used to go back to sleep between two activations and miss the start of the second
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)                 # threads change places as often as they can
    try:
        for attempt in range(150):
            release = threading.Event()
            quick = [FunctionNode(f'q{i}', lambda x: x, inputs=[Port('x')], outputs=[Port('y')]) for i in range(20)]
            slow = FunctionNode('slow', lambda x: release.wait(0.3), inputs=[Port('x')], outputs=[Port('y')])
            with pytest.raises(RunError, match='node slow ran longer than 0.05 seconds'):
                Engine(Chain(*quick, slow).build(DEFAULT), DEFAULT, time_limit=0.05).run({'in': attempt})
            release.set()
    finally:
        sys.setswitchinterval(interval)
