"""Real engine threads + FakeProvider; no FastAPI, workbench preset or remote model."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT=r'''
import importlib.abc, sys, time, re
from pathlib import Path
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if any(fullname == n or fullname.startswith(n+'.') for n in (
            'mch','fastapi','uvicorn','pypdf','openpyxl','psutil')):
            raise RuntimeError('unexpected domain import: '+fullname)
sys.meta_path.insert(0,Block())
from indeterminate.agents.runtime import AgentManager
from indeterminate.agents.providers import FakeProvider, ChatResult
from indeterminate.core.projects import ProjectStore
from indeterminate.core.settings_store import SettingsStore
from indeterminate.core.events import EventLog
from indeterminate.core.locks import ProjectLocks
from indeterminate.tools import registry
from indeterminate.tools.registry import ToolContext
from indeterminate.graph.model import fingerprint
from indeterminate.graph.recovery import STATE_TEMPLATE
base,task=Path(sys.argv[1]),sys.argv[2]
store=ProjectStore(base/'workspace')
project=store.create(task,mode='graph',description=task)
pid=project['id']; root=store.path(pid)
(root/'work/source.txt').write_text('操作者材料：必须保留来源与已知限制',encoding='utf-8')
settings=SettingsStore(base/'settings')
settings._save({'defaults':{'agent_model':'fake:m'}})
events=EventLog(store.events_path(pid)); locks=ProjectLocks()
def run_tool(pid,name,params,actor='human'):
    with locks[pid]:
        return registry.call(ToolContext(pid,root,store.repo(pid),events,settings,actor,lock=locks[pid],agents=manager),name,params)
manager=AgentManager(store,settings,lambda pid:events,run_tool,locks)
def policy(system,messages,tools):
    assert '数学建模竞赛' not in system and '先证明再' not in system
    match=re.search(r'你是节点 ([a-z_-]+)，实例 ([a-z_-]+):r(\d+)',system)
    if not match:
        return ChatResult('等待验收',[])
    node,_,number=match.groups()
    if node=='worker':
        assert 'work/source.txt' in system
    done={c['name'] for m in messages for c in m.get('tool_calls',[])}
    ref=f'nodes/{node}/r{number}/work/result.md'
    inputs=manager.graph(pid).run.state()['submissions']
    source='work/source.txt' if node=='worker' else inputs['worker' if node=='inspector' else 'inspector']['submit']
    for name,args in [('fs.read',{'path':source}),('fs.write',{'path':ref,'content':task+'：已核查材料'}),
                      ('graph.save_state',{'content':STATE_TEMPLATE}),('graph.submit',{'summary':'文件与来源可核查','files':[ref]})]:
        if name not in done:
            return ChatResult('',[{'id':name,'name':name,'args':args}])
    return ChatResult('等待审查',[])
manager.provider_factory=lambda model:FakeProvider(policy)
runtime=manager.graph(pid)
document=runtime.store.load(); plan=document['plan']
plan['nodes'][1].update(role='worker',reads=['work/source.txt'])
plan['nodes'][2]['role']='reviewer'
runtime.store.draft('human',plan)
runtime.store.confirm('human',fingerprint(plan))
runtime.tick()
def wait(node):
    deadline=time.time()+15
    while time.time()<deadline:
        if runtime.run.state()['submissions'].get(node,{}).get('status')=='submitted':
            return
        time.sleep(.03)
    raise AssertionError(str(manager.list(pid)))
wait('inspector')
assert 'delivery' not in runtime.run.state()['by_node']
manager.run_tool(pid,'graph.release',{'block':'work','round':1,'note':'内部主控检查后放行'},'lead')
wait('delivery')
assert not [m for m in sys.modules if m == 'mch' or m.startswith('mch.')]
assert all(not a.rec.get('error') for a in manager._project_states(pid))
manager.shutdown()
print('fake-provider standalone task completed: '+task)
'''


@pytest.mark.parametrize('task',['整理代码并核对说明','审查资料并标明信息来源'])
def test_generic_tasks_run_without_workbench_or_web(tmp_path,task):
    result=subprocess.run([sys.executable,'-X','utf8','-c',SCRIPT,str(tmp_path),task],capture_output=True,text=True,
        encoding='utf-8',env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')},timeout=40)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    assert 'fake-provider standalone task completed' in result.stdout
