"""Replaceable task policy. Model scheduling, permissions and graph gates stay fixed."""
import copy
from . import roles
from .state import AgentError, SUB_MAX_STEPS


class Hooks:
    """No competition policy unless the application explicitly supplies one."""
    special_tools=False
    def before_spawn(self,manager,*,pid,actor,role,parent,task,expect,skills,tools,question,trusted):
        return task,expect,skills,tools,question

    def before_tool(self,manager,state,name):
        pass

    def scope_call(self,manager,state,name,args):
        return None

    def filter_result(self,name,result,scoped):
        return result

    def before_report(self,manager,state,args):
        return args

    def on_report(self,manager,state,args):
        pass

    def prompt_sections(self,manager,state,**options):
        return roles.generic_prompt(state.rec,manager._root(state.project_id),
            manager.name_of(state.project_id,state.rec.get('parent')) if state.rec.get('parent') else None,**options)

    def core_tools(self,manager,state,allowed):
        return [n for n in allowed if n.startswith(('fs.','graph.','access.')) or n=='code.run']

    def tool_enabled(self,name):
        return name.startswith(('fs.','graph.','access.')) or name=='code.run'

    def agent_spec(self,name,spec):
        if name!='agent.spawn':
            return spec
        out=copy.deepcopy(spec)
        out['parameters']['properties'].pop('question',None)
        return out

    def special_tool(self,manager,state,name,args):
        raise AgentError('当前运行时未注册此任务预设：'+name)

    def arranged(self,manager,pid,slot,role,fallback):
        return fallback

    def delivery(self,manager,state,args):
        return args

    def devil_gone(self,manager,pid,record):
        return True

    def confirms_later(self,manager,pid,state,name,params):
        return False

    def wrap_up_call(self,manager,state,name,args):
        return name in ('agent.report','graph.save_state','graph.submit')

    def budget_defaults(self,manager,role):
        return int(manager._defaults().get('sub_budget_tokens',400_000)),SUB_MAX_STEPS
