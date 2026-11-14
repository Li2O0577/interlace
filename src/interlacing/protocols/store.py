"""A content store: keeps bytes under the hash of their content, so that an envelope can carry a reference."""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from ..base import Ref


class StoreError(LookupError):
    pass


class Store(ABC):
    def put(self, data: bytes, media: str = "") -> Ref:
        """Keep ``data`` and return its reference. Putting the same content twice keeps it once."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f"a store keeps bytes, not {type(data).__name__}")
        data = bytes(data)
        ref = Ref(hashlib.sha256(data).hexdigest(), len(data), media)
        self._write(ref.digest, data)
        return ref

    def get(self, ref: Ref) -> bytes:
        """The content a reference stands for, checked against its hash."""
        data = self._read(ref.digest)
        if data is None:
            raise StoreError(f"no content {ref.digest[:12]}")
        if hashlib.sha256(data).hexdigest() != ref.digest:
            raise StoreError(f"content {ref.digest[:12]} does not match its hash")
        return data

    @abstractmethod
    def _write(self, digest: str, data: bytes) -> None: ...

    @abstractmethod
    def _read(self, digest: str) -> bytes | None: ...


class MemoryStore(Store):
    def __init__(self):
        self._content: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def _write(self, digest: str, data: bytes) -> None:
        with self._lock:
            self._content.setdefault(digest, data)

    def _read(self, digest: str) -> bytes | None:
        return self._content.get(digest)

    def __len__(self) -> int:
        return len(self._content)


class DirectoryStore(Store):
    """One file per content, named by its hash, under ``root/ab/cdef...``. A file appears only when complete."""

    def __init__(self, root):
        self.root = Path(root)

    def _path(self, digest: str) -> Path:
        if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
            raise StoreError(f"not a content hash: {digest!r}")
        return self.root / digest[:2] / digest[2:]

    def _write(self, digest: str, data: bytes) -> None:
        path = self._path(digest)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".part-")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _read(self, digest: str) -> bytes | None:
        try:
            return self._path(digest).read_bytes()
        except FileNotFoundError:
            return None
