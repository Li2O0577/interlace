"""1ndeterminate: a task-neutral multi-agent orchestration kernel.

A plan graph approved by the operator, a deterministic scheduler, per-node file
boundaries, approvals, recoverable state and an audit log. The kernel constrains
boundaries (who may start, read, write, talk and release), never the reasoning.

Layers, bottom up (each imports only from below):

1. foundation      core (events, files, locks, sandbox), host adapters, model providers
2. entities        Plan / Block / Node / Edge; agent kinds (Agent > Coordinator > Lead, SubLead;
                   Worker; Reviewer; Keeper); the node protocol (NodeKind)
3. orchestration   scheduler, rounds, approvals, release, plan changes, recovery; the agent runtime
4. interface       Kernel, Operator, Extension, the stdio MCP server

Public names are importable from the package root. Standard library only; git is the one external program.
"""

__version__ = "0.1.0"

_PUBLIC = {
    # interface
    "Kernel": ("kernel", "Kernel"),
    "Operator": ("kernel", "Operator"),
    "HostAdapters": ("host", "HostAdapters"),
    "Extension": ("extensions", "Extension"),
    "TaskType": ("extensions", "TaskType"),
    "ExtensionError": ("extensions", "ExtensionError"),
    "Contract": ("extensions", "Contract"),
    # entities: the plan
    "Plan": ("plan", "Plan"),
    "Block": ("plan", "Block"),
    "Node": ("plan", "Node"),
    "Edge": ("plan", "Edge"),
    "GraphError": ("plan", "GraphError"),
    # entities: agent kinds
    "Agent": ("agents.kinds", "Agent"),
    "Coordinator": ("agents.kinds", "Coordinator"),
    "Lead": ("agents.kinds", "Lead"),
    "SubLead": ("agents.kinds", "SubLead"),
    "Worker": ("agents.kinds", "Worker"),
    "Reviewer": ("agents.kinds", "Reviewer"),
    "Keeper": ("agents.kinds", "Keeper"),
    "AgentKinds": ("agents.kinds", "AgentKinds"),
    # entities: the node protocol
    "NodeKind": ("graph.nodes", "NodeKind"),
    "NodeKinds": ("graph.nodes", "NodeKinds"),
    # foundation: models (FakeProvider for tests and simulations)
    "Provider": ("agents.providers", "Provider"),
    "FakeProvider": ("agents.providers", "FakeProvider"),
    "ChatResult": ("agents.providers", "ChatResult"),
}


def __getattr__(name):
    if name in _PUBLIC:
        import importlib
        module, attr = _PUBLIC[name]
        return getattr(importlib.import_module(f"{__name__}.{module}"), attr)
    raise AttributeError(name)


__all__ = ["__version__", *_PUBLIC]
