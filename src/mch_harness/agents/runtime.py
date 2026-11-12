"""Agent runtime: the lead agent, sub-agents and their chain of authority.

Authority: **user (human) > lead agent > sub-agents > their sub-agents**.

- The user can message, pause, resume, stop, re-budget and approve for any
  agent. Only the user approves dangerous tool calls. A pause set by the user
  can only be lifted by the user. Agents the user spawned (owner = human) can
  be messaged by their parent agent but not paused or stopped by it.
- An agent can only command its own descendants (message, pause, resume,
  stop); it talks to its parent through reports, questions and updates, and
  never directly to siblings. Questions from sub-agents go to their parent; the
  lead (or a top-level agent) asks the user.
- Inbox priority: user > parent > children/reports > system. User messages are
  labelled as the highest authority; when the user talks to a sub-agent
  directly, its parent is told, so the lead stays coordinated.
- Permissions only shrink down the tree: a child's tools are a subset of its
  parent's, and its token budget is carved out of the parent's remaining
  budget.
- Pausing or stopping an agent cascades to its subtree.

Each agent runs in its own thread: drain inbox → call the model → run the tool
calls (asking the user for approval where the project's autonomy level
requires it) → repeat until the turn ends. Transcripts live in
``agents/<id>/messages.jsonl``; long contexts are compacted into a summary.
"""

from __future__ import annotations

import threading
from typing import Callable

from .. import host as host_module, skills
from . import kinds as agent_kinds, providers
from .compaction import CompactionMixin
from .conversation import ConversationMixin
from .loop import LoopMixin
from .providers import ChatResult
from .records import AgentRecords
from .state import LEAD_ID, AgentError, AgentState, Approval, spent_tokens
from .specs import AGENT_TOOLS
from .store import StoreMixin
from .team import TeamMixin
from .tooling import ToolsMixin
from .watchdog import WatchdogMixin
from .hooks import Hooks


class AgentManager(StoreMixin, TeamMixin, LoopMixin, ToolsMixin, CompactionMixin, ConversationMixin, WatchdogMixin):
    """Runs every project's agents; the behaviour lives in the mixins (store, team, loop, tooling)."""

    def __init__(self, store, settings, events_for: Callable, run_tool: Callable, locks, *, hooks=None, host=None,
                 tools=None, contracts=None, roles=None, kinds=None):
        from ..tools import registry
        registry.load_generic()
        self.host = host if host is not None else host_module.default()
        self.hooks = hooks if hooks is not None else self.host.hooks if self.host.hooks is not None else Hooks()
        self.tools = tools if tools is not None else registry.TOOLS   # the tools this engine knows; a Kernel passes its own
        self.contracts = dict(contracts or {})    # extension tools a graph node may hold: name -> path contract
        # The agent kinds this engine knows: the process default plus kinds registered for this engine only.
        self.kinds = (kinds if kinds is not None else agent_kinds.DEFAULT).copy()
        for role, names in (roles or {}).items():
            self.kinds.register(agent_kinds.define(role, names))
        self.bridge_url = ""  # where CLI agents' MCP bridge reaches this workbench (set by the app)
        self.store = store
        self.settings = settings
        self.events_for = events_for
        self.run_tool = run_tool          # (project_id, name, params, actor) -> result
        self.locks = locks
        self.provider_factory: Callable[[str], providers.Provider] = self._default_provider
        self.records = AgentRecords(store, self.load)   # in-memory agents and their files (records.py)
        self.states = self.records.states
        self.approvals: dict[str, Approval] = {}
        self.loaded: set[str] = set()
        self.guard = threading.RLock()
        self.closing = False
        self._graph_ticket=object()
        self._graphs={}
        self.skills = skills.store_for(settings.path.parent, self.host.skills_dir)   # experience manuals agents can read

    def graph(self,pid):
        from ..graph.runtime import GraphRuntime
        with self.guard:
            if pid not in self._graphs:
                self._graphs[pid]=GraphRuntime(self,pid)
            return self._graphs[pid]

    def known_role(self, role) -> bool:
        return role in self.kinds

    def role_tools(self, role) -> list[str]:
        return self.kinds.tools(role)

    def graph_tools_for(self,role,requested=None):
        defaults=set(self._expand(self.role_tools(role)))
        if requested is not None and not set(self._expand(requested))<=defaults:
            raise AgentError('节点工具必须是角色默认权限的子集')
        selected=defaults if requested is None else set(self._expand(requested))
        mapped={'fs.read','fs.list','fs.search','fs.write','fs.edit','code.run'} | set(self.contracts)
        coordination={'graph.block_status','graph.request_review'} if self.kind(role).coordinates else set()
        return sorted(selected & mapped | {'graph.save_state','graph.submit','graph.send','graph.request_helpers','access.request'} | coordination)


__all__ = ["AgentManager", "AgentError", "AgentState", "Approval", "LEAD_ID", "AGENT_TOOLS", "ChatResult", "spent_tokens"]
