"""The engine behind the stdio MCP server: a Kernel plus the mapping of the eight external tools.

Process-local. Despite the module name, NOT a daemon: closing stdin stops it.
"""
from __future__ import annotations

from ..kernel import Kernel


class Engine(Kernel):
    def __init__(self,workspace,home,*,demo=False,**options):
        super().__init__(workspace,home,simulation=demo,**options)
        if demo:
            from .demo import factory
            self.manager.provider_factory=factory(self)

    def call(self,name,args):
        """One external tool call. Arguments were checked against the advertised schema by the server."""
        mode={'runtime_mode':self.mode}
        if name=='project.create':
            return self.create_project(args['name'],args['task'],task_type=args.get('task_type','generic'),
                automatic_files=args.get('automatic_files',False),request_id=args['request_id'],_args=args)
        if name=='project.list':
            return {'projects':self.projects(),'task_types':self.task_types(),**mode}
        pid=args['project']
        if name=='project.open':
            return {**self.open(pid),**mode}
        if name=='graph.view':
            return {**self.graph(pid),**mode}
        if name=='operation.request':
            return self.request(pid,args['request_id'],args['instruction'],args.get('proposal'),_args=args)
        if name=='events.read':
            return {**self.events_since(pid,args.get('after',0),args.get('limit',100)),**mode}
        if name=='artifact.list':
            return {'files':self.artifacts(pid),**mode}
        if name=='artifact.read':
            return {**self.read_artifact(pid,args['path']),**mode}
        raise ValueError('未知外部工具')
