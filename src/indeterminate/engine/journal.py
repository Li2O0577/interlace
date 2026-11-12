"""The journal: a file to which a run appends what has happened, so that it can be taken up again.

One line of JSON per record:

* ``run``: the graph's digest and the envelopes made from the graph's inputs;
* ``act``: one finished activation, written in one piece when it ends: which envelopes it took, which it
  sent, what reached the graph's outputs, the node's own snapshot;
* ``change``: the run continues under another graph;
* ``end``: nothing was left to do.

An activation that did not finish leaves no record: after a restart its inputs are still waiting and it runs
again. A line cut short by the process dying is dropped when the journal is next read.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..base import Envelope, Ref
from .stream import Stream


class JournalError(ValueError):
    pass


class Journal:
    """``sync=True`` also asks the operating system to put every record on the disk before going on, which
    survives a power cut and costs a disk write per activation."""

    def __init__(self, path, *, sync: bool = False):
        self.path, self.sync = Path(path), sync
        self._file = None

    def read(self) -> list[dict]:
        """Every complete record, oldest first."""
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return []
        records, good = [], 0
        for number, line in enumerate(data.splitlines(keepends=True), 1):
            try:
                if not line.endswith(b"\n"):
                    raise ValueError("cut short")
                records.append(json.loads(line))
            except ValueError as exc:
                if good + len(line) != len(data):
                    raise JournalError(f"{self.path}: line {number} is damaged: {exc}") from None
                self.close()
                with open(self.path, "r+b") as file:      # the last line was being written when the process died
                    file.truncate(good)
                break
            good += len(line)
        return records

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
        if self._file is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.path, "ab")
        self._file.write(line.encode("utf-8"))
        self._file.flush()
        if self.sync:
            os.fsync(self._file.fileno())

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def pack_body(body: Any) -> list:
    if isinstance(body, Ref):
        return ["r", body.digest, body.size, body.media]
    if isinstance(body, Stream):
        return ["s", [pack_body(chunk) for chunk in body._kept or ()]]
    return ["v", body]


def unpack_body(packed: list, protocol=None, port=None) -> Any:
    kind = packed[0]
    if kind == "r":
        return Ref(packed[1], packed[2], packed[3])
    if kind == "s":
        stream = Stream(protocol, port)
        for chunk in packed[1]:
            stream._put(unpack_body(chunk))
        stream._close()
        return stream
    return packed[1]


def pack(envelope: Envelope) -> list:
    return [envelope.edge, envelope.seq, envelope.round, pack_body(envelope.body), list(envelope.cause)]
