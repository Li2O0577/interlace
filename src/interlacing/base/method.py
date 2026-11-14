"""The method: a reusable way of joining nodes, a chain, a loop, a tree and so on.

A method takes the parts it is given and returns a graph. How the result runs is already in that graph: each
node says when it fires, each back edge says how many rounds it may open. A graph can be wrapped as one node,
so methods nest.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from .graph import Graph
from .protocol import Protocols


class Method(ABC):
    name: ClassVar[str] = ""

    @abstractmethod
    def build(self, protocols: Protocols) -> Graph:
        """The graph this method makes of its parts."""
