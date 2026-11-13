"""``file``: what travels is the path of a file, relative to a directory both ends work in.

The file itself never moves. The sender writes it under the working directory and emits its path; the
envelope carries the path together with the file's size, its hash and what kind of file it is, taken at the
moment of sending. That is small plain data, so it is journaled like anything else, and the receiver can
tell whether the file it finds is the file that was sent.

A path is only worth sending if the file is really there, so sending checks, in this order:

1. the payload is a path: a string, or a mapping with ``path`` (anything else in the mapping travels along);
2. the path is relative and stays inside the working directory, also after links are followed;
3. it names a file, not a directory and not nothing;
4. the file is not empty (unless the protocol was made with ``empty=True``);
5. the file is what the sending port declares, when its schema is a ``FileOf``: its suffix, its kind, its size.

On arrival the file is looked at again: still there, same size, same hash. What happens when a check finds a
problem is not this protocol's business. It says what is wrong; the port decides (``Port``: fail, warn, or
give it back to the node to put right).
"""
from __future__ import annotations

import fnmatch
import hashlib
import mimetypes
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping

from ..base import Port, Protocol, ProtocolError, Protocols, Schema, SchemaError


class FileOf(Schema):
    """The schema of a port that carries file paths, with what the file must be.

    ``FileOf(".png", ".jpg")``                       one of these suffixes
    ``FileOf(media="image/*")``                      this kind of file, judged by its name
    ``FileOf(".csv", max_size=10_000_000)``          no larger than this many bytes
    ``FileOf(".tex", min_size=200)``                 no smaller

    The limits are enforced where the file can be seen: by the ``file`` protocol, when the path is sent.
    """

    def __init__(self, *suffixes: str, media: str | None = None, max_size: int | None = None, min_size: int | None = None):
        if not all(isinstance(s, str) and s.startswith(".") for s in suffixes):
            raise SchemaError("a suffix starts with a dot: '.png'")
        self.suffixes = tuple(s.lower() for s in suffixes)
        self.media, self.max_size, self.min_size = media, max_size, min_size

    def check(self, value) -> None:
        path = path_of(value)
        if self.suffixes and PurePosixPath(path).suffix.lower() not in self.suffixes:
            raise SchemaError(f"{path} is not a {' or '.join(self.suffixes)} file")
        if self.media and not fnmatch.fnmatch(media_of(path), self.media):
            raise SchemaError(f"{path} is {media_of(path) or 'of no known kind'}, not {self.media}")

    def accepts(self, other: Schema) -> bool:
        if not isinstance(other, FileOf):
            return False
        return ((not self.suffixes or (bool(other.suffixes) and set(other.suffixes) <= set(self.suffixes)))
                and (self.media is None or self.media == other.media
                     or (bool(other.suffixes) and all(fnmatch.fnmatch(media_of("x" + s), self.media) for s in other.suffixes)))
                and (self.max_size is None or (other.max_size is not None and other.max_size <= self.max_size))
                and (self.min_size is None or (other.min_size is not None and other.min_size >= self.min_size)))

    def describe(self):
        limits = {"suffixes": list(self.suffixes), "media": self.media, "max_size": self.max_size, "min_size": self.min_size}
        return {"file": {key: value for key, value in limits.items() if value}}


_OURS = ("size", "sha256", "unchecked")     # what the protocol itself says about a file: a sender cannot say it


def path_of(value: Any) -> str:
    """The path in a payload: the payload itself, or its ``path``."""
    path = value.get("path") if isinstance(value, Mapping) else value
    if not isinstance(path, str) or not path.strip():
        raise SchemaError(f"expected the path of a file (a string, or a mapping with 'path'), got {type(value).__name__}")
    return path


def media_of(path: str) -> str:
    return mimetypes.guess_type(path)[0] or ""


