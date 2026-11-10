"""mch-harness: standalone stdio server and separate local operator commands."""
import argparse
import json
import os
from pathlib import Path
import sys

from . import control


def main(argv=None):
    for stream in (sys.stdin,sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(prog='mch-harness',description='Hyperplan 通用图任务引擎；stdio 连接关闭会停止本进程工作')
    home=Path(os.environ.get('MCH_HOME',Path.home()/'.mch'))
    parser.add_argument('--home',type=Path,default=home,help='提供器设置目录，不放入项目')
    parser.add_argument('--workspace',type=Path,default=Path(os.environ.get('MCH_WORKSPACE',home/'workspace')))
    sub=parser.add_subparsers(dest='command',required=True)
    server=sub.add_parser('serve',help='进程内 stdio MCP 服务，非后台守护服务')
    server.add_argument('--demo',action='store_true',help='显式使用 FakeProvider 模拟；不调用外部模型。建议使用独立工作区')
    sub.add_parser('pending',help='查看草稿、改图差异和工具审批，不修改它们')
    result=sub.add_parser('result',help='读取本地命令的处理结果')
    result.add_argument('id')
    confirm=sub.add_parser('confirm',help='操作者核对草稿后按 SHA256 确认')
    confirm.add_argument('project'); confirm.add_argument('hash')
    approve=sub.add_parser('approve-change',help='批准指定改图摘要；仍由内部 lead 应用')
    approve.add_argument('project'); approve.add_argument('request'); approve.add_argument('hash')
    decide=sub.add_parser('decide-tool',help='只处理现有工具审批，不开放自动批准或批量永许')
    decide.add_argument('project'); decide.add_argument('approval'); decide.add_argument('decision',choices=['approve','deny'])
    decide.add_argument('--reason',default='')
    message=sub.add_parser('message',help='操作者回复内部 lead 的问题，不经外部 MCP 伪装成人工回复')
    message.add_argument('project'); message.add_argument('text')
    resume=sub.add_parser('resume',help='操作者检查暂停原因后恢复已暂停会话；不会唤醒已结束图实例')
    resume.add_argument('project'); resume.add_argument('agent'); resume.add_argument('--reason',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='serve':
            from .daemon import Engine
            from .server import serve
            with Engine(args.workspace,args.home,demo=args.demo) as engine:
                print('Hyperplan '+engine.mode+'; stdio EOF stops this engine.',file=sys.stderr,flush=True)
                serve(engine)
            return
        if args.command=='pending':
            output=control.pending(args.workspace)
        elif args.command=='result':
            output=control.result(args.workspace,args.id)
        else:
            fields={k:v for k,v in vars(args).items() if k not in ('command','workspace','home')}
            output=control.enqueue(args.workspace,args.command,**fields)
        print(json.dumps(output,ensure_ascii=False,indent=2))
    except (ValueError,OSError,RuntimeError) as exc:
        print(str(exc),file=sys.stderr)
        raise SystemExit(1) from exc


if __name__=='__main__':
    main()
