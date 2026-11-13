"""Graphs as data: write a graph to a file, build a graph from a file, and say what a file may refer to."""
from .catalog import Catalog, SpecError
from .load import dump, load, read, write

__all__ = ["Catalog", "SpecError", "dump", "load", "read", "write"]
