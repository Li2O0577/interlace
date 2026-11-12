"""The engine: runs a compiled graph. It knows the abstractions of ``indeterminate.base`` and nothing else:
no particular kind of node, protocol or method."""
from .engine import Engine, Result, RunError

__all__ = ["Engine", "Result", "RunError"]
