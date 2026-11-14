"""A function in a process of its own: it answers like any function, and it can be ended from outside."""
import os
import threading
import time

import pytest

from interlacing import DEFAULT, Catalog, Chain, Engine, GraphBuilder, IsolatedNode, NodeError, Port, RunError, SpecError, dump, load

HERE = __name__


def double(x):
    print('a function may print')                 # must not be taken for its answer
    return x * 2


def split(x):
    return {'low': min(x), 'high': max(x)}


def nothing(x):
    return None


def refuses(x):
    raise ValueError(f'{x} is no good')


def dies(x):
    os._exit(7)


def sleeps(x):
    """Leaves its process id where the test can find it, then outstays any limit."""
    with open(x, 'w') as file:
        file.write(str(os.getpid()))
    time.sleep(120)


def which(x):
    return os.getpid()


def node(function, outputs=('y',), **options):
    return IsolatedNode('work', function, inputs=[Port('x')], outputs=[Port(name) for name in outputs], **options)


def run(node, x, **options):
    return Engine(Chain(node).build(DEFAULT), DEFAULT, **options).run({'in': x})


def gone(path, seconds=10):
    """Whether the process that wrote its id to ``path`` has ended."""
    end = time.monotonic() + seconds
    while not os.path.exists(path) or not open(path).read():
        assert time.monotonic() < end, 'the process never started'
        time.sleep(0.01)
    pid = int(open(path).read())
    if os.name != 'posix':
        return True                               # asking is not safe there; the node's own check stands
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.01)
    return False


def test_a_function_runs_in_another_process_and_answers():
    assert run(node(double), 21).outputs == {'out': [42]}
    assert run(node(f'{HERE}.double'), 'ab').outputs == {'out': ['abab']}
    assert run(node(which), 0).last('out') != os.getpid()
    assert run(node(nothing), 1).outputs == {'out': []}
    graph = (GraphBuilder().add(node(split, outputs=('low', 'high'))).input('in', 'work.x')
             .output('low', 'work.low').output('high', 'work.high').compile(DEFAULT))
    assert Engine(graph, DEFAULT).run({'in': [3, 1, 2]}).outputs == {'low': [1], 'high': [3]}


def test_what_goes_wrong_there_fails_the_node_and_nothing_else():
    with pytest.raises(RunError, match='node work failed in round 1: ValueError: 5 is no good'):
        run(node(refuses), 5)
    with pytest.raises(RunError, match='ended with 7 and no answer'):
        run(node(dies), 1)
    with pytest.raises(RunError, match='must be plain data'):
        run(node(double), {1, 2})
    assert run(node(double), 1).outputs == {'out': [2]}             # the engine's own process is as it was


def test_a_function_that_overruns_the_run_s_limit_is_ended(tmp_path):
    began = time.monotonic()
    with pytest.raises(RunError, match='node work ran longer than 0.5 seconds'):
        run(node(sleeps), str(tmp_path / 'pid'), time_limit=0.5)
    assert time.monotonic() - began < 30 and gone(tmp_path / 'pid')


def test_a_function_that_overruns_its_own_limit_is_ended(tmp_path):
    with pytest.raises(RunError, match='ran longer than 0.5 seconds and was ended'):
        run(node(sleeps, timeout=0.5), str(tmp_path / 'pid'))
    assert gone(tmp_path / 'pid')


def test_stopping_the_run_ends_the_function(tmp_path):
    engine = Engine(Chain(node(sleeps)).build(DEFAULT), DEFAULT)
    threading.Timer(0.5, engine.stop).start()
    result = engine.run({'in': str(tmp_path / 'pid')})
    assert not result.complete and gone(tmp_path / 'pid')


def test_only_a_function_another_process_can_find_is_taken():
    for function in (lambda x: x, 'nowhere', 7):
        with pytest.raises(NodeError, match='cannot be found from another process|a function, or the dotted name'):
            node(function)

    def inner(x):
        return x
    with pytest.raises(NodeError, match='cannot be found from another process'):
        node(inner)
    with pytest.raises(NodeError, match='cannot make calls'):
        IsolatedNode('work', double, inputs=[Port('x')], outputs=[Port('ask', call=True)])
    with pytest.raises(NodeError, match='timeout must be'):
        node(double, timeout=0)


def test_it_is_written_out_and_read_back_where_processes_are_allowed():
    graph = Chain(node(double, timeout=30)).build(DEFAULT)
    described = dump(graph)
    assert described['nodes'][0]['kind'] == 'isolated'
    again = load(described, Catalog(allow=[HERE], processes=True))
    assert again.digest == graph.digest and Engine(again, DEFAULT).run({'in': 4}).outputs == {'out': [8]}
    with pytest.raises(SpecError, match='does not allow processes'):
        load(described, Catalog(allow=[HERE]))
    with pytest.raises(SpecError, match='not in a module this catalog allows'):
        load(described, Catalog(processes=True))
    written = {'method': 'chain', 'nodes': [{'id': 'work', 'kind': 'isolated', 'function': f'{HERE}.double',
                                             'inputs': ['x'], 'outputs': ['y']}]}
    assert Engine(load(written, Catalog(allow=[HERE], processes=True)), DEFAULT).run({'in': 5}).outputs == {'out': [10]}

