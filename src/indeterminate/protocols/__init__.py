"""The protocols that come with the package. ``DEFAULT`` is a registry holding all of them; copy it to add more."""
from ..base import Protocols
from .value import Value

DEFAULT = Protocols([Value()])

__all__ = ["DEFAULT", "Value"]
