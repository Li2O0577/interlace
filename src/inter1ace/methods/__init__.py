"""The methods that come with the package: reusable ways of joining nodes into a graph."""
from .chain import Chain
from .fan import Fan
from .loop import Loop
from .pool import Pool
from .tree import Governed, Star, Tree

__all__ = ["Chain", "Fan", "Governed", "Loop", "Pool", "Star", "Tree"]
