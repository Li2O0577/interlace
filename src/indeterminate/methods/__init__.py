"""The methods that come with the package: reusable ways of joining nodes into a graph."""
from .chain import Chain
from .fan import Fan
from .loop import Loop
from .tree import Star, Tree

__all__ = ["Chain", "Fan", "Loop", "Star", "Tree"]
