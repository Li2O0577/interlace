"""Local operator queue, deliberately not an MCP tool or a generic executor.

The OS user can write this queue. It is not authentication of a human identity.
"""
import json
import re
import secrets
import time
from pathlib import Path

from ..core.jsonfile import write


def directory(workspace):
    return Path(workspace)/'.harness-control'


def enqueue(workspace,command,**args):
    key=secrets.token_hex(12)
    record={'schema':1,'id':key,'command':command,'args':args,'status':'pending','created':time.time()}
    write(directory(workspace)/(key+'.json'),record)
    return record


def read(path):
    """One queue record. A file that is not a record is set aside as ``*.corrupt`` (kept for inspection) and
    replaced by a failed record, so one bad file neither blocks the engine nor silently disappears."""
    try:
        data=json.loads(Path(path).read_text(encoding='utf-8'))
        if isinstance(data,dict) and data.get('id')==Path(path).stem:
            return data
    except (ValueError,OSError):
        pass
    path=Path(path)
    kept=path.with_name(path.name+'.corrupt')
    try:
        path.replace(kept)
    except OSError:
        pass
    data={'schema':1,'id':path.stem,'status':'failed','error':f'操作记录损坏，未执行；原文件保留为 {kept.name}'}
    write(path,data)
    return data


def result(workspace,key):
    if not re.fullmatch(r'[0-9a-f]{24}',key):
        raise ValueError('非法操作记录 ID')
    return read(directory(workspace)/(key+'.json'))


def pending(workspace):
    """Read-only; does not claim engine ownership or construct a manager."""
    from ..core.projects import ProjectStore
    from ..graph.model import fingerprint
    from ..graph.store import GraphStore
    store=ProjectStore(Path(workspace))
    projects=[]
    for meta in store.list():
        root=store.path(meta['id'])
        if not (root/'state/graph.json').exists():
            continue
        try:
            doc=GraphStore(root).load()
        except ValueError as exc:
            projects.append({'id':meta['id'],'name':meta['name'],'error':str(exc)})
            continue
        approvals=root/'state/approvals.json'
        projects.append({'id':meta['id'],'name':meta['name'],'status':doc['status'],
            'plan_hash':fingerprint(doc['plan']),'plan':doc['plan'] if doc['status']=='draft' else None,
            'changes':[c for c in doc['changes'].values() if not c['applied']],
            'tool_approvals':json.loads(approvals.read_text(encoding='utf-8')) if approvals.exists() else []})
    commands=[]
    for path in sorted(directory(workspace).glob('*.json')):
        commands.append(read(path))
    return {'projects':projects,'commands':commands}
