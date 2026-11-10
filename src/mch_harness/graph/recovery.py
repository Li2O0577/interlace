"""Minimal recovery records, with references rather than private reasoning."""
from __future__ import annotations

import re

from .boundary import Boundary, file_hash, safe_path
from .model import require

STATE_SECTIONS=('进度','关键决定与依据','失败的尝试','未完成与下一步','相关文件与输入')
STATE_TEMPLATE='\n\n'.join('## '+s+'\n（无事项时写明无；只保存可核查工作记录）' for s in STATE_SECTIONS)+'\n'


def validate_state(content):
    require(isinstance(content,str) and len(content.encode('utf-8'))<=200_000, 'state 必须为不超过 200KB 的文本')
    headings=list(re.finditer(r'^##\s+(.+?)\s*$',content,re.M))
    sections={h.group(1):content[h.end():headings[i+1].start() if i+1<len(headings) else len(content)].strip() for i,h in enumerate(headings)}
    require(all(sections.get(s) for s in STATE_SECTIONS), 'state 缺少必备节或节内容为空')


def work_hashes(root, folder):
    result={}
    start=safe_path(root,folder)
    def walk(directory):
        if not directory.exists():
            return
        for child in directory.iterdir():
            ref=child.relative_to(root).as_posix()
            safe_path(root,ref)
            if child.is_dir():
                walk(child)
            elif child.is_file():
                result[ref]=file_hash(root,ref)
    walk(start)
    return result


def check_state(root,document,inst, *, ending=False):
    state=document['run'].get('states',{}).get(inst['id'])
    require(state is not None, '先更新 state 再提交或结束')
    require(file_hash(root,state['path'])==state['sha256'], 'state 被绕过工具修改')
    require(state['epoch']==inst.get('work_epoch',0), '返工后必须更新 state')
    require(state['revision']>inst.get('last_state_revision',0), '本次提交或结束前必须重新保存 state')
    folder=f'nodes/{inst["node"]}/r{inst["round"]}/work'
    require(state['work_hashes']==work_hashes(root,folder), '工作文件在 state 保存后变化，先更新 state')
    return state


def recovery_refs(root,document,instance):
    boundary=Boundary(root,document,instance)
    node=boundary.node
    history=[]
    for inst in document['run']['instances'].values():
        if inst['node']!=node or inst['round']>=boundary.instance['round']:
            continue
        state=document['run'].get('states',{}).get(inst['id'])
        if state and boundary.readable(state['path']) and file_hash(root,state['path'])==state['sha256']:
            history.append(state['path'])
    latest=document['run']['submissions'].get(node)
    if latest and latest.get('manifest') and latest.get('submit'):
        for ref in (latest['manifest'],latest['submit']):
            if boundary.readable(ref) and file_hash(root,ref)==latest['hashes'][ref]:
                history.append(ref)
    return {'history':history,'inputs':boundary.inputs()}
