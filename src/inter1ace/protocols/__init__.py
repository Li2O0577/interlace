"""The protocols that come with the package.

``DEFAULT`` is a registry of those that need nothing else; ``standard(store)`` makes a new registry that also
has ``artifact``, which keeps content in the store it is given (in memory when none is given), and, when it
is given ``files``, ``file``, which carries the paths of files in that directory.
"""
from ..base import Protocols
from .artifact import Artifact
from .file import File, FileOf, with_files
from .standard import Call, Latest, Signal, State, Stream
from .store import DirectoryStore, MemoryStore, Store, StoreError
from .value import Value


def standard(store: Store | None = None, files=None) -> Protocols:
    return Protocols([Value(), Latest(), State(), Signal(), Stream(), Call(),
                      Artifact(store if store is not None else MemoryStore()),
                      *([File(files)] if files is not None else [])])


DEFAULT = Protocols([Value(), Latest(), State(), Signal(), Stream(), Call()])

__all__ = ["DEFAULT", "Artifact", "Call", "DirectoryStore", "File", "FileOf", "Latest", "MemoryStore", "Signal", "State", "Store",
           "StoreError", "Stream", "Value", "standard", "with_files"]
