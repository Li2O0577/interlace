"""What a run uses is added up and kept under a budget; an activation has a key that survives a restart;
one process at a time runs a run directory."""
import io
import json
import os
import subprocess
import sys

import pytest

from indeterminate import cli
from indeterminate.base import Port
from indeterminate.engine import Engine, Journal, OverBudget, RunError
from indeterminate.methods import Chain
from indeterminate.models import ChatResult, FakeProvider
from indeterminate.nodes import AgentNode, FunctionNode, GraphNode
from indeterminate.protocols import DEFAULT
from indeterminate.runs import Run
from indeterminate.spec import Catalog, SpawnNode, SpecError

HERE = __name__
KEYS = []
BROKEN = []
WENT_ON = []


def spend(ctx, x):
    ctx.use(tokens=x, calls=1)
    return x


def stubborn(ctx, x):
    try:
        ctx.use(tokens=x)
    except OverBudget:
        WENT_ON.append(x)                   # a node that carries on regardless
    return x


def keyed(ctx, x):
    KEYS.append(ctx.key)
    if BROKEN:
        raise OSError('not yet')
    return x


def fn(id, function):
    return FunctionNode(id, function, context=True, inputs=[Port('x')], outputs=[Port('y')])


def node(id, function):
    return {'id': id, 'kind': 'function', 'function': f'{HERE}.{function}', 'context': True, 'inputs': ['x'], 'outputs': ['y']}


def test_what_nodes_report_using_is_added_up_and_told_to_the_observer():
    seen = []
    result = Engine(Chain(fn('a', spend), fn('b', spend)).build(DEFAULT), DEFAULT, observe=seen.append).run({'in': 30})
    assert result.used == {'tokens': 60, 'calls': 2}
    used = [e for e in seen if e['event'] == 'used']
    assert [(e['node'], e['amounts'], e['total']) for e in used] == [
        ('a', {'tokens': 30, 'calls': 1}, {'tokens': 30, 'calls': 1}), ('b', {'tokens': 30, 'calls': 1}, {'tokens': 60, 'calls': 2})]
    with pytest.raises(RunError, match='tokens must be a number that is not negative'):
        Engine(Chain(fn('a', spend)).build(DEFAULT), DEFAULT).run({'in': -1})
    with pytest.raises(ValueError, match='budget: tokens'):
        Engine(Chain(fn('a', spend)).build(DEFAULT), DEFAULT, budget={'tokens': 'many'})


def test_a_run_over_its_budget_fails_even_when_the_node_carries_on():
    graph = lambda *nodes: Chain(*nodes).build(DEFAULT)    # noqa: E731
    with pytest.raises(RunError, match='the run has used 60 tokens, and its budget is 50') as failed:
        Engine(graph(fn('a', spend), fn('b', spend), fn('c', spend)), DEFAULT, budget={'tokens': 50}).run({'in': 30})
    assert failed.value.node == 'b' and failed.value.used == {'tokens': 60, 'calls': 2}
    assert Engine(graph(fn('a', spend), fn('b', spend)), DEFAULT, budget={'tokens': 60, 'other': 1}).run({'in': 30}).used['tokens'] == 60
    WENT_ON.clear()
    with pytest.raises(RunError, match='its budget is 5'):
        Engine(graph(fn('a', stubborn), fn('b', stubborn)), DEFAULT, budget={'tokens': 5}).run({'in': 30})
    assert WENT_ON == [30]                                  # b never ran


def test_an_agents_tokens_count_and_a_budget_stops_it():
    model = FakeProvider(lambda system, messages, tools: ChatResult(text='ok', input_tokens=100, output_tokens=20))
    graph = lambda: Chain(AgentNode('a', model), AgentNode('b', model)).build(DEFAULT)   # noqa: E731
    assert Engine(graph(), DEFAULT).run({'in': 'go'}).used == {'calls': 2, 'input_tokens': 200, 'output_tokens': 40, 'tokens': 240}
    with pytest.raises(RunError, match='the run has used 240 tokens, and its budget is 200'):
        Engine(graph(), DEFAULT, budget={'tokens': 200}).run({'in': 'go'})


def test_what_is_used_inside_a_block_or_a_grown_graph_counts_outside():
    inner = Chain(fn('a', spend), fn('b', spend)).build(DEFAULT)
    outer = lambda: Chain(fn('first', spend), GraphNode('block', inner, DEFAULT)).build(DEFAULT)    # noqa: E731
    assert Engine(outer(), DEFAULT).run({'in': 10}).used == {'tokens': 30, 'calls': 3}
    with pytest.raises(RunError, match='the run has used 30 tokens, and its budget is 25'):
        Engine(outer(), DEFAULT, budget={'tokens': 25}).run({'in': 10})
    request = FunctionNode('plan', lambda x: {'graph': {'method': 'chain', 'nodes': [node('n1', 'spend'), node('n2', 'spend')]},
                                              'inputs': {'in': x}}, inputs=[Port('x')], outputs=[Port('y')])
    grown = lambda: Chain(request, SpawnNode('grow', Catalog(allow=[HERE]))).build(DEFAULT)    # noqa: E731
    assert Engine(grown(), DEFAULT).run({'in': 10}).used == {'tokens': 20, 'calls': 2}
    with pytest.raises(RunError, match='its budget is 15'):       # not turned into an answer the sender may retry on
        Engine(grown(), DEFAULT, budget={'tokens': 15}).run({'in': 10})


