"""Project-level read-only keeper decisions; only the program writes grants."""
from __future__ import annotations

import copy
import secrets
from pathlib import PurePosixPath

from ..core.jsonfile import locked
from .boundary import file_hash, safe_path
from .model import require
from .run import GraphRun
from ..plan import LEAD, HUMAN


class Access:
    def __init__(self,store):
        self.store=store
        self.runtime=GraphRun(store)

    def _load(self):
        data=self.runtime._load()
        data['run'].setdefault('access',{'automatic':False,'generation':0,'keeper':None,'requests':{}})
        data['run'].setdefault('grants',{})
        return data

    def configure(self,automatic,*,task,authorization):
        require(type(automatic) is bool and isinstance(task,str) and task.strip() and isinstance(authorization,str) and authorization.strip(), '需要明确模式、任务和只读授权')
        with locked(self.store.path):
            data=self._load()
            access=data['run']['access']
            changed=access['automatic']!=automatic
            if changed:
                access['generation']+=1
            access.update(automatic=automatic,task=task,authorization=authorization)
            if not automatic:
                for request in access['requests'].values():
                    if request['status']=='pending':
                        request['status']='cancelled'
                for grant in data['run']['grants'].values():
                    grant['status']='revoked'
                if access['keeper']:
                    access['keeper']['status']='disabled'
            elif changed or access['keeper'] is None:
                access['keeper']={'key':f'file_keeper:g{access["generation"]}','handle':None,'status':'reserved'}
            self.store._save(data,'access.mode_changed','system',{'automatic':automatic,'generation':access['generation']})
            return copy.deepcopy(access['keeper'])

    def ensure_keeper(self,create):
        """Trusted runtime callback create(key, context), idempotent by key.

        A reserved record is not a created model. Only a successful callback
        returning its handle changes this record to active.
        """
        with locked(self.store.path):
            data=self._load()
            access=data['run']['access']
            if not access['automatic']:
                return None
            keeper=access['keeper']
            if keeper['status']=='active':
                return keeper['handle']
            require(keeper['status']=='reserved', '文件模型处于不可创建状态')
            context={'task':access['task'],'graph_path':str(self.store.path),'authorization':access['authorization']}
            key=keeper['key']
        # Never call manager/provider code while holding the graph file lock:
        # the scheduler takes its manager lock before reading this same file.
        handle=create(key,context)
        require(isinstance(handle,str) and handle.strip(), '创建接口必须返回真实运行时句柄')
        with locked(self.store.path):
            data=self._load()
            access=data['run']['access']
            keeper=access['keeper']
            if not access['automatic'] or not keeper or keeper['key']!=key:
                return None  # A concurrent mode change revoked this reservation.
            if keeper['status']=='active':
                require(keeper['handle']==handle,'创建接口必须按预留 key 幂等')
                return handle
            require(keeper['status']=='reserved','文件模型预留已失效')
            keeper.update(handle=handle,status='active')
            self.store._save(data,'access.keeper_created','system',{'key':keeper['key'],'handle':handle})
            return handle

    def request(self,actor,instance,need,purpose,*,request_id=None):
        require(isinstance(need,str) and need.strip() and isinstance(purpose,str) and purpose.strip(), '说明需要什么及其用途')
        with locked(self.store.path):
            data=self._load()
            access=data['run']['access']
            require(access['automatic'] and access['keeper'] and access['keeper']['status']=='active', '自动文件处理机制未启用或模型尚未创建')
            inst=self.runtime._current(data,actor,instance)
            require(inst['status'] in ('active','submitted') and inst['plan_version']==data['version'], '申请实例已失效')
            key=request_id or 'request-'+secrets.token_hex(8)
            require(isinstance(key,str) and key.strip(), '请求 ID 无效')
            request={'id':key,'node':actor,'instance':instance,'plan_version':data['version'],
                     'generation':access['generation'],'need':need,'purpose':purpose,'status':'pending'}
            existing=access['requests'].get(key)
            if existing:
                require(all(existing.get(k)==v for k,v in request.items() if k!='status'), '请求 ID 已用于其他申请')
                return copy.deepcopy(existing)
            access['requests'][key]=request
            self.store._save(data,'access.requested',actor,request)
            return copy.deepcopy(request)

    def decide(self,keeper_handle,request_id,decision,refs,reason):
        require(decision in ('grant','deny') and isinstance(refs,list) and isinstance(reason,str) and reason.strip(), '文件决策格式无效')
        require((decision=='grant' and bool(refs)) or (decision=='deny' and not refs), '批准需要具体文件，拒绝不能附带授权')
        with locked(self.store.path):
            data=self._load()
            access=data['run']['access']
            keeper=access['keeper']
            require(access['automatic'] and keeper and keeper['status']=='active' and keeper['handle']==keeper_handle, '只有当前文件模型可独立决策')
            request=access['requests'].get(request_id)
            require(request is not None and request['status']=='pending' and request['generation']==access['generation'] and request['plan_version']==data['version'], '申请已处理、关闭或过期')
            inst=self.runtime._current(data,request['node'],request['instance'])
            require(inst['status'] in ('active','submitted'), '申请实例已结束')
            hashes={}
            for ref in refs:
                require(isinstance(ref,str), '只允许具体文件地址')
                parts=PurePosixPath(ref).parts
                require(parts and parts[0] not in ('state','agents','.git') and 'state' not in parts, '不能转授内部控制或恢复材料')
                require(safe_path(self.store.root,ref).is_file(), '不能授权目录或不存在文件')
                hashes[ref]=file_hash(self.store.root,ref)
            grant_id=None
            if decision=='grant':
                grant_id='grant-'+secrets.token_hex(8)
                data['run']['grants'][grant_id]={'id':grant_id,'request':request_id,'instance':inst['id'],
                    'node':request['node'],'plan_version':data['version'],'generation':access['generation'],
                    'status':'active','hashes':hashes,'reason':reason}
            request.update(status='decided',decision=decision,refs=list(hashes),reason=reason,grant=grant_id,by=keeper_handle)
            self.store._save(data,'access.decided',keeper_handle,request)
            return copy.deepcopy(request)

    def revoke(self,actor,grant_id,reason):
        require(actor in (HUMAN,LEAD) and isinstance(reason,str) and reason.strip(), '仅操作者或主控可撤销授权')
        with locked(self.store.path):
            data=self._load()
            grant=data['run']['grants'].get(grant_id)
            require(grant is not None, '未知授权')
            grant.update(status='revoked',revoked_by=actor,revoke_reason=reason)
            self.store._save(data,'access.revoked',actor,{'grant':grant_id,'reason':reason})
