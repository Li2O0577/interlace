"""The transport: how an envelope's body gets from the sending side to the receiving side.

Inside one process nothing has to happen, and an engine without a transport hands bodies over as they are.
The interface is the place where another way of carrying them is put in: across processes or machines.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict
from typing import Any

from ..base import Ref, canonical


class Transport(ABC):
    @abstractmethod
    def carry(self, edge: str, body: Any) -> Any:
        """Take ``body`` across edge ``edge`` and return what arrives. Called by the sending node's thread."""


class Direct(Transport):
    """The body arrives as the same object."""

    def carry(self, edge: str, body: Any) -> Any:
        return body


class Wire(Transport):
    """The body is written out as text and read back, as it would be between two processes: what arrives is
    a copy, and anything that is not plain data (or a ``Ref``) is refused. ``sent`` counts the characters."""

    def __init__(self):
        self.sent = 0

    def carry(self, edge: str, body: Any) -> Any:
        is_ref = isinstance(body, Ref)
        try:
            text = canonical(asdict(body) if is_ref else body)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"edge {edge}: only plain data can be carried: {exc}") from None
        self.sent += len(text)
        value = json.loads(text)
        return Ref(**value) if is_ref else value