def test_usage_is_kept_in_the_journal_and_a_larger_budget_lets_the_run_go_on(tmp_path):
    graph = lambda: Chain(fn('a', spend), fn('b', spend), fn('c', spend)).build(DEFAULT)    # noqa: E731
    journal = lambda: Journal(tmp_path / 'j')    # noqa: E731
    with pytest.raises(RunError, match='its budget is 50'):
        Engine(graph(), DEFAULT, journal=journal(), budget={'tokens': 50}).run({'in': 30})
    with pytest.raises(RunError, match='it goes on only under a larger budget'):
        Engine(graph(), DEFAULT, journal=journal(), budget={'tokens': 50}).resume()
    result = Engine(graph(), DEFAULT, journal=journal(), budget={'tokens': 200}).resume()
    assert result.last('out') == 30 and result.used == {'tokens': 120, 'calls': 4}      # b was paid for twice, and it shows
    assert Engine(graph(), DEFAULT, journal=journal()).resume().used == {'tokens': 120, 'calls': 4}


def test_an_activation_has_a_key_that_is_the_same_when_it_runs_again(tmp_path):
    graph = lambda: Chain(fn('a', keyed), fn('b', keyed)).build(DEFAULT)    # noqa: E731
    KEYS.clear()
    BROKEN.clear()
    Engine(graph(), DEFAULT).run({'in': 1})
    Engine(graph(), DEFAULT).run({'in': 1})
    assert len(set(KEYS)) == 4 and KEYS[0].endswith('/a#1') and KEYS[1].endswith('/b#1')     # two runs, two names

    KEYS.clear()

    def break_b(event):
        if event['event'] == 'finished' and event['node'] == 'a':
            BROKEN.append(True)
    with pytest.raises(RunError):
        Engine(graph(), DEFAULT, journal=Journal(tmp_path / 'j'), observe=break_b).run({'in': 1})
    BROKEN.clear()
    Engine(graph(), DEFAULT, journal=Journal(tmp_path / 'j')).resume()
    assert len(KEYS) == 3 and KEYS[1] == KEYS[2] and KEYS[0] != KEYS[1]                     # b ran twice under one key

    KEYS.clear()
    inner = Chain(fn('a', keyed)).build(DEFAULT)
    Engine(Chain(fn('first', keyed), GraphNode('block', inner, DEFAULT)).build(DEFAULT), DEFAULT).run({'in': 1})
    assert KEYS[1] == KEYS[0].replace('/first#1', '/block#1/a#1')


def call(*argv):
    out = io.StringIO()
    code = cli.main([str(a) for a in argv], out)
    return code, json.loads(out.getvalue())


def test_the_command_line_takes_a_budget_and_resume_may_raise_it(tmp_path):
    graph = tmp_path / 'g.json'
    graph.write_text(json.dumps({'method': 'chain', 'nodes': [node('a', 'spend'), node('b', 'spend')]}), encoding='utf-8')
    code, said = call('run', graph, '--dir', tmp_path / 'run', '--input', 'in=30', '--allow', HERE, '--budget', 'tokens=50')
    assert code == 1 and said['status'] == 'failed' and 'its budget is 50' in said['error'] and said['used'] == {'tokens': 60, 'calls': 2}
    code, said = call('resume', '--dir', tmp_path / 'run')
    assert code == 1 and 'only under a larger budget' in said['error']
    code, said = call('resume', '--dir', tmp_path / 'run', '--budget', 'tokens=500')
    assert code == 0 and said['outputs'] == {'out': [30]} and said['used'] == {'tokens': 90, 'calls': 3}
    with pytest.raises(SystemExit):
        cli.main(['run', str(graph), '--dir', str(tmp_path / 'r2'), '--budget', 'tokens'])
    with pytest.raises(SpecError, match='budget: tokens'):
        Run.create(tmp_path / 'r3', json.loads(graph.read_text()), allow=[HERE], budget={'tokens': -1})


def test_one_process_at_a_time_runs_a_run_directory(tmp_path):
    description = {'method': 'chain', 'nodes': [node('a', 'spend')]}
    run = Run.create(tmp_path / 'run', description, allow=[HERE])
    lock = tmp_path / 'run' / 'lock'
    holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        lock.write_text(json.dumps({'pid': holder.pid, 'host': __import__('socket').gethostname(), 'since': 'earlier'}))
        with pytest.raises(SpecError, match=f'is being run by process {holder.pid} .* since earlier; if that process is gone, delete'):
            run.start({'in': 1})
        assert run.status()['status'] == 'new' and lock.exists()            # looking is always allowed
    finally:
        holder.kill()
        holder.wait()
    if os.name != 'posix':
        lock.unlink()                           # where a dead holder cannot be told from a live one, a person removes it
    assert run.start({'in': 1})['status'] == 'done' and not lock.exists()          # the holder died: the lock is taken over
    lock.write_text(json.dumps({'pid': 1, 'host': 'another-machine', 'since': 'earlier'}))
    with pytest.raises(SpecError, match='on another-machine'):
        Run(tmp_path / 'run').resume()                                             # cannot tell from here: left alone


def held_inside(ctx, x):
    with pytest.raises(SpecError, match='is being run by process'):
        Run(HELD[0]).resume()
    return x


HELD = []


def test_a_run_cannot_be_resumed_while_it_is_running_and_the_lock_goes_when_it_fails(tmp_path):
    HELD[:] = [tmp_path / 'run']
    run = Run.create(tmp_path / 'run', {'method': 'chain', 'nodes': [node('a', 'held_inside'), node('b', 'keyed')]}, allow=[HERE])
    BROKEN[:] = [True]
    assert run.start({'in': 1})['status'] == 'failed' and not (tmp_path / 'run' / 'lock').exists()
    BROKEN.clear()
    assert Run(tmp_path / 'run').resume()['status'] == 'done'
