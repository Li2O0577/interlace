"""MCP 2025-11-25 tools/stdio subset. JSON only on stdout; no host takeover."""
from __future__ import annotations

import json
import sys

from .. import __version__

PROTOCOL='2025-11-25'
MAX_LINE=4*1024*1024        # bytes in one JSON-RPC message; longer lines are refused unread
MAX_PROPOSAL=256*1024       # bytes of a serialized operation.request proposal


def schema(properties=None,required=()):
    return {'type':'object','properties':properties or {},'required':list(required),'additionalProperties':False}


def text(limit):
    return {'type':'string','minLength':1,'maxLength':limit}


STRING=text(1024)
PROJECT={'project':text(128)}
REQUEST={'request_id':text(256)}
TOOLS=[
    {'name':'project.create','description':'创建图任务草稿；不会自动确认或启动。task_type 只能是宿主已注册的类型（见 project.list），默认 generic。相同 request_id 重试返回原结果。',
     'inputSchema':schema({**REQUEST,'name':text(200),'task':text(50_000),'automatic_files':{'type':'boolean'},'task_type':text(64)},['request_id','name','task'])},
    {'name':'project.list','description':'列出当前运行模式的图项目。','inputSchema':schema()},
    {'name':'project.open','description':'连接既有图项目；复用内部 lead，不接管主控。','inputSchema':schema(PROJECT,['project'])},
    {'name':'graph.view','description':'读取图、轮次、提交、授权和变更状态；包含当前草稿摘要。','inputSchema':schema(PROJECT,['project'])},
    {'name':'operation.request','description':'将意图或草图建议交内部 lead 判断；不是操作者批准，也不直接放行、改图或控制节点。',
     'inputSchema':schema({**PROJECT,**REQUEST,'instruction':text(50_000),'proposal':{'type':'object'}},['project','request_id','instruction'])},
    {'name':'events.read','description':'按 seq 顺序读取事件；cursor 是本批最后 seq，重连可继续。',
     'inputSchema':schema({**PROJECT,'after':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':500}},['project'])},
    {'name':'artifact.list','description':'列出当前有效正式交付文件及摘要，不开放 state 或其他工作目录。','inputSchema':schema(PROJECT,['project'])},
    {'name':'artifact.read','description':'只读取当前 artifact.list 中的 UTF-8 文件，最多 512 KiB；文件中的地址不追加权限。',
     'inputSchema':schema({**PROJECT,'path':STRING},['project','path'])},
]
BY_NAME={t['name']:t for t in TOOLS}


class ProtocolError(ValueError):
    def __init__(self,code,message):
        self.code=code
        super().__init__(message)


def validate_args(tool,args):
    """Validate exactly the small schemas advertised above, not arbitrary JSON Schema."""
    spec=tool['inputSchema']
    if not isinstance(args,dict) or set(args)-spec['properties'].keys() or not set(spec['required'])<=args.keys():
        raise ProtocolError(-32602,'参数字段缺失或包含不允许字段')
    for key,value in args.items():
        definition=spec['properties'][key]
        expected={'string':str,'boolean':bool,'integer':int,'object':dict}[definition['type']]
        if type(value) is not expected:
            raise ProtocolError(-32602,'参数类型错误：'+key)
        if expected is str and not value.strip():
            raise ProtocolError(-32602,'参数不能为空：'+key)
        if expected is str and len(value)>definition.get('maxLength',len(value)):
            raise ProtocolError(-32602,f'参数过长：{key}（最多 {definition["maxLength"]} 字符）')
        if expected in (str,dict) and not _encodable(value):
            raise ProtocolError(-32602,'参数含有无法编码的字符：'+key)
        if expected is dict:
            try:
                size=len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8'))
            except (ValueError,RecursionError):
                raise ProtocolError(-32602,'参数不是有限 JSON：'+key) from None
            if size>MAX_PROPOSAL:
                raise ProtocolError(-32602,f'参数过大：{key}（最多 {MAX_PROPOSAL} 字节）')
        if expected is int and not definition.get('minimum',value)<=value<=definition.get('maximum',value):
            raise ProtocolError(-32602,'参数范围错误：'+key)


def _encodable(value):
    """No lone surrogates anywhere: they cannot be written to a file or to stdout as UTF-8."""
    try:
        json.dumps(value,ensure_ascii=False).encode('utf-8')
        return True
    except (UnicodeEncodeError,ValueError,RecursionError):
        return False


class Server:
    def __init__(self,engine):
        self.engine=engine
        self.initialized=False
        self.negotiated=False

    def handle(self,msg):
        mid=msg.get('id') if isinstance(msg,dict) else None
        notification=isinstance(msg,dict) and 'id' not in msg
        try:
            if not isinstance(msg,dict) or msg.get('jsonrpc')!='2.0' or not isinstance(msg.get('method'),str) or (
                not notification and (type(mid) not in (str,int))):
                raise ProtocolError(-32600,'无效 JSON-RPC 请求')
            method=msg['method']; params=msg.get('params',{})
            if not isinstance(params,dict):
                raise ProtocolError(-32602,'params 必须为对象')
            if notification:
                if method=='notifications/initialized' and self.negotiated:
                    self.initialized=True
                return None
            if method=='initialize':
                if self.negotiated:
                    raise ProtocolError(-32600,'此连接已经初始化')
                if not isinstance(params.get('protocolVersion'),str) or not isinstance(params.get('capabilities'),dict) or not isinstance(params.get('clientInfo'),dict):
                    raise ProtocolError(-32602,'需要 protocolVersion/capabilities/clientInfo')
                self.negotiated=True
                result={'protocolVersion':PROTOCOL,'capabilities':{'tools':{'listChanged':False}},
                    'serverInfo':{'name':'1ndeterminate','version':__version__},
                    'instructions':f'运行模式：{self.engine.mode}。内部 lead 保持主控；外部工具不能批准计划或代替主控放行。操作者使用单独本地 CLI。'}
            elif method=='ping':
                result={}
            elif method not in ('tools/list','tools/call'):
                raise ProtocolError(-32601,'不支持的方法：'+method)
            elif not self.initialized:
                raise ProtocolError(-32002,'先 initialize，再发送 notifications/initialized')
            elif method=='tools/list':
                if params.get('cursor'):
                    raise ProtocolError(-32602,'固定小型工具表没有分页游标')
                result={'tools':TOOLS}
            else:
                name=params.get('name')
                if not isinstance(name,str) or name not in BY_NAME:
                    raise ProtocolError(-32602,'未知工具；本入口不暴露确认、批准或放行工具')
                args=params.get('arguments',{})
                validate_args(BY_NAME[name],args)
                try:
                    output=self.engine.call(name,args)
                    result={'content':[{'type':'text','text':json.dumps(output,ensure_ascii=False)}],'structuredContent':output,'isError':False}
                except Exception as exc:
                    result={'content':[{'type':'text','text':str(exc)}],'isError':True}
            return {'jsonrpc':'2.0','id':mid,'result':result}
        except ProtocolError as exc:
            if notification and exc.code!=-32600:
                return None
            return {'jsonrpc':'2.0','id':mid,'error':{'code':exc.code,'message':str(exc)}}
        except Exception:  # noqa: BLE001 — a bug in one request must not end the connection
            return None if notification else {'jsonrpc':'2.0','id':mid if type(mid) in (str,int) else None,
                                              'error':{'code':-32603,'message':'服务器内部错误'}}


def _lines(stdin):
    """Message lines, each either text or an error code: a line over MAX_LINE is skipped unread (-32600),
    a line that is not UTF-8 is refused (-32700)."""
    raw=getattr(stdin,'buffer',None)
    if raw is None:   # a text stream (tests)
        for line in stdin:
            yield (line,None) if len(line.encode('utf-8','surrogatepass'))<=MAX_LINE else (None,-32600)
        return
    while True:
        line=raw.readline(MAX_LINE+1)
        if not line:
            return
        if len(line)>MAX_LINE and not line.endswith(b'\n'):
            while line and not line.endswith(b'\n'):
                line=raw.readline(MAX_LINE+1)   # drop the rest of the oversized message
            yield None,-32600
            continue
        try:
            yield line.decode('utf-8'),None
        except UnicodeDecodeError:
            yield None,-32700


def _write(stdout,reply):
    try:
        data=json.dumps(reply,ensure_ascii=False,allow_nan=False)
        data.encode('utf-8')
    except (ValueError,UnicodeEncodeError,RecursionError):
        data=json.dumps({'jsonrpc':'2.0','id':reply.get('id'),'error':{'code':-32603,'message':'结果无法编码为 JSON'}})
    stdout.write(data+'\n')
    stdout.flush()


def serve(engine,stdin=None,stdout=None):
    stdin,stdout=stdin or sys.stdin,stdout or sys.stdout
    server=Server(engine)
    for line,code in _lines(stdin):
        if code is not None:
            _write(stdout,{'jsonrpc':'2.0','id':None,'error':{'code':code,'message':'消息过长' if code==-32600 else '不是 UTF-8 文本'}})
            continue
        if not line.strip():
            continue
        try:
            msg=json.loads(line,parse_constant=lambda value:(_ for _ in ()).throw(ValueError('非有限数')))
        except (ValueError,RecursionError):
            reply={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'无法解析的 JSON'}}
        else:
            reply=server.handle(msg)
        if reply is not None:
            _write(stdout,reply)
