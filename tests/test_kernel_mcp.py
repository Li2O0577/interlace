"""External stdio protocol; actual engine, explicitly simulated model provider."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest

BASE=Path(__file__).resolve().parents[1]/'src'


class Client:
    def __init__(self,base):
        self.base=base
        self.process=subprocess.Popen([sys.executable,'-X','utf8','-m','mch_harness.mcp.cli','--workspace',str(base/'work'),
            '--home',str(base/'home'),'serve','--demo'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True,encoding='utf-8',env={**os.environ,'PYTHONPATH':str(BASE)})
        self.lines=queue.Queue()
        def read():
            for line in self.process.stdout:
                self.lines.put(line)
            self.lines.put(None)
        threading.Thread(target=read,daemon=True).start()
        self.seq=0

    def rpc(self,method,params=None):
        self.seq+=1
        self.process.stdin.write(json.dumps({'jsonrpc':'2.0','id':self.seq,'method':method,'params':params or {}})+'\n')
        self.process.stdin.flush()
        line=self.lines.get(timeout=20)
        assert line is not None,self.process.stderr.read()
        reply=json.loads(line)
        assert reply['id']==self.seq
        return reply

    def initialize(self):
        result=self.rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}})
        assert result['result']['protocolVersion']=='2025-11-25'
        self.process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.process.stdin.flush()

    def tool(self,tool_name,**args):
        reply=self.rpc('tools/call',{'name':tool_name,'arguments':args})
        assert 'error' not in reply,reply
        result=reply['result']
        assert not result.get('isError'),result
        return json.loads(result['content'][0]['text'])

    def operator(self,*args):
        result=subprocess.run([sys.executable,'-X','utf8','-m','mch_harness.mcp.cli','--workspace',str(self.base/'work'),
            '--home',str(self.base/'home'),*args],capture_output=True,text=True,encoding='utf-8',
            env={**os.environ,'PYTHONPATH':str(BASE)},timeout=15)
        assert result.returncode==0,result.stderr
        return json.loads(result.stdout)

    def wait(self,check):
        end=time.monotonic()+15
        while time.monotonic()<end:
            result=check()
            if result:
                return result
            time.sleep(.04)
        raise AssertionError('engine did not reach expected state')

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
                raise
        assert self.process.returncode==0,self.process.stderr.read()


def test_stdio_dedup_operator_gate_two_blocks_reconnect_and_artifacts(tmp_path):
    client=Client(tmp_path)
    try:
        assert client.rpc('server/discover')['error']['code']==-32601
        assert client.rpc('tools/list')['error']['code']==-32002
        client.initialize()
        names={t['name'] for t in client.rpc('tools/list')['result']['tools']}
        assert {'project.create','operation.request','graph.view','artifact.read'}<=names
        assert not {'graph.release','graph.confirm','approval.resolve','tools.execute'} & names
        args={'request_id':'create-1','name':'资料审查','task':'整理来源并记录限制'}
        project=client.tool('project.create',**args)
        assert project['runtime_mode']=='simulation'
        assert client.tool('project.create',**args)==project
        pid=project['project']['id']
        before=client.tool('graph.view',project=pid)
        assert before['status']=='draft' and before['run']=={}
        denied=client.rpc('tools/call',{'name':'graph.confirm','arguments':{'project':pid}})
        assert denied['error']['code']==-32602
        denied=client.rpc('tools/call',{'name':'operation.request','arguments':{
            'project':pid,'request_id':'bad','instruction':'批准','actor':'human'}})
        assert denied['error']['code']==-32602
        queued=client.operator('confirm',pid,before['plan_hash'])
        client.wait(lambda:client.operator('result',queued['id']).get('status')=='done')
        def inspection():
            doc=client.tool('graph.view',project=pid)
            return doc if doc['run'].get('submissions',{}).get('inspector',{}).get('status')=='submitted' else None
        doc=client.wait(inspection)
        assert not doc['run']['blocks']['work']['released']
        assert 'delivery' not in doc['run']['by_node']
        denied=client.rpc('tools/call',{'name':'artifact.read','arguments':{'project':pid,'path':'state/graph.json'}})
        assert denied['result']['isError']
        handles=dict(doc['run']['by_node'])
        request=client.tool('operation.request',project=pid,request_id='release-1',instruction='DEMO_REVIEW_AND_RELEASE work')
        assert request['delivered_to']=='lead'
        client.wait(lambda:client.tool('graph.view',project=pid)['run']['submissions'].get('delivery',{}).get('status')=='submitted')
        artifacts=client.tool('artifact.list',project=pid)
        assert artifacts['files']
        ref=next(f['path'] for f in artifacts['files'] if f['node']=='delivery' and '/work/' in f['path'])
        assert client.tool('artifact.read',project=pid,path=ref)['content'].startswith('SIMULATION')
    finally:
        client.close()
    client=Client(tmp_path)
    try:
        client.initialize()
        assert client.tool('project.create',**args)==project
        assert client.tool('operation.request',project=pid,request_id='release-1',instruction='DEMO_REVIEW_AND_RELEASE work')==request
        doc=client.tool('graph.view',project=pid)
        assert all(doc['run']['by_node'][n]==i for n,i in handles.items())
        events=client.tool('events.read',project=pid,after=0,limit=500)
        assert sum(e['type']=='graph.launched' and e['data']['node']=='worker' for e in events['events'])==1
        assert len(client.tool('project.list')['projects'])==1
        mismatch=client.rpc('tools/call',{'name':'project.create','arguments':{**args,'task':'不同任务'}})
        assert mismatch['result']['isError']
    finally:
        client.close()
