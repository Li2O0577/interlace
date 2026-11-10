"""Authenticated graph tools shared by the browser and model runtimes."""
from .registry import tool, ToolError
from ..graph.model import fingerprint, THINKING_LEVELS

S={'type':'string'}
O={'type':'object'}
A={'type':'array','items':S}
I={'type':'integer'}


def runtime(ctx):
    if ctx.agents is None:
        raise ToolError('图工具需要引擎运行时')
    return ctx.agents.graph(ctx.project_id)


@tool('graph.view','查看流程图','读取图计划、审批和运行状态。',readonly=True)
def view(ctx):
    r=runtime(ctx)
    data=r.store.load()
    return {**data,'draft_hash':fingerprint(data['plan']) if data['plan'] else None}


@tool('graph.models','可用节点模型','供操作者和内部总主控拟图时查看配置过的 API/CLI 模型，不返回凭据。',readonly=True)
def models(ctx):
    r=runtime(ctx)
    if r.identity(ctx.actor) not in ('human','lead'):
        raise ToolError('模型目录由操作者或总主控查看')
    return {**ctx.settings.model_catalog(),'thinking':list(THINKING_LEVELS[1:])}


@tool('graph.draft','拟定图计划','操作者或内部主控保存未确认草稿。',{'plan':O},['plan'])
def draft(ctx,plan):
    return runtime(ctx).store.draft(ctx.actor,plan)


@tool('graph.change','申请局部改图','仅主控提出：包含前后定义、原因、受影响节点及成果处理。',
      {'plan':O,'reason':S,'handling':S},['plan','reason','handling'])
def change(ctx,plan,reason,handling):
    r=runtime(ctx)
    return r.store.propose(r.identity(ctx.actor),plan,reason=reason,handling=handling)


@tool('graph.approve','操作者批准改图','仅操作者按具体变更摘要批准，模型不能代批。',{'request':S,'plan_hash':S},['request','plan_hash'])
def approve(ctx,request,plan_hash):
    r=runtime(ctx)
    r.store.approve(ctx.actor,request,plan_hash)
    r.notify('graph.change_approved',{'request':request,'hash':plan_hash})
    return {'request':request,'approved':True}


@tool('graph.apply','应用已获批变更','仅内部主控应用；先结束受影响实例，保留无关实例。',{'request':S},['request'])
def apply(ctx,request):
    return runtime(ctx).apply_change(ctx.actor,request)


@tool('graph.retry','重试创建预留','主控检查创建故障后重试同一轮次，复用已有句柄。',{'instance':S,'reason':S},['instance','reason'])
def retry(ctx,instance,reason):
    r=runtime(ctx)
    r.run.retry(r.identity(ctx.actor),instance,reason)
    r.tick()
    return r.run.state()['instances'][instance]


@tool('graph.confirm','确认图计划','仅操作者可确认具体摘要，不能由模型代批。',{'plan_hash':S,'authorization':S},['plan_hash'])
def confirm(ctx,plan_hash,authorization=None):
    r=runtime(ctx)
    out=r.store.confirm(ctx.actor,plan_hash)
    if authorization is not None:
        r.access.configure(ctx.agents.store.get(ctx.project_id).get('autonomy')=='full',
                           task=out['plan'].get('task') or '完成当前项目任务',authorization=authorization)
    r.tick()
    return r.store.load()


@tool('graph.save_state','保存节点状态','保存工作记录、依据、失败和待办，不保存内部推理。',{'content':S},['content'])
def save_state(ctx,content):
    r=runtime(ctx)
    node,instance=r.node_instance(ctx.actor)
    return r.run.save_state(node,instance,content)


@tool('graph.submit','正式交付','先更新 state；提交后等待审查，只有主控结束才结束。',
      {'summary':S,'files':A,'known_issues':A,'usage':S},['summary','files'])
def submit(ctx,summary,files,known_issues=None,usage='按节点要求使用'):
    return runtime(ctx).submit(ctx.actor,summary,files,known_issues,usage)


@tool('graph.publish','主控正式交付','内部主控发布自己的工作文件，供图中下游使用；不启用主控 state 要求。',
      {'summary':S,'files':A,'known_issues':A,'usage':S},['summary','files'])
def publish(ctx,summary,files,known_issues=None,usage='按已确认计划使用主控交付'):
    r=runtime(ctx)
    out=r.run.publish_lead(r.identity(ctx.actor),summary=summary,files=files,known_issues=known_issues,usage=usage)
    r.tick()
    return out


@tool('graph.release','主控放行任务块','仅内部主控明确验收后调用，操作者界面不能代替。',{'block':S,'round':I,'note':S},['block','round','note'])
def release(ctx,block,round,note):
    r=runtime(ctx)
    r.run.release(r.identity(ctx.actor),block,round,note)
    r.tick()
    return r.run.state()['blocks'][block]


