"""Explicit block coordination, without release or additional file authority."""
import copy

from ..core.jsonfile import locked
from .model import fingerprint, require
from .run import GraphRun
from ..plan import LEAD, HUMAN


class Coordination:
    def __init__(self,store):
        self.store=store
        self.run=GraphRun(store)

    def _scope(self,data,actor,block):
        definition=next((b for b in data['plan']['blocks'] if b['id']==block),None)
        require(definition is not None,'未知任务块')
        if actor in (HUMAN,LEAD):
            return None
        require(definition.get('coordinator')==actor,'没有此任务块的协调权限')
        instance=self.run._current(data,actor)
        require(instance['status'] in ('active','submitted') and instance['plan_version']==data['version'],
                '协调实例已结束或过期')
        return instance

    def _status(self,data,block):
        run=data['run']; nodes=[]
        for node in data['plan']['nodes']:
            if node.get('block')!=block:
                continue
            instance=run['instances'].get(run['by_node'].get(node['id']),{})
            sub=run['submissions'].get(node['id'],{})
            held=node['id'] in run['input_holds']
            nodes.append({'node':node['id'],'name':node['name'],'instance':instance.get('id'),
                'status':instance.get('status','not_started'),'submission_status':sub.get('status'),
                'input_held':held,'submitted':sub.get('status')=='submitted' and not held})
        return {'block':block,'round':run['blocks'][block]['round'],'plan_version':data['version'],
            'released':bool(run['blocks'][block]['released']),'nodes':nodes,
            'all_submitted':bool(nodes) and all(n['submitted'] for n in nodes)}

    def status(self,actor,block):
        with locked(self.store.path):
            data=self.run._load()
            self._scope(data,actor,block)
            self.run._audit(data)
            # Names/status only: no other node's task files, summaries or state.
            return self._status(data,block)

    def request_review(self,actor,block,number,summary):
        require(type(number) is int and isinstance(summary,str) and summary.strip(),'需要块轮次及验收申请说明')
        with locked(self.store.path):
            data=self.run._load()
            instance=self._scope(data,actor,block)
            require(instance is not None,'验收申请由指定的次主控提出；总主控可直接独立审查')
            self.run._audit(data)
            status=self._status(data,block)
            require(status['round']==number and not status['released'],'块已放行或申请轮次过期')
            basis=fingerprint({n['node']:data['run']['submissions'].get(n['node'],{}).get('id') for n in status['nodes']})
            record={'block':block,'round':number,'plan_version':data['version'],'node':actor,'instance':instance['id'],
                'summary':summary,'all_submitted':status['all_submitted'],'basis':basis,'status':'requested'}
            key='block-review-'+fingerprint(record)[:24]
            requests=data['run'].setdefault('block_reviews',{})
            if key in requests:
                return copy.deepcopy(requests[key])
            record['id']=key
            requests[key]=record
            self.store._save(data,'graph.block_review_requested',actor,record)
            return copy.deepcopy(record)
