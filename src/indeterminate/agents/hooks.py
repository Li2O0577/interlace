"""Replaceable task policy. Model scheduling, permissions and graph gates stay fixed."""
from . import roles
from .state import AgentError, SUB_MAX_STEPS


class Hooks:
    """The task policy an application plugs into the agent runtime. The defaults add no policy at all.

    ``before_spawn`` may keep (after checking) the extra attributes a caller asked to put on the new agent's record;
    by default every such attribute is dropped. ``agent_tools`` adds collaboration tools of the application (handled
    by ``special_tool``); like ``agent.spawn`` they create agents, so they follow its depth and graph rules.
    """
    def before_spawn(self,manager,*,pid,actor,role,parent,task,expect,tools,attrs,trusted):
        return task,expect,tools,{}

    def agent_tools(self)->dict:
        return {}

    def allow_groups(self)->list:
        """Sets of tools that are steps of the same work: "always allow" on one covers all of its set."""
        return []

    def always_ask(self,name)->bool:
        """Tools that need the operator's decision every time, whatever the autonomy setting."""
        return False

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
        return spec

    def special_tool(self,manager,state,name,args):
        raise AgentError('当前运行时未注册此任务预设：'+name)

    def wrap_up_call(self,manager,state,name,args):
        return name in ('agent.report','graph.save_state','graph.submit')

    def budget_defaults(self,manager,role):
        return int(manager._defaults().get('sub_budget_tokens',400_000)),SUB_MAX_STEPS
