"""Persisted graph lifecycle, without a second model execution loop.

File and state checks are enforced here; AgentManager execution is a later milestone.
Only a trusted runtime may reserve/attach; actors must come from its tools.
"""
from __future__ import annotations

import copy
from ..core.jsonfile import write

from ..core.jsonfile import locked
from .model import downstream, fingerprint, require
from .scheduler import ready
from .boundary import file_hash, safe_path, validate_submission_text
from .recovery import validate_state, work_hashes, check_state
from ..plan import LEAD, HUMAN, LEAD_INSTANCE


def initial(plan):
    return {'confirmed':True,'blocks':{b['id']:{'round':1,'released':None} for b in plan['blocks']},
            'instances':{},'by_node':{},'counters':{},'submissions':{},'input_holds':{},'launched':{},'wake_pending':{},'states':{}}


def invalidate_for_change(data,request):
    """Minimal policy: retain history, require affected live work to finish first.

    Unaffected instances remain the same. Affected work needs an explicit input
    review before a new round; this policy is provisional, not model validation.
    """
    run=data['run']
    touched=set(request['affected'])-{LEAD}
    for node in touched:
        inst=run['instances'].get(run['by_node'].get(node))
        require(not inst or inst['status']=='ended' or
                inst['status'] in ('reserved','blocked') and not inst.get('handle'),
                '先由主控明确结束受影响实例，再应用改图')
    new_version=data['version']+1
    new_nodes={n['id']:n for n in request['after']['nodes']}
    if LEAD in request['affected'] and LEAD in run['submissions']:
        run['submissions'][LEAD]['status']='invalidated'
    for node,instance in list(run['by_node'].items()):
        inst=run['instances'][instance]
        if node not in touched:
            inst['plan_version']=new_version
            continue
        if inst['status']!='ended':
            inst.update(status='ended',end_reason='未启动预留因获批改图取消')
        if node not in new_nodes:
            run['by_node'].pop(node)
            run['wake_pending'].pop(node,None)
            run['input_holds'].pop(node,None)
        else:
            run['wake_pending'][node]='获批计划变更后的新轮次'
    for node in touched:
        if node in run['submissions']:
            run['submissions'][node]['status']='invalidated'
        if node in new_nodes:
            run['input_holds'][node]={'basis':fingerprint({'change':request['id'],'node':node}),
                'reason':'获批计划变更，需确认新一轮执行','source':'plan'}
    old_blocks=run['blocks']
    touched_blocks={n.get('block') for n in data['plan']['nodes']+request['after']['nodes'] if n['id'] in touched}
    run['blocks']={b['id']:({'round':old_blocks.get(b['id'],{}).get('round',0)+1,'released':None}
                          if b['id'] in touched_blocks or b['id'] not in old_blocks else old_blocks[b['id']])
                   for b in request['after']['blocks']}
    for grant in run.get('grants',{}).values():
        if grant['node'] in touched:
            grant['status']='revoked'
        else:
            grant['plan_version']=new_version
    for req in run.get('access',{}).get('requests',{}).values():
        if req['status']=='pending':
            req['status']='cancelled'
    for req in run.get('helper_requests',{}).values():
        if req['node'] in touched:
            req['status']='cancelled'
        else:
            req['plan_version']=new_version


