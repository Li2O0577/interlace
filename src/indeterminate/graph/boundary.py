"""Path checks for generic graph nodes; not an operating-system sandbox."""
from __future__ import annotations

import ast
import copy
import hashlib
import re
from pathlib import Path, PurePosixPath

from .model import GraphError, require


def safe_path(root, ref):
    require(isinstance(ref,str) and ref and '\\' not in ref and ':' not in ref and '\x00' not in ref, '非法文件引用')
    p=PurePosixPath(ref)
    require(not p.is_absolute() and '..' not in p.parts, '文件引用越界')
    root=Path(root).resolve()
    target=root.joinpath(*p.parts)
    require(target.resolve().is_relative_to(root), '文件引用越界')
    current=target
    while current != root:
        require(not current.is_symlink() and not (hasattr(current,'is_junction') and current.is_junction()), '不允许链接或 junction 绕过文件边界')
        current=current.parent
    return target


def file_hash(root, ref):
    path=safe_path(root,ref)
    require(path.is_file(), '需要具体文件')
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Boundary:
    def __init__(self, root, document, instance):
        self.root=Path(root)
        self.document=document
        self.run=document['run']
        self.instance=self.run['instances'].get(instance)
        require(self.instance is not None, '未知图实例')
        self.node=self.instance['node']
        self.definition=next(n for n in document['plan']['nodes'] if n['id']==self.node)
        self.folder=f'nodes/{self.node}/r{self.instance["round"]}'
        self.work=self.folder+'/work'

    def _under(self, ref, prefix):
        return ref==prefix.rstrip('/') or ref.startswith(prefix.rstrip('/')+'/')

    def _active(self):
        return (self.run['by_node'].get(self.node)==self.instance['id'] and
                self.instance['status'] in ('reserved','active','submitted') and
                self.instance['plan_version']==self.document['version'])

    def inputs(self):
        refs=[]
        if self._active():
            for ref in self.definition.get('reads',[]):
                path=safe_path(self.root,ref)
                if path.is_file():
                    refs.append({'path':ref,'source':'operator','purpose':'计划中明确授权的公共输入','sha256':file_hash(self.root,ref)})
                elif path.is_dir():
                    refs.append({'path':ref,'source':'operator','purpose':'计划中明确授权的公共目录','kind':'directory'})
        for edge in self.document['plan']['edges']:
            if edge['to']!=self.node:
                continue
            source=edge['from']
            sub=self.run['submissions'].get(source)
            if not sub or sub['status']!='submitted' or source in self.run['input_holds']:
                continue
            try:
                if not all(file_hash(self.root,r)==h for r,h in sub['hashes'].items()):
                    continue
            except (GraphError,OSError):
                continue
            for ref in [*sub['files'],sub.get('manifest'),sub.get('submit')]:
                if ref and ref.startswith(f'nodes/{source}/') and '/state/' not in ref:
                    refs.append({'path':ref,'source':source,'purpose':edge['label'],'sha256':sub['hashes'][ref]})
        for grant in self.run.get('grants',{}).values():
            if (grant.get('status')!='active' or grant.get('instance')!=self.instance['id'] or
                    grant.get('plan_version')!=self.document['version'] or not self._active()):
                continue
            for ref,value in grant.get('hashes',{}).items():
                try:
                    if file_hash(self.root,ref)==value:
                        refs.append({'path':ref,'source':'file_keeper','purpose':grant.get('reason',''),'sha256':value})
                except (GraphError,OSError):
                    pass
        return refs

    def readable(self, ref):
        try:
            path=safe_path(self.root,ref)
            if not path.is_file():
                return False
            if self._under(ref,'nodes/'+self.node):
                return True
            if not self._active():
                return False
            if any(self._under(ref,p) for p in self.definition.get('reads',[])):
                return True
            return any(r['path']==ref for r in self.inputs())
        except (GraphError,OSError):
            return False

    def writable(self, ref):
        try:
            path=safe_path(self.root,ref)
            return self._active() and self._under(ref,self.work) and ref!=self.work and not path.is_dir()
        except (GraphError,OSError):
            return False

    def listable(self, ref):
        try:
            path=safe_path(self.root,ref)
            return path.is_dir() and (self._under(ref,'nodes/'+self.node) or self._active() and
                   any(self._under(ref,p) and safe_path(self.root,p).is_dir() for p in self.definition.get('reads',[])))
        except (GraphError,OSError):
            return False

    def _read_or_list(self, ref):
        return self.readable(ref) or self.listable(ref)

    def check_call(self, name, args, contracts=None):
        """Fail closed for tools whose side effects have no graph mapping yet."""
        require(self._active(), '图实例已过期或已结束')
        out=copy.deepcopy(args)
        if name=='fs.read':
            require(self.readable(out.get('path')), '文件未授权；请申请具体材料')
        elif name=='fs.list':
            require(self.listable(out.get('path',self.work)), '不得浏览其他节点目录')
            out.setdefault('path',self.work)
        elif name=='fs.search':
            out.setdefault('path',self.work)
            require(self._read_or_list(out['path']), '搜索范围未授权')
        elif name in ('fs.write','fs.edit'):
            require(self.writable(out.get('path')), '只能写本节点当前轮次的 work')
            for ref in out.get('replaces') or []:
                require(self.writable(ref), '不能借 replaces 操作他人文件')
        elif name in ('fs.move','fs.delete'):
            for field in (('src','dst') if name=='fs.move' else ('path',)):
                require(self.writable(out.get(field)), '移动或删除范围未授权')
        elif name=='code.run':
            # Literal checks help cooperative agents; dynamic paths and subprocess
            # side effects require a real host sandbox and are not contained here.
            code=out.get('code') or ''
            command=out.get('command') or ''
            require(not re.search(r'(os\.walk|os\.listdir|Path\([^)]*["\']\.["\']|glob\([^)]*["\']\*\*)',code+command), '不允许遍历整个项目')
            literals=re.findall(r'["\']([^"\'\n]+)["\']',code+command)
            for value in literals:
                first=value.replace('\\','/').split('/')[0]
                if (value.startswith(('/','\\','../')) or re.match(r'^[A-Za-z]:',value) or
                        ('/' in value or '\\' in value) and (self.root/first).exists()):
                    require(self.readable(value) or self.writable(value), '代码引用了未授权文件')
            try:
                for call in ast.walk(ast.parse(code)):
                    if isinstance(call,ast.Call) and isinstance(call.func,ast.Name) and call.func.id=='open' and call.args and isinstance(call.args[0],ast.Constant):
                        ref=call.args[0].value
                        mode=call.args[1].value if len(call.args)>1 and isinstance(call.args[1],ast.Constant) else 'r'
                        mode=next((k.value.value for k in call.keywords if k.arg=='mode' and isinstance(k.value,ast.Constant)),mode)
                        write=isinstance(mode,str) and any(m in mode for m in 'wax+')
                        require(self.writable(ref) if write else self.readable(ref), 'open 文件范围未授权')
            except SyntaxError:
                pass  # code.run reports syntax errors; this is not a parser replacement.
        elif name.startswith(('graph.','access.')):
            pass  # Their handlers authenticate actor and validate their own records.
        elif contracts and name in contracts:
            # A host extension: the parameters it declared as project paths are held to this node's range.
            contract=contracts[name]
            for keys,allowed,why in ((contract.reads,self.readable,'扩展工具读取的文件未授权；请申请具体材料'),
                                     (contract.writes,self.writable,'扩展工具只能写本节点当前轮次的 work')):
                for key in keys:
                    value=out.get(key)
                    for ref in value if isinstance(value,list) else [value]:
                        if ref is None:
                            continue
                        require(isinstance(ref,str) and allowed(ref), why)
        else:
            raise GraphError('图节点尚未接入此工具的文件边界：'+name)
        return out