@tool('graph.block_status','查看协调块进度','指定次主控只能查看负责块的状态；不提供额外文件授权。',{'block':S},['block'],readonly=True)
def block_status(ctx,block):
    r=runtime(ctx)
    return r.coordination.status(r.identity(ctx.actor),block)


@tool('graph.request_review','申请总主控验收','指定次主控汇总进度后申请验收；本工具不会放行任务块。',
      {'block':S,'round':I,'summary':S},['block','round','summary'])
def request_review(ctx,block,round,summary):
    return runtime(ctx).request_block_review(ctx.actor,block,round,summary)


@tool('graph.return','退回交付','内部主控让原实例返工。',{'node':S,'reason':S},['node','reason'])
def rework(ctx,node,reason):
    return runtime(ctx).rework(ctx.actor,node,reason)


@tool('graph.end','结束图实例','主控明确结束已提交且更新 state 的实例。',{'node':S,'instance':S,'reason':S},['node','instance','reason'])
def end(ctx,node,instance,reason):
    return runtime(ctx).end(ctx.actor,node,instance,reason)


@tool('graph.close_interrupted','主控记录已停止实例','仅已物理停止且执行退出的实例；记录主控恢复材料，不接受交付、不自动唤醒。',
      {'node':S,'instance':S,'content':S,'reason':S},['node','instance','content','reason'])
def close_interrupted(ctx,node,instance,content,reason):
    return runtime(ctx).close_interrupted(ctx.actor,node,instance,content,reason)


@tool('graph.wake','唤醒新轮次','主控结束旧实例后创建新轮次。',{'node':S,'reason':S},['node','reason'])
def wake(ctx,node,reason):
    r=runtime(ctx)
    r.run.wake(r.identity(ctx.actor),node,reason)
    r.tick()
    return r.run.state()


@tool('graph.review_inputs','复核输入更新','操作者或主控按当前 basis 确认后才能解除新启动阻塞。',{'node':S,'basis':S,'reason':S},['node','basis','reason'])
def review(ctx,node,basis,reason):
    r=runtime(ctx)
    r.run.review_inputs(r.identity(ctx.actor),node,basis,reason)
    r.tick()
    return r.run.state()


@tool('access.request','申请具体材料','仅自动模式启用，由图外只读模型独立决策。',{'need':S,'purpose':S,'request_id':S},['need','purpose'])
def request(ctx,need,purpose,request_id=None):
    return runtime(ctx).request_files(ctx.actor,need,purpose,request_id)


@tool('access.decide','记录文件决策','仅当前项目文件处理模型调用。',{'request':S,'decision':S,'refs':A,'reason':S},['request','decision','refs','reason'])
def decide(ctx,request,decision,refs,reason):
    return runtime(ctx).decide_files(ctx.actor,request,decision,refs,reason)


@tool('access.revoke','撤销补充授权','仅操作者或主控撤销。',{'grant':S,'reason':S},['grant','reason'])
def revoke(ctx,grant,reason):
    r=runtime(ctx)
    r.access.revoke(r.identity(ctx.actor),grant,reason)
    return {'grant':grant,'status':'revoked'}


@tool('graph.request_helpers','申请整批图外助手','一次说明数量、分工和用途；追加说明新增原因。',
      {'count':I,'assignments':{'type':'array','items':{'type':'object','properties':{
          'role':S,'task':S,'name':S,'model':S,'thinking':{'type':'string','enum':list(THINKING_LEVELS)}},
          'required':['role','task'],'additionalProperties':False}},'purpose':S,'additional_reason':S,'request_id':S},['count','assignments','purpose'])
def request_helpers(ctx,count,assignments,purpose,additional_reason='',request_id=None):
    return runtime(ctx).request_helpers(ctx.actor,count,assignments,purpose,additional_reason,request_id)


@tool('graph.approve_helpers','主控批准助手批次','仅内部主控批准后按同一批次执行，不重复消费名额。',{'request':S},['request'])
def approve_helpers(ctx,request):
    return runtime(ctx).approve_helpers(ctx.actor,request)


@tool('graph.deny_helpers','主控拒绝助手批次','说明整批拒绝理由。',{'request':S,'reason':S},['request','reason'])
def deny_helpers(ctx,request,reason):
    r=runtime(ctx)
    r.helpers.deny(r.identity(ctx.actor),request,reason)
    return {'request':request,'status':'denied'}


@tool('graph.send','沿信息边发送消息','只能沿已定义边传递自己的正式引用，文本中的地址不授权。',{'to':S,'text':S,'refs':A},['to','text'])
def send(ctx,to,text,refs=None):
    return runtime(ctx).send(ctx.actor,to,text,refs or [])
