"""Robustness of the kernel and its stdio server. SIMULATION: FakeProvider only, no model is called.

Hostile or broken input must never end the connection; a damaged file stops only what depends on it, with a
plain reason; concurrent and repeated requests have one effect; a killed engine resumes from disk.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from mch_harness.graph.model import GraphError, fingerprint
from mch_harness.mcp import control
from mch_harness.mcp.daemon import Engine

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kernel_mcp import BASE, Client  # noqa: E402

INIT = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
         'clientInfo': {'name': 'robustness', 'version': '1'}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'}]


def call(name, args, i=9):
    return {'jsonrpc': '2.0', 'id': i, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}


def serve_once(tmp_path, lines):
    data = b''.join((m if isinstance(m, bytes) else json.dumps(m).encode()) + b'\n' for m in INIT + lines)
    data += b'{"jsonrpc":"2.0","id":"last","method":"ping"}\n'
    out = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'mch_harness.mcp.cli', '--workspace', str(tmp_path / 'w'),
                          '--home', str(tmp_path / 'h'), 'serve', '--demo'], input=data, capture_output=True, timeout=60,
                         env={**os.environ, 'PYTHONPATH': str(BASE)})
    assert out.returncode == 0, out.stderr.decode('utf-8', 'replace')[-2000:]
    replies = [json.loads(line) for line in out.stdout.decode('utf-8').splitlines()]
    assert replies[-1] == {'jsonrpc': '2.0', 'id': 'last', 'result': {}}, 'the connection must survive'
    return replies[1:-1]


@pytest.mark.parametrize('line,code', [
    (b'[' * 200_000, -32700),                       # nesting deeper than the parser's recursion limit
    (b'\xff\xfe not utf-8', -32700),
    (b'{"jsonrpc":"2.0","id":5,"method":"ping"}' + b' ' * (5 * 1024 * 1024), -32600),   # over the line limit
    (b'{"jsonrpc":"2.0","id":5,"method":"ping","params":{"x":NaN}}', -32700),
    (b'"a string"', -32600),
    (b'[]', -32600),
    (b'{"jsonrpc":"2.0","id":true,"method":"ping"}', -32600),
])
def test_malformed_lines_get_an_error_and_the_connection_stays(tmp_path, line, code):
    replies = serve_once(tmp_path, [line])
    assert [r['error']['code'] for r in replies] == [code]


@pytest.mark.parametrize('args', [
    {'request_id': 'a', 'name': '\ud800', 'task': 't'},                       # a lone surrogate cannot be stored
    {'request_id': 'a', 'name': 'n' * 201, 'task': 't'},
    {'request_id': 'a', 'name': 'n', 'task': 't' * 50_001},
    {'request_id': 'x' * 257, 'name': 'n', 'task': 't'},
])
def test_arguments_outside_the_advertised_limits_are_refused_before_any_effect(tmp_path, args):
    replies = serve_once(tmp_path, [call('project.create', args), call('project.list', {}, 10)])
    assert replies[0]['error']['code'] == -32602
    assert replies[1]['result']['structuredContent']['projects'] == []


def test_oversized_or_non_finite_proposals_are_refused(tmp_path):
    big = {'x': 'y' * (300 * 1024)}
    replies = serve_once(tmp_path, [call('operation.request', {'project': 'p', 'request_id': 'r', 'instruction': 'i', 'proposal': big}),
                                    b'{"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"operation.request","arguments":'
                                    b'{"project":"p","request_id":"r","instruction":"i","proposal":{"x":1e999}}}}'])
    assert [r['error']['code'] for r in replies] == [-32602, -32602]


def make_project(engine, request_id='c'):
    return engine.call('project.create', {'request_id': request_id, 'name': 'n', 'task': 't'})['project']['id']


def test_damaged_files_stop_only_what_depends_on_them(tmp_path):
    with Engine(tmp_path / 'w', tmp_path / 'h', demo=True) as engine:
        broken, healthy, mode = make_project(engine, 'a'), make_project(engine, 'b'), make_project(engine, 'm')
    (tmp_path / 'w' / broken / 'state/graph.json').write_text('{"plan":', encoding='utf-8')
    (tmp_path / 'w' / mode / 'state/harness-mode.json').write_text('{', encoding='utf-8')
    queued = control.enqueue(tmp_path / 'w', 'confirm', project=healthy, hash='x')
    (tmp_path / 'w' / '.harness-control' / (queued['id'] + '.json')).write_text('{', encoding='utf-8')
    for path in (tmp_path / 'w' / '.harness-requests').glob('*.json'):
        path.write_text('{', encoding='utf-8')
    with Engine(tmp_path / 'w', tmp_path / 'h', demo=True) as engine:   # one bad file does not stop the engine
        assert engine.call('graph.view', {'project': healthy})['status'] == 'draft'
        with pytest.raises(GraphError, match='graph.json 损坏'):
            engine.call('graph.view', {'project': broken})
        with pytest.raises(GraphError, match='harness-mode.json 损坏'):
            engine.call('graph.view', {'project': mode})
        with pytest.raises(GraphError, match='记录损坏'):            # never replays a request it cannot account for
            engine.call('project.create', {'request_id': 'b', 'name': 'n', 'task': 't'})
        engine.process_commands()
        record = control.result(tmp_path / 'w', queued['id'])
        assert record['status'] == 'failed' and 'corrupt' in record['error']
        assert (tmp_path / 'w' / '.harness-control' / (queued['id'] + '.json.corrupt')).exists()
        listed = {p['id']: p for p in engine.projects()}
        assert set(listed) == {broken, healthy, mode} and 'harness-mode.json' in listed[mode]['error']


def test_concurrent_identical_requests_have_one_effect(tmp_path):
    with Engine(tmp_path / 'w', tmp_path / 'h', demo=True) as engine:
        args = {'request_id': 'same', 'name': 'n', 'task': 't'}
        with ThreadPoolExecutor(12) as pool:
            results = list(pool.map(lambda _: _safe(engine.call, 'project.create', dict(args)), range(24)))
        ids = {r['project']['id'] for r in results if isinstance(r, dict)}
        assert len(ids) == 1 and len(engine.projects()) == 1
        pid = ids.pop()
        doc = engine.manager.graph(pid).store.load()
        confirm = {'schema': 1, 'id': 'c', 'command': 'confirm', 'args': {'project': pid, 'hash': fingerprint(doc['plan'])}}
        with ThreadPoolExecutor(8) as pool:
            outcomes = list(pool.map(lambda _: _safe(engine._operator, dict(confirm)), range(8)))
        assert sum(1 for o in outcomes if not isinstance(o, Exception)) == 1
        assert engine.graph_view(pid)['version'] == 1


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        return exc


def test_a_killed_engine_resumes_from_disk(tmp_path):
    client = Client(tmp_path)
    client.initialize()
    pid = client.tool('project.create', request_id='k', name='强杀恢复', task='核对资料')['project']['id']
    plan_hash = client.tool('graph.view', project=pid)['plan_hash']
    queued = client.operator('confirm', pid, plan_hash)
    assert client.wait(lambda: client.operator('result', queued['id'])['status'] == 'done')
    assert client.wait(lambda: client.tool('graph.view', project=pid)['run']['submissions'].get('worker'))
    client.process.send_signal(signal.SIGKILL)
    client.process.wait(timeout=10)
    again = Client(tmp_path)               # the workspace lease of the killed process does not block
    again.initialize()
    assert again.tool('project.create', request_id='k', name='强杀恢复', task='核对资料')['project']['id'] == pid
    assert again.wait(lambda: again.tool('graph.view', project=pid)['run']['submissions'].get('inspector'))
    again.tool('operation.request', project=pid, request_id='rel', instruction='DEMO_REVIEW_AND_RELEASE work')
    assert again.wait(lambda: again.tool('graph.view', project=pid)['run']['submissions'].get('delivery'))
    view = again.tool('graph.view', project=pid)
    assert view['run']['counters'].get('worker') == 1          # nothing started twice
    again.process.stdin.close()
    assert again.process.wait(timeout=15) == 0
