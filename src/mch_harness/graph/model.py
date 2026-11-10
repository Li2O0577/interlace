"""Pure graph validation. Feedback can cycle; launch dependencies cannot."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import PurePosixPath

THINKING_LEVELS=('', 'default', 'low', 'medium', 'high')


class GraphError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise GraphError(message)


def fingerprint(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _keys(value, required, optional=()):
    require(isinstance(value, dict), '定义必须是对象')
    require(set(required) <= value.keys() and value.keys() <= set(required) | set(optional), '定义字段缺失或包含未知字段')


def _id(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', value), '非法 ID')
    require(value.lower() not in {'con','prn','aux','nul',*[f'com{i}' for i in range(1,10)],*[f'lpt{i}' for i in range(1,10)]}, '保留文件名不能用作 ID')


def _text(value):
    require(isinstance(value, str) and value.strip(), '说明必须是非空文本')


def validate_configuration(value):
    require(isinstance(value.get('model',''),str),'model 必须为字符串')
    require(value.get('model','')=='' or value['model'].strip(),'model 不能为空白')
    require(value.get('thinking','') in THINKING_LEVELS,'未知思考强度')


def _acyclic(ids, edges):
    pending = set(ids)
    while pending:
        roots = {n for n in pending if not any(a in pending and b == n for a,b in edges)}
        require(roots, '启动依赖或任务块依赖存在环')
        pending -= roots


def validate(plan):
    _keys(plan, ('nodes','edges','blocks'), ('task',))
    for field in ('nodes','edges','blocks'):
        require(isinstance(plan[field], list), field + ' 必须是列表')
        ids = []
        for item in plan[field]:
            require(isinstance(item, dict) and 'id' in item, '缺少 ID')
            _id(item['id'])
            ids.append(item['id'])
        require(len(ids) == len(set(ids)), 'ID 重复')
    nodes, blocks = {n['id']:n for n in plan['nodes']}, {b['id']:b for b in plan['blocks']}
    require('lead' in nodes and nodes['lead'].get('role') == 'lead', '必须有内部主控 lead')
    for n in nodes.values():
        _keys(n, ('id','name','summary','role'), ('duty','block','accept','stance','reads','tools','model','thinking','helpers'))
        validate_configuration(n)
        for f in ('name','summary','role'):
            _text(n[f])
        if n['id'] == 'lead':
            require(n.get('block') is None, '主控不属于任务块')
        else:
            require(n['role'] != 'lead' and n.get('block') in blocks, '工作节点必须属于有效任务块且不能冒充主控')
            _text(n.get('duty'))
            _text(n.get('accept'))
        require(type(n.get('helpers', False)) is bool, 'helpers 必须为布尔值')
        require(n.get('stance','') in ('','proof'), '未知 stance')
        reads = n.get('reads', [])
        require(isinstance(reads, list), 'reads 必须为列表')
        for path in reads:
            require(isinstance(path,str) and path and '\\' not in path and ':' not in path, '公共输入路径无效')
            p = PurePosixPath(path)
            require(not p.is_absolute() and '..' not in p.parts and p.parts and p.parts[0] not in ('nodes','state','agents','.git'), '公共输入不能扩大节点或内部状态的访问权限')
        tools = n.get('tools')
        require(tools is None or isinstance(tools,list) and all(isinstance(t,str) and t for t in tools), 'tools 必须为空或工具列表')
    for b in blocks.values():
        _keys(b, ('id','name','after'),('coordinator',))
        _text(b['name'])
        if b.get('coordinator'):
            require(isinstance(b['coordinator'],str) and b['coordinator'] in nodes and nodes[b['coordinator']]['role']=='sublead',
                    '任务块协调者必须是已有次主控节点')
        else:
            require(b.get('coordinator','')=='','协调者应为空或次主控节点 ID')
        require(isinstance(b['after'],list) and all(isinstance(a,str) for a in b['after']), 'after 必须是 ID 列表')
        require(len(b['after']) == len(set(b['after'])) and set(b['after']) <= blocks.keys(), '无效前置任务块')
        require(any(n.get('block') == b['id'] for n in nodes.values()), '任务块不能为空')
    _acyclic(blocks, [(a,b['id']) for b in blocks.values() for a in b['after']])
    startup = []
    for e in plan['edges']:
        _keys(e, ('id','from','to','kind','start','label'))
        require(e['from'] in nodes and e['to'] in nodes and e['from'] != e['to'], '无效信息流端点')
        require(e['kind'] in ('deliverable','review','feedback','rework') and type(e['start']) is bool, '无效信息流类型')
        _text(e['label'])
        if e['start']:
            require(e['to'] != 'lead', '主控不由图调度启动')
            startup.append((e['from'],e['to']))
    _acyclic(nodes, startup)
    # Combine block gates with launch inputs to catch cross-block deadlocks.
    effective = list(startup)
    for b in blocks.values():
        for prior in b['after']:
            effective.extend((a['id'],z['id']) for a in nodes.values() for z in nodes.values()
                             if a.get('block') == prior and z.get('block') == b['id'])
    _acyclic(nodes, effective)
    if 'task' in plan:
        _text(plan['task'])
    return copy.deepcopy(plan)


def downstream(plan, seeds):
    out = set(seeds)
    while True:
        old = set(out)
        out |= {e['to'] for e in plan['edges'] if e['start'] and e['from'] in out}
        touched = {n.get('block') for n in plan['nodes'] if n['id'] in out}
        later = {b['id'] for b in plan['blocks'] if set(b['after']) & touched}
        out |= {n['id'] for n in plan['nodes'] if n.get('block') in later}
        if old == out:
            return out


def affected(before, after):
    seeds = set()
    for collection in ('nodes','edges','blocks'):
        a,b = ({x['id']:x for x in p[collection]} for p in (before,after))
        for key in a.keys() | b.keys():
            if a.get(key) == b.get(key):
                continue
            for item,plan in ((a.get(key),before),(b.get(key),after)):
                if not item:
                    continue
                if collection == 'nodes':
                    seeds.add(item['id'])
                elif collection == 'edges':
                    seeds |= {item['from'],item['to']}
                else:
                    seeds |= {n['id'] for n in plan['nodes'] if n.get('block') == item['id']}
                    if item.get('coordinator'):
                        seeds.add(item['coordinator'])
    if before.get('task') != after.get('task'):
        seeds |= {n['id'] for n in before['nodes'] + after['nodes']}
    return sorted(downstream(before,seeds) | downstream(after,seeds))
