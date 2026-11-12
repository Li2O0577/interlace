"""Deterministic FakeProvider policy, never a real model or quality review."""
import re

from ..agents.providers import ChatResult, FakeProvider
from ..graph.recovery import STATE_TEMPLATE


def factory(engine):
    def policy(system,messages,tools):
        match=re.search(r'你是节点 ([a-z_-]+)，实例 ([a-z_-]+):r(\d+)',system)
        if not match:
            # Deliberately wait after inspection. A demo command asks the fake
            # INTERNAL lead to explicitly call the real release tool.
            for index in range(len(messages)-1,-1,-1):
                msg=messages[index]
                if msg.get('role')!='user':
                    continue
                match=re.search(r'DEMO_REVIEW_AND_RELEASE ([a-z_-]+)',msg.get('content',''))
                if match:
                    source=re.search(r'"project": "([a-z0-9-]+)"',msg.get('content',''))
                    pid=source[1] if source else None
                    if any(c['name']=='graph.release' for m in messages[index+1:] for c in m.get('tool_calls',[])):
                        break
                    if pid:
                        runtime=engine.manager.graph(pid)
                        block=match[1]
                        state=runtime.run.state()['blocks'].get(block)
                        if state and not state['released']:
                            return call('graph.release',{'block':block,'round':state['round'],
                                'note':'SIMULATION：确定性假主控显式放行，不代表模型审查质量'})
                    break
            return ChatResult('SIMULATION：等待主控验收；无真实模型调用。',[])
        node,_,number=match.groups()
        # A step counts as done only when its result came back without error: a call cut short by a restart
        # (or refused) is tried again, so a killed engine resumes the same steps instead of skipping one.
        ok={m.get('tool_call_id') for m in messages if m.get('role')=='tool' and not m.get('error')}
        done={c['name'] for m in messages for c in m.get('tool_calls',[]) if c.get('id') in ok}
        ref=f'nodes/{node}/r{number}/work/result.md'
        for name,args in [('fs.write',{'path':ref,'content':f'SIMULATION：{node} r{number} 演示交付；未调用模型。'}),
            ('graph.save_state',{'content':STATE_TEMPLATE}),
            ('graph.submit',{'summary':'SIMULATION：演示流程交付，不作质量保证','files':[ref]})]:
            if name not in done:
                return call(name,args)
        return ChatResult('SIMULATION：交付后等待审查。',[])
    return lambda model:FakeProvider(policy)


def call(name,args):
    return ChatResult('',[{'id':name,'name':name,'args':args}])