def for_agent(manager, state):
    """Find the graph owner of an agent or one of its approved descendants."""
    seen=set()
    owner=state
    while owner is not None and owner.rec['id'] not in seen:
        seen.add(owner.rec['id'])
        graph=owner.rec.get('graph')
        if graph:
            from .store import GraphStore
            data=GraphStore(manager._root(state.project_id)).load()
            require(data['status']=='confirmed' and data['run'], '图计划或运行记录不存在')
            instance=f'{graph["node"]}:r{graph["round"]}'
            boundary=Boundary(manager._root(state.project_id),data,instance)
            require(boundary.instance['round']==graph['round'] and boundary.instance['handle']==owner.rec['id'], '图实例元数据过期')
            return boundary
        parent=owner.rec.get('parent')
        owner=manager._get(state.project_id,parent) if parent else None
    return None


def validate_submission_text(text,node):
    require(isinstance(text,str), '提交说明必须是文本')
    normalized=text.replace('\\','/')
    for owner in re.findall(r'(?i)nodes/([a-z0-9_-]+)/',normalized):
        require(owner==node, 'submit 不能转授其他节点的文件地址')
    require(not re.search(r'(?i)(agents/|state/|\.\./|file://|https?://|[a-z]:/|(?:^|\s)/(?:[^\s]+))',normalized), 'submit 不能夹带内部、绝对或外部文件地址；请列本节点交付文件')
