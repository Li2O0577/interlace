"""Graphs as data: write a graph to a file, build a graph from a file, and say what a file may refer to."""
from .catalog import Catalog, SpecError
from .load import block, dump, load, load_node, read, write
from .spawn import SpawnNode

__all__ = ["Catalog", "SpawnNode", "SpecError", "block", "dump", "load", "load_node", "read", "write"]
