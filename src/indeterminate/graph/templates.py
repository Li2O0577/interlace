"""Editable starter, not a confirmed plan."""


def starter(task):
    return {
        'task':task,
        'nodes':[
            {'id':'lead','name':'主控','summary':'统筹、验收和放行','role':'lead'},
            {'id':'worker','name':'执行','summary':'完成当前任务','role':'worker','block':'work',
             'duty':'完成操作者任务，产出可核查的工作文件','accept':'结论有依据，文件可使用'},
            {'id':'inspector','name':'检查员','summary':'检查工作和依据','role':'reviewer','block':'work',
             'duty':'检查执行节点的正式交付，写出问题及检查结论','accept':'列出检查依据、已知问题和是否通过'},
            {'id':'delivery','name':'整理','summary':'汇总已检查的成果','role':'worker','block':'delivery',
             'duty':'按检查结论整理最终交付','accept':'交付完整、一致、注明限制'},
        ],
        'edges':[
            {'id':'check','from':'worker','to':'inspector','kind':'deliverable','start':True,'label':'工作成果'},
            {'id':'feedback','from':'inspector','to':'worker','kind':'feedback','start':False,'label':'检查意见'},
            {'id':'report','from':'inspector','to':'lead','kind':'review','start':False,'label':'检查结论'},
            {'id':'pack','from':'inspector','to':'delivery','kind':'deliverable','start':True,'label':'检查报告'},
            {'id':'result','from':'delivery','to':'lead','kind':'deliverable','start':False,'label':'最终交付'},
        ],
        'blocks':[{'id':'work','name':'执行与检查','after':[]},
                  {'id':'delivery','name':'整理交付','after':['work']}],
    }