class File(Protocol):
    """``File(root)``: paths are relative to ``root``. The receiver gets
    ``{"path": ..., "size": ..., "sha256": ..., "media": ...}`` and whatever else the sender put in its mapping;
    ``protocol.locate(received)`` is the file's full path.

    ``empty=True`` lets empty files through. ``hash=False`` skips hashing (for very large files): a change of
    content that keeps the size then goes unseen.
    """

    name = "file"

    def __init__(self, root, *, empty: bool = False, hash: bool = True):
        self.root = Path(root).resolve()
        self.empty, self.hash = bool(empty), bool(hash)

    def check(self, source: Port, target: Port) -> None:
        if source.call:
            raise ProtocolError(f"{source.name} is a call port and needs a call edge")
        # the sender gives a path (text, or a mapping with it); the receiver gets a mapping
        for port, plain in ((source, ("any", "str", "dict", "str|dict", "dict|str")), (target, ("any", "dict"))):
            if not isinstance(port.schema, FileOf) and port.schema.describe() not in plain:
                raise ProtocolError(f"{port.name} carries {port.schema.describe()}, and a file edge carries the path of a file")
        if isinstance(source.schema, FileOf) and isinstance(target.schema, FileOf) and not target.schema.accepts(source.schema):
            raise ProtocolError(f"{target.name} does not accept the files {source.name} produces")

    # ---- sending ---------------------------------------------------------------------------------------

    def locate(self, value: Any) -> Path:
        """The full path of the file a payload names. Raises when the path leaves the working directory."""
        name = path_of(value)
        if PurePosixPath(name).is_absolute() or PureWindowsPath(name).is_absolute() or PureWindowsPath(name).drive:
            raise ProtocolError(f"{name} is an absolute path: give the path relative to the working directory")
        path = (self.root / name).resolve()
        if path != self.root and self.root not in path.parents:
            raise ProtocolError(f"{name} leads outside the working directory")
        return path

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _sha256(self, path: Path) -> str:
        hashed = hashlib.sha256()
        with open(path, "rb") as file:
            for block in iter(lambda: file.read(1 << 20), b""):
                hashed.update(block)
        return hashed.hexdigest()

    def encode(self, payload: Any, source: Port) -> dict:
        path = self.locate(payload)
        name = path_of(payload)
        if path.is_dir():
            raise ProtocolError(f"{name} is a directory, not a file")
        if not path.is_file():
            raise ProtocolError(f"there is no file {name} in the working directory{self._near(path)}")
        size = path.stat().st_size
        if not size and not self.empty:
            raise ProtocolError(f"{name} is empty")
        schema = source.schema
        if not isinstance(schema, FileOf):
            schema.check(payload)
        else:
            schema.check(name)
            if schema.max_size is not None and size > schema.max_size:
                raise ProtocolError(f"{name} is {size} bytes, more than the {schema.max_size} allowed")
            if schema.min_size is not None and size < schema.min_size:
                raise ProtocolError(f"{name} is {size} bytes, fewer than the {schema.min_size} required")
        said = {key: value for key, value in payload.items() if key not in _OURS} if isinstance(payload, Mapping) else {}
        body = {**said, "path": self._relative(path), "size": size, "media": media_of(name)}
        if self.hash:
            body["sha256"] = self._sha256(path)
        return body

    def _near(self, path: Path) -> str:
        """A hint for whoever must put a wrong path right: what is there instead."""
        folder = path.parent
        if not folder.is_dir():
            return f" (there is no directory {self._relative(folder)} either)" if folder != self.root else ""
        names = sorted(p.name for p in folder.iterdir())[:8]
        where = self._relative(folder) if folder != self.root else "the working directory"
        return f" ({where} holds: {', '.join(names)})" if names else f" ({where} is empty)"

    def encode_anyway(self, payload: Any, source: Port) -> dict:
        """The path as it was given, marked as unchecked: the receiver is told what it is getting."""
        try:
            name = path_of(payload)
        except SchemaError as exc:
            raise ProtocolError(str(exc)) from None
        return {**(payload if isinstance(payload, Mapping) else {}), "path": name, "media": media_of(name), "unchecked": True}

    # ---- receiving -------------------------------------------------------------------------------------

    def decode(self, body: Any, target: Port) -> dict:
        if not isinstance(body, Mapping) or not isinstance(body.get("path"), str):
            raise ProtocolError(f"a file arrives as a mapping with its path, not {type(body).__name__}")
        if body.get("unchecked"):
            return dict(body)
        name = body["path"]
        path = self.locate(body)
        if not path.is_file():
            raise ProtocolError(f"{name} was there when it was sent and is gone now")
        size = path.stat().st_size
        if size != body.get("size"):
            raise ProtocolError(f"{name} has changed since it was sent: it was {body.get('size')} bytes and is {size}")
        if "sha256" in body and self._sha256(path) != body["sha256"]:
            raise ProtocolError(f"{name} has changed since it was sent: its content is different")
        return dict(body)

    def decode_anyway(self, body: Any, target: Port) -> Any:
        return {**body, "unchecked": True} if isinstance(body, Mapping) else body


def with_files(protocols: Iterable[Protocol], root, **options) -> Protocols:
    """The given protocols, and ``file`` working in ``root``."""
    return Protocols([*(p for p in protocols if p.name != "file"), File(root, **options)])