class GraphRun:
    def __init__(self, store):
        self.store = store

    def _load(self):
        data = self.store.load()
        require(data['status'] == 'confirmed', '计划尚未确认')
        if not data['run']:
            data['run'] = initial(data['plan'])
        return data

    def state(self):
        with locked(self.store.path):
            return copy.deepcopy(self._load()['run'])

    def _node(self, data, node):
        found = next((n for n in data['plan']['nodes'] if n['id']==node), None)
        require(found is not None and node != LEAD, '未知工作节点')
        return found

    def _current(self, data, node, instance=None):
        r = data['run']
        current = r['instances'].get(r['by_node'].get(node))
        require(current is not None and (instance is None or current['id'] == instance), '实例不存在或事件已过期')
        return current

    def _hash(self, ref):
        return file_hash(self.store.root,ref)

    def _valid_files(self, submission):
        try:
            return all(self._hash(path) == value for path,value in submission['hashes'].items())
        except (ValueError,OSError):
            return False

    def _hold(self, data, source, reason):
        r = data['run']
        affected = downstream(data['plan'], {source}) - {source}
        for node in affected:
            basis = fingerprint({'node':node,'inputs':{n:s['id'] for n,s in r['submissions'].items()}})
            r['input_holds'][node] = {'basis':basis,'reason':reason,'source':source}
            sub = r['submissions'].get(node)
            if sub and sub['status'] != 'returned':
                sub['status'] = 'needs_check'
        blocks={n['block'] for n in data['plan']['nodes'] if n['id'] in affected | {source} and n.get('block')}
        for block in blocks:
            state=r['blocks'][block]
            if state['released']:
                state['round']+=1
            state['released']=None

    def _audit(self, data, skip=None):
        changed = []
        for node, sub in data['run']['submissions'].items():
            if node != skip and sub['status'] == 'submitted' and not self._valid_files(sub):
                sub['status'] = 'needs_check'
                self._hold(data,node,'正式交付文件在提交后变化')
                changed.append(node)
        if changed:
            self.store._save(data,'graph.inputs_changed','system',{'nodes':changed})

    def ready(self):
        with locked(self.store.path):
            data = self._load()
            self._audit(data)
            return ready(data['plan'],data['run'])

    def _launch_valid(self, data, inst):
        r = data['run']
        node = self._node(data,inst['node'])
        block = next(b for b in data['plan']['blocks'] if b['id']==node['block'])
        require(inst['plan_version'] == data['version'] and inst['node'] not in r['input_holds'], '预留后的计划或输入已变化，先复核')
        require(all(r['blocks'][prior]['released'] for prior in block['after']), '预留后的前置块放行已撤销')
        for source, submission_id in inst['inputs'].items():
            sub = r['submissions'].get(source,{})
            require(sub.get('status') == 'submitted' and sub.get('id') == submission_id and source not in r['input_holds'], '预留后的上游交付已变化')

    def reserve(self, node):
        with locked(self.store.path):
            data = self._load()
            self._node(data,node)
            self._audit(data)
            r = data['run']
            current = r['instances'].get(r['by_node'].get(node))
            if current and current['status'] == 'reserved':
                self._launch_valid(data,current)
                return copy.deepcopy(current)
            require(node in ready(data['plan'],r), '启动条件未满足或本轮已启动')
            number = r['counters'].get(node,0) + 1
            r['counters'][node] = number
            instance = {'id':f'{node}:r{number}','node':node,'round':number,'plan_version':data['version'],
                        'wake_reason':r['wake_pending'].get(node,'首次启动'),'work_epoch':0,'last_state_revision':0,
                        'status':'reserved','handle':None,'inputs':{e['from']:r['submissions'][e['from']]['id']
                        for e in data['plan']['edges'] if e['to']==node and e['start']}}
            r['instances'][instance['id']] = instance
            r['by_node'][node] = instance['id']
            block = self._node(data,node)['block']
            r['launched'][f'{node}@{block}#{r["blocks"][block]["round"]}'] = instance['id']
            r['wake_pending'].pop(node,None)
            folder=f'nodes/{node}/r{number}'
            for part in ('work','submit','state'):
                safe_path(self.store.root,folder+'/'+part).mkdir(parents=True,exist_ok=True)
            safe_path(self.store.root,folder+'/README.md').write_text(
                f'# {node} / r{number}\n\nwork/：当前工作文件，可写。\nsubmit/：正式交付快照，只由程序维护。\nstate/：内部恢复，只由保存状态工具维护。\n',encoding='utf-8')
            self.store._save(data,'graph.reserved','system',instance)
            return copy.deepcopy(instance)

    def attach(self, instance, handle):
        require(isinstance(handle,str) and handle.strip(), '需要实际运行时句柄')
        with locked(self.store.path):
            data = self._load()
            self._audit(data)
            inst = data['run']['instances'].get(instance)
            require(inst is not None and inst['status'] in ('reserved','active'), '不能绑定该实例')
            require(inst['handle'] in (None,handle), '不能重复创建或替换句柄')
            if inst['status'] == 'reserved':
                self._launch_valid(data,inst)
            inst.update(status='active',handle=handle)
            self.store._save(data,'graph.launched','system',inst)

    def failed_start(self,instance,error):
        with locked(self.store.path):
            data=self._load()
            inst=data['run']['instances'][instance]
            require(inst['status']=='reserved','只能阻塞未启动预留')
            inst.update(status='blocked',error=str(error)[:1000])
            self.store._save(data,'graph.start_blocked','system',copy.deepcopy(inst))

    def retry(self,actor,instance,reason):
        require(actor==LEAD and isinstance(reason,str) and reason.strip(),'需主控明确重试依据')
        with locked(self.store.path):
            data=self._load()
            inst=data['run']['instances'].get(instance)
            require(inst and inst['status']=='blocked','没有可重试的启动预留')
            self._launch_valid(data,inst)
            inst.update(status='reserved',error=None)
            self.store._save(data,'graph.start_retry',actor,{'instance':instance,'reason':reason})

    def save_state(self, actor, instance, content):
        validate_state(content)
        with locked(self.store.path):
            data=self._load()
            inst=self._current(data,actor,instance)
            require(inst['status'] in ('active','submitted'), '只能保存当前活实例的 state')
            states=data['run'].setdefault('states',{})
            revision=states.get(instance,{}).get('revision',0)+1
            folder=f'nodes/{actor}/r{inst["round"]}'
            work=work_hashes(self.store.root,folder+'/work')
            ref=folder+'/state/state.md'
            target=safe_path(self.store.root,ref)
            target.write_text(content,encoding='utf-8')
            latest=data['run']['submissions'].get(actor,{})
            record={'path':ref,'sha256':self._hash(ref),'revision':revision,'epoch':inst.get('work_epoch',0),'work_hashes':work,
                    'instance':instance,'plan_version':data['version'],'inputs':copy.deepcopy(inst['inputs']),
                    'latest_submit':latest.get('submit'),'latest_manifest':latest.get('manifest')}
            write(safe_path(self.store.root,folder+'/state/meta.json'),record)
            states[instance]=record
            self.store._save(data,'graph.state_saved',actor,{'instance':instance,'revision':revision})
            return copy.deepcopy(record)

    def publish_lead(self,actor,*,summary,files,known_issues=None,usage='按已确认计划使用主控交付'):
        require(actor==LEAD,'只有内部主控可发布主控交付')
        return self._submit(LEAD,LEAD_INSTANCE,summary=summary,files=files,known_issues=known_issues,usage=usage,controller=True)

    def submit(self, actor, instance, **submission):
        require(actor!=LEAD,'主控使用独立发布入口，不能冒充工作实例')
        return self._submit(actor,instance,**submission)

    def _submit(self, actor, instance, *, summary, files, known_issues=None, usage='按本节点验收要求使用交付文件',controller=False):
        with locked(self.store.path):
            data = self._load()
            if controller:
                r=data['run']
                r['instances'].setdefault(instance,{'id':instance,'node':LEAD,'round':1,'plan_version':data['version'],
                    'status':'active','handle':LEAD,'kind':'controller','inputs':{}})
                r['by_node'][LEAD]=instance
                folder=safe_path(self.store.root,'nodes/lead/r1')
                (folder/'submit').mkdir(parents=True,exist_ok=True)
                (folder/'README.md').write_text('# 主控正式交付\n\nwork/：主控工作文件。\nsubmit/：程序维护的正式入口。\n主控 state 恢复为可选机制，不在此强制启用。\n',encoding='utf-8')
            inst = self._current(data,actor,instance)
            require(inst['status'] in ('active','submitted') and actor not in data['run']['input_holds'], '实例不能提交，先处理输入复核')
            require(isinstance(summary,str) and summary.strip() and isinstance(files,list) and files, '需要交付结论和具体文件')
            validate_submission_text(summary,actor)
            validate_submission_text(usage,actor)
            require(known_issues is None or isinstance(known_issues,list) and all(isinstance(s,str) for s in known_issues), '已知问题必须是文本列表')
            for issue in known_issues or []:
                validate_submission_text(issue,actor)
            prefix = f'nodes/{actor}/r{inst["round"]}/work/'
            require(all(isinstance(f,str) and f.startswith(prefix) for f in files), '只能提交本节点本轮工作文件')
            hashes = {f:self._hash(f) for f in files}
            state=None if controller else check_state(self.store.root,data,inst)
            self._audit(data,skip=actor)
            require(actor not in data['run']['input_holds'], '输入已变化，先复核')
            old = data['run']['submissions'].get(actor)
            revision = old['revision'] + 1 if old else 1
            record = {'id':f'{instance}:s{revision}','instance':instance,'revision':revision,'status':'submitted',
                      'summary':summary,'files':files,'hashes':hashes,'inputs':copy.deepcopy(inst['inputs']),
                      'known_issues':known_issues or [],'usage':usage}
            base=f'nodes/{actor}/r{inst["round"]}/submit/s{revision}'
            manifest=base+'/manifest.json'
            text_ref=base+'/submit.md'
            text='# 交付结论\n'+summary+'\n\n## 文件\n'+'\n'.join(files)+'\n\n## 已知问题\n'+'\n'.join(known_issues or ['无'])+'\n\n## 使用说明\n'+usage+'\n'
            path=safe_path(self.store.root,text_ref)
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(text,encoding='utf-8')
            write(safe_path(self.store.root,manifest),copy.deepcopy(record))
            record.update(manifest=manifest,submit=text_ref)
            hashes.update({manifest:self._hash(manifest),text_ref:self._hash(text_ref)})
            data['run']['submissions'][actor] = record
            inst['status'] = 'submitted'
            if state is not None:
                inst['last_state_revision']=state['revision']
                state.update(latest_submit=text_ref,latest_manifest=manifest)
                write(safe_path(self.store.root,f'nodes/{actor}/r{inst["round"]}/state/meta.json'),state)
            if old:
                self._hold(data,actor,'上游有新正式交付')
            self.store._save(data,'graph.submitted',actor,record)
            return copy.deepcopy(record)

    def release(self, actor, block, number, note):
        require(actor == LEAD, '只有内部主控能明确放行')
        with locked(self.store.path):
            data = self._load()
            self._audit(data)
            r = data['run']
            b = next((b for b in data['plan']['blocks'] if b['id']==block),None)
            require(b is not None and r['blocks'][block]['round'] == number and isinstance(note,str) and note.strip(), '块轮次或验收依据无效')
            require(all(r['blocks'][a]['released'] for a in b['after']), '前置块未放行')
            members = [n['id'] for n in data['plan']['nodes'] if n.get('block') == block]
            require(all(r['submissions'].get(n,{}).get('status') == 'submitted' and n not in r['input_holds'] for n in members), '正式交付未齐或仍待复核')
            r['blocks'][block]['released'] = {'by':LEAD,'round':number,'plan_version':data['version'],'note':note}
            self.store._save(data,'graph.released',actor,{'block':block,**r['blocks'][block]['released']})

    def rework(self, actor, node, reason):
        require(actor == LEAD and isinstance(reason,str) and reason.strip(), '需主控明确返工原因')
        require(node!=LEAD,'主控交付用发布新修订更新，不调用工作实例返工')
        with locked(self.store.path):
            data = self._load()
            inst = self._current(data,node)
            require(inst['status'] == 'submitted', '只能返工已提交实例')
            data['run']['submissions'][node]['status'] = 'returned'
            inst['status'] = 'active'
            inst['work_epoch']=inst.get('work_epoch',0)+1
            self._hold(data,node,reason)
            self.store._save(data,'graph.returned',actor,{'node':node,'reason':reason})

    def review_inputs(self, actor, node, basis, reason):
        require(actor in (HUMAN,LEAD) and isinstance(reason,str) and reason.strip(), '仅操作者或主控可复核输入')
        with locked(self.store.path):
            data = self._load()
            self._audit(data)
            r = data['run']
            hold = r['input_holds'].get(node)
            require(hold is not None and hold['basis'] == basis, '输入复核不存在或已过期')
            sources = [e['from'] for e in data['plan']['edges'] if e['to']==node and e['start']]
            require(all(r['submissions'].get(s,{}).get('status') == 'submitted' for s in sources), '上游尚无有效交付')
            sub = r['submissions'].get(node)
            if sub and sub['status'] == 'needs_check':
                require(self._valid_files(sub), '本节点交付文件已变动，不能确认旧成果可复用')
                sub['status'] = 'submitted'
                sub['inputs'] = {s:r['submissions'][s]['id'] for s in sources}
            if node in r['by_node']:
                self._current(data,node)['inputs'] = {s:r['submissions'][s]['id'] for s in sources}
            del r['input_holds'][node]
            self.store._save(data,'graph.inputs_reviewed',actor,{'node':node,'basis':basis,'reason':reason})

    def end(self, actor, node, instance, reason):
        require(actor == LEAD and isinstance(reason,str) and reason.strip(), '需主控明确结束')
        require(node!=LEAD,'主控生命周期不由工作实例结束工具管理')
        with locked(self.store.path):
            data = self._load()
            inst = self._current(data,node,instance)
            require(inst['status'] == 'submitted', '只有已提交实例可结束')
            state=check_state(self.store.root,data,inst,ending=True)
            inst['last_state_revision']=state['revision']
            inst['status'] = 'ended'
            self.store._save(data,'graph.instance_ended',actor,{'instance':instance,'reason':reason})

    def close_interrupted(self,actor,node,instance,content,reason):
        """Trusted runtime verifies the physical model is already stopped.

        A lead-authored checkpoint is explicitly marked; no delivery is accepted.
        """
        require(actor==LEAD and node!=LEAD and isinstance(reason,str) and reason.strip(),'需内部主控明确记录中断收尾')
        with locked(self.store.path):
            data=self._load()
            inst=self._current(data,node,instance)
            require(inst['status'] in ('active','submitted'),'只能收尾尚未逻辑结束的中断实例')
            self.save_state(node,instance,content+'\n\n## 中断记录来源\n此记录由主控核查已停止的实例后记录，不代表原模型完成或交付通过。\n原因：'+reason+'\n')
            data=self._load()
            inst=self._current(data,node,instance)
            checkpoint=data['run']['states'][instance]
            checkpoint.update(recorded_by=LEAD,kind='interrupted_checkpoint',reason=reason)
            write(safe_path(self.store.root,f'nodes/{node}/r{inst["round"]}/state/meta.json'),checkpoint)
            inst.update(status='ended',outcome='interrupted',last_state_revision=checkpoint['revision'])
            if node in data['run']['submissions']:
                data['run']['submissions'][node]['status']='invalidated'
            self._hold(data,node,'中断后须重新核查成果，不能沿用旧放行')
            self.store._save(data,'graph.interrupted_closed',actor,{'node':node,'instance':instance,'reason':reason,
                'checkpoint':checkpoint['path'],'recorded_by':LEAD,'outcome':'interrupted'})
            return copy.deepcopy(inst)

    def wake(self, actor, node, reason):
        require(actor == LEAD and isinstance(reason,str) and reason.strip(), '需主控明确唤醒')
        require(node!=LEAD,'主控生命周期不由工作实例唤醒工具管理')
        with locked(self.store.path):
            data = self._load()
            inst = self._current(data,node)
            require(inst['status'] == 'ended', '先明确结束旧实例')
            data['run']['wake_pending'][node] = reason
            if node in data['run']['submissions']:
                data['run']['submissions'][node]['status'] = 'returned'
            self._hold(data,node,'上游节点重新唤醒')
            self.store._save(data,'graph.wake_requested',actor,{'node':node,'reason':reason})
