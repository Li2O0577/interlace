import io
import json

import pytest

from indeterminate.mcp.server import Server, serve, PROTOCOL


class Engine:
    mode='simulation'
    def call(self,name,args):
        return {'name':name,'args':args}


def request(method,params=None,**extra):
    return {'jsonrpc':'2.0','id':1,'method':method,'params':params or {},**extra}


def initialize(server):
    reply=server.handle(request('initialize',{'protocolVersion':'unsupported-future-version',
        'capabilities':{},'clientInfo':{'name':'test','version':'0'}}))
    assert reply['result']['protocolVersion']==PROTOCOL
    assert not server.initialized
    assert server.handle({'jsonrpc':'2.0','method':'notifications/initialized'}) is None


@pytest.mark.parametrize('message',[[],None,12,{'method':'ping'},request('ping',id=True)])
def test_invalid_rpc_does_not_crash(message):
    assert Server(Engine()).handle(message)['error']['code']==-32600


def test_protocol_lifecycle_errors_and_unknown_tool():
    server=Server(Engine())
    assert server.handle(request('tools/list'))['error']['code']==-32002
    initialize(server)
    assert server.handle(request('initialize'))['error']['code']==-32600
    assert server.handle(request('tools/call',{'name':'graph.release'}))['error']['code']==-32602
    assert server.handle(request('ping'))['result']=={}
    assert server.handle({'jsonrpc':'2.0','method':'unknown-notification'}) is None


@pytest.mark.parametrize('arguments',[
    {'project':'p','after':True},{'project':'p','limit':0},{'project':'p','limit':501},
    {'project':'p','actor':'lead'},{'project':''},[],
])
def test_advertised_tool_schema_is_enforced(arguments):
    server=Server(Engine()); initialize(server)
    reply=server.handle(request('tools/call',{'name':'events.read','arguments':arguments}))
    assert reply['error']['code']==-32602


def test_stdio_handles_malformed_json_and_writes_only_json_lines():
    stdin=io.StringIO('{bad\n[]\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
    stdout=io.StringIO()
    serve(Engine(),stdin,stdout)
    replies=[json.loads(s) for s in stdout.getvalue().splitlines()]
    assert [r.get('error',{}).get('code') for r in replies]==[-32700,-32600,None]
