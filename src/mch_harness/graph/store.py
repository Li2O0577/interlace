"""Atomic plan/approval state. The caller supplies authenticated actor identity."""
from __future__ import annotations

import copy
import json
import secrets
from pathlib import Path

from ..core.jsonfile import locked, write
from ..core.events import EventLog
from .model import affected, fingerprint, require, validate
from ..plan import LEAD, HUMAN


class GraphStore:
    def __init__(self, root, events=None):
        self.root = Path(root)
        self.path = self.root/'state'/'graph.json'
        self.events=events

    def load(self):
        with locked(self.path):
            if not self.path.exists():
                return {'schema':1,'version':0,'status':'draft','plan':None,'confirmed_hash':None,'changes':{},'run':{}}
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data['plan'] is not None:
                validate(data['plan'])
            if data['status'] == 'confirmed':
                require(fingerprint(data['plan']) == data['confirmed_hash'], '已确认计划摘要不匹配，停止执行')
            return data

    def _save(self, data, event, actor, detail):
        write(self.path, data)
        events=self.events or EventLog(self.root/'state'/'events.jsonl')
        events.append(event, detail, source=actor)

    def draft(self, actor, plan):
        require(actor in (HUMAN,LEAD), '只有操作者或主控能拟定草稿')
        plan = validate(plan)
        with locked(self.path):
            data = self.load()
            require(data['status'] == 'draft', '已确认计划必须走改图审批')
            data['plan'] = plan
            self._save(data,'graph.drafted',actor,{'hash':fingerprint(plan)})
        return copy.deepcopy(data)

    def confirm(self, actor, plan_hash):
        require(actor == HUMAN, '只有操作者能确认计划')
        with locked(self.path):
            data = self.load()
            require(data['status'] == 'draft' and data['plan'] is not None, '没有待确认草稿')
            require(fingerprint(data['plan']) == plan_hash, '草稿已变化，请核对摘要')
            data.update(status='confirmed',version=1,confirmed_hash=plan_hash)
            self._save(data,'graph.confirmed',actor,{'version':1,'hash':plan_hash})
        return copy.deepcopy(data)

    def propose(self, actor, plan, *, reason, handling):
        require(actor == LEAD, '只有主控能提出改图申请')
        plan = validate(plan)
        require(isinstance(reason,str) and reason.strip() and handling == 'invalidate', '需要原因和明确成果处理；初版只支持 invalidate')
        with locked(self.path):
            data = self.load()
            require(data['status'] == 'confirmed', '先确认初始计划')
            require(fingerprint(plan) != data['confirmed_hash'], '没有实际修改')
            req = {'id':'change-'+secrets.token_hex(6),'base_version':data['version'],'old_hash':data['confirmed_hash'],
                   'new_hash':fingerprint(plan),'before':copy.deepcopy(data['plan']),'after':plan,'reason':reason,
                   'affected':affected(data['plan'],plan),'handling':handling,'approved':False,'applied':False}
            data['changes'][req['id']] = req
            self._save(data,'graph.change_requested',actor,req)
        return copy.deepcopy(req)

    def approve(self, actor, request_id, plan_hash):
        require(actor == HUMAN, '只有操作者能批准改图')
        with locked(self.path):
            data = self.load()
            req = data['changes'].get(request_id)
            require(req is not None, '未知申请')
            require(req['base_version'] == data['version'] and req['old_hash'] == data['confirmed_hash'], '申请已过期')
            require(req['new_hash'] == plan_hash and not req['applied'], '批准摘要不匹配或已生效')
            req['approved'] = True
            self._save(data,'graph.change_approved',actor,{'request':request_id,'hash':plan_hash})

    def apply(self, actor, request_id):
        require(actor == LEAD, '只有主控能应用获批修改')
        with locked(self.path):
            data = self.load()
            req = data['changes'].get(request_id)
            require(req is not None and req['approved'], '没有操作者批准')
            require(not req['applied'] and req['base_version'] == data['version'] and req['old_hash'] == data['confirmed_hash'], '申请已使用或过期')
            require(fingerprint(req['after']) == req['new_hash'], '已批准的变更内容摘要不匹配')
            if data['run']:
                from .run import invalidate_for_change
                invalidate_for_change(data,req)
            data.update(plan=copy.deepcopy(req['after']),version=data['version']+1,confirmed_hash=req['new_hash'])
            req['applied'] = True
            self._save(data,'graph.changed',actor,{'request':request_id,'version':data['version'],'affected':req['affected']})
        return copy.deepcopy(data)
