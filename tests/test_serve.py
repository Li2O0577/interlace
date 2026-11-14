"""The MCP server: the command line's operations, offered to a client that the server does not trust."""
import io
import json
import subprocess
import sys

from interlacing import cli
from interlacing.serve import TOOLS, Server

HERE = __name__
CALLS = []


def survey(x):
    CALLS.append('survey')
    print('a node may print')                                       # must not reach the client
    return f'surveyed {x}'


def broken(x):
    raise OSError('the instrument is off')


def node(id, function):
    return {'id': id, 'kind': 'function', 'function': f'{HERE}.{function}', 'inputs': ['x'], 'outputs': ['y']}


REVIEWED = {'nodes': [node('survey', 'survey'), {'id': 'editor', 'kind': 'human'}],
            'edges': [{'from': 'survey.y', 'to': 'editor.request'}],
            'inputs': {'site': 'survey.x'}, 'outputs': {'verdict': 'editor.answer'}}


def tool(server, name, **arguments):
    reply = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
    result = reply['result']
    assert json.loads(result['content'][0]['text']) == result['structuredContent']
    return result['isError'], result['structuredContent']


def test_the_handshake_and_the_tool_list(tmp_path):
    server = Server(tmp_path)
    hello = server.handle({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26', 'capabilities': {}}})
    assert hello['result']['protocolVersion'] == '2025-03-26' and hello['result']['capabilities'] == {'tools': {}}
    assert hello['result']['serverInfo']['name'] == 'interlacing'
    assert server.handle({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {'protocolVersion': '1999-01-01'}})['result']['protocolVersion'] == '2025-06-18'
    assert server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) is None
    assert server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'})['result'] == {}
    listed = server.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'})['result']['tools']
    assert [t['name'] for t in listed] == ['check', 'start', 'status', 'resume', 'answer', 'runs'] and listed == TOOLS
    assert all(t['inputSchema']['type'] == 'object' and t['description'] for t in listed)
    assert server.handle({'jsonrpc': '2.0', 'id': 4, 'method': 'resources/list'})['error']['code'] == -32601
    assert server.handle({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'erase'}})['error']['code'] == -32602
    assert server.handle([1, 2])['error']['code'] == -32600


def test_a_client_runs_a_graph_and_a_person_answers(tmp_path):
    server = Server(tmp_path, allow=[HERE])
    CALLS.clear()
    failed, said = tool(server, 'check', graph=REVIEWED)
    assert not failed and said['nodes'] == {'survey': 'function', 'editor': 'human'} and CALLS == []
    failed, said = tool(server, 'start', graph=REVIEWED, inputs={'site': 'the bridge'})
    run = said['run']
    assert not failed and said['status'] == 'waiting' and said['questions'][0]['request'] == 'surveyed the bridge'
    assert tool(server, 'status', run=run)[1]['status'] == 'waiting'
    assert tool(server, 'runs')[1]['runs'][0]['run'] == run
    failed, said = tool(server, 'answer', run=run, answer={'approved': True})
    assert not failed and said['questions'] == []
    failed, said = tool(server, 'resume', run=run)
    assert not failed and said['status'] == 'done' and said['outputs'] == {'verdict': [{'approved': True}]} and CALLS == ['survey']
    assert (tmp_path / run / 'journal.jsonl').exists()             # the same directory the command line works on
    out = io.StringIO()
    assert cli.main(['status', '--dir', str(tmp_path / run)], out) == 0 and json.loads(out.getvalue())['status'] == 'done'


def test_the_client_cannot_reach_past_what_the_server_was_given(tmp_path):
    server = Server(tmp_path / 'runs')
    failed, said = tool(server, 'start', graph=REVIEWED, inputs={'site': 'x'})
    assert failed and said['status'] == 'refused' and 'not in a module this catalog allows' in said['error']
    failed, said = tool(server, 'start', graph={'nodes': [{'id': 'p', 'kind': 'process', 'command': ['ls']}]})
    assert failed and 'does not allow processes' in said['error']
    assert tool(server, 'start', graph=REVIEWED, allow=[HERE], processes=True)[0]       # asking for more changes nothing
    assert tool(server, 'runs')[1] == {'runs': []} and not list((tmp_path / 'runs').glob('*/graph.json'))
    for name in ('../outside', '/etc', '', 'nope', 7):
        failed, said = tool(server, 'status', run=name)
        assert failed and said['error'] == f'no run {name!r}'
    assert tool(server, 'check', graph='not a graph')[1]['error'].startswith('graph must be an object')
    assert tool(server, 'start', graph=REVIEWED, inputs=[1])[1]['error'].startswith('inputs must be an object')


def test_a_failing_node_is_reported_and_the_server_goes_on(tmp_path):
    server = Server(tmp_path, allow=[HERE])
    failed, said = tool(server, 'start', graph={'method': 'chain', 'nodes': [node('a', 'broken')]}, inputs={'in': 1})
    assert not failed and said['status'] == 'failed' and said['node'] == 'a' and 'the instrument is off' in said['error']
    assert tool(server, 'answer', run=said['run'], answer=1)[1]['error'] == 'no question is open'
    assert tool(server, 'runs')[1]['runs'][0]['status'] == 'failed'


def test_the_server_speaks_over_stdio_and_only_the_protocol_reaches_the_client(tmp_path):
    messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'start', 'arguments': {'graph': REVIEWED, 'inputs': {'site': '桥'}}}}]
    sent = '\n'.join(json.dumps(m) for m in messages) + '\nthis is not json\n\n'
    done = subprocess.run([sys.executable, '-m', 'interlacing.cli', 'serve', '--runs', str(tmp_path), '--allow', HERE],
                          input=sent, capture_output=True, text=True, encoding='utf-8', cwd=str(tmp_path),
                          env={**__import__('os').environ, 'PYTHONPATH': str(__import__('pathlib').Path(__file__).parent)})
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert done.returncode == 0 and [r.get('id') for r in replies] == [1, 2, None]
    assert replies[1]['result']['structuredContent']['questions'][0]['request'] == 'surveyed 桥'
    assert replies[2]['error']['code'] == -32700 and 'a node may print' in done.stderr
