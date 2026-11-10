"""Lead-approved batches. Slots are durable idempotent spawn reservations."""
from __future__ import annotations

import copy
import secrets

from ..core.jsonfile import locked
from .model import require, validate_configuration
from .run import GraphRun


class Helpers:
    def __init__(self,store):
        self.store=store
        self.runtime=GraphRun(store)

    def _load(self):
        data=self.runtime._load()
        data['run'].setdefault('helper_requests',{})
        return data

    def request(self,actor,instance,count,assignments,purpose,*,additional_reason='',request_id=None):
        require(type(count) is int and count>0 and isinstance(assignments,list) and len(assignments)==count, '数量与完整分工必须一致')
        require(isinstance(purpose,str) and purpose.strip(), '说明整批用途')
        for assignment in assignments:
            require(isinstance(assignment,dict) and {'role','task'}<=assignment.keys()<= {'role','task','name','model','thinking'}, '分工接受角色、任务、名称及模型/思考强度')
            require(all(isinstance(assignment[k],str) and assignment[k].strip() for k in ('role','task')), '角色和任务必须写清楚')
            require('name' not in assignment or isinstance(assignment['name'],str) and assignment['name'].strip(),'名称无效')
            validate_configuration(assignment)
        with locked(self.store.path):
            data=self._load()
            inst=self.runtime._current(data,actor,instance)
            require(inst['status'] in ('active','submitted') and inst['plan_version']==data['version'], '申请实例已失效')
            node=self.runtime._node(data,actor)
            require(node.get('helpers',True), '本节点不允许申请图外 helper')
            key=request_id or 'helpers-'+secrets.token_hex(8)
            requests=data['run']['helper_requests']
            record={'id':key,'node':actor,'instance':instance,'plan_version':data['version'],'count':count,
                    'assignments':copy.deepcopy(assignments),'purpose':purpose,'additional_reason':additional_reason,
                    'status':'pending','slots':{}}
            if key in requests:
                existing=requests[key]
                require(all(existing.get(k)==v for k,v in record.items() if k not in ('status','slots')), '请求 ID 不可复用于不同批次')
                return copy.deepcopy(existing)
            if any(r['instance']==instance for r in requests.values()):
                require(isinstance(additional_reason,str) and additional_reason.strip(), '追加申请需说明新增原因；尽量一次完整规划')
            requests[key]=record
            self.store._save(data,'graph.helpers_requested',actor,record)
            return copy.deepcopy(record)

    def _request(self,data,key):
        request=data['run']['helper_requests'].get(key)
        require(request is not None and request['plan_version']==data['version'], '申请不存在或已过期')
        inst=self.runtime._current(data,request['node'],request['instance'])
        require(inst['status'] in ('active','submitted'), '申请实例已结束')
        return request

    def approve(self,actor,request_id):
        require(actor=='lead', '图外 helper 必须由内部主控批准')
        with locked(self.store.path):
            data=self._load()
            request=self._request(data,request_id)
            require(request['status'] in ('pending','approved'), '已拒绝的申请不能批准')
            request['status']='approved'
            self.store._save(data,'graph.helpers_approved',actor,{'request':request_id,'count':request['count']})

    def deny(self,actor,request_id,reason):
        require(actor=='lead' and isinstance(reason,str) and reason.strip(), '需要主控明确拒绝原因')
        with locked(self.store.path):
            data=self._load()
            request=self._request(data,request_id)
            require(request['status']=='pending', '只能拒绝尚未批准的批次')
            request.update(status='denied',reason=reason)
            self.store._save(data,'graph.helpers_denied',actor,{'request':request_id,'reason':reason})

    def consume(self,actor,request_id,index):
        with locked(self.store.path):
            data=self._load()
            request=self._request(data,request_id)
            require(actor in ('lead',request['node']) and request['status']=='approved', '批次未经主控批准或调用者无权执行')
            require(type(index) is int and 0<=index<request['count'], '超过批准数量')
            slot=str(index)
            if slot not in request['slots']:
                request['slots'][slot]={'key':f'helper:{request_id}:{index}','boundary_instance':request['instance'],
                    'assignment':copy.deepcopy(request['assignments'][index]),'status':'reserved','handle':None}
                self.store._save(data,'graph.helper_reserved',actor,{'request':request_id,'slot':index})
            return copy.deepcopy(request['slots'][slot])
