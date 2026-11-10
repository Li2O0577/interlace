"""mch-harness: a task-neutral multi-agent orchestration kernel.

A plan graph approved by the operator, a deterministic scheduler, per-node file
boundaries, approvals, recoverable state and an audit log. The kernel constrains
boundaries (who may start, read, write, talk and release), never the reasoning.

Public entry points: :class:`Kernel`, :class:`HostAdapters`, :class:`Extension`,
:class:`TaskType`. Standard library only; git is the one external program.
"""

__version__ = "0.2.0"

_PUBLIC = {
    "Kernel": ("kernel", "Kernel"),
    "HostAdapters": ("host", "HostAdapters"),
    "Extension": ("extensions", "Extension"),
    "TaskType": ("extensions", "TaskType"),
    "ExtensionError": ("extensions", "ExtensionError"),
}


def __getattr__(name):
    if name in _PUBLIC:
        import importlib
        module, attr = _PUBLIC[name]
        return getattr(importlib.import_module(f"{__name__}.{module}"), attr)
    raise AttributeError(name)


__all__ = ["__version__", *_PUBLIC]
