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


def result(workspace,key):
    if not re.fullmatch(r'[0-9a-f]{24}',key):
        raise ValueError('非法操作记录 ID')
    return json.loads((directory(workspace)/(key+'.json')).read_text(encoding='utf-8'))


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
        doc=GraphStore(root).load()
        approvals=root/'state/approvals.json'
        projects.append({'id':meta['id'],'name':meta['name'],'status':doc['status'],
            'plan_hash':fingerprint(doc['plan']),'plan':doc['plan'] if doc['status']=='draft' else None,
            'changes':[c for c in doc['changes'].values() if not c['applied']],
            'tool_approvals':json.loads(approvals.read_text(encoding='utf-8')) if approvals.exists() else []})
    commands=[]
    for path in sorted(directory(workspace).glob('*.json')):
        commands.append(json.loads(path.read_text(encoding='utf-8')))
    return {'projects':projects,'commands':commands}
