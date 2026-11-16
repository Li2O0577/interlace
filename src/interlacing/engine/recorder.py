"""What the run writes to its journal, and how a run is rebuilt from it.

The records themselves are described in ``journal.py``. This module knows what goes into each one and how
to put the run back as it was: counters, the nodes' own state and the envelopes still waiting.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping

from ..base import Envelope
from .journal import Journal, pack, pack_body, unpack_body
from .ledger import Ledger
from .result import Result
from .scheduler import Scheduler
from .wiring import Wiring


class Act:
    """What one activation did, for the journal."""

    __slots__ = ("node", "round", "took", "sent", "out", "limited", "calls", "snaps", "problems", "changes")

    def __init__(self, node: str, round: int, took: tuple) -> None:
        self.node, self.round, self.took = node, round, took
        self.sent: list[Envelope] = []
        self.out: list[tuple] = []
        self.limited: list[str] = []
        self.calls: list[str] = []
        self.snaps: dict[str, Any] = {}
        self.problems: list[dict] = []
        self.changes: list = []           # the changes to the graph it made, as plain data


def discard(journal: Path) -> None:
    """Remove the journal of a run that happened inside an activation, and those of the runs inside that."""
    shutil.rmtree(journal.with_name(journal.name + ".d"), ignore_errors=True)
    try:
        journal.unlink()
    except OSError:
        pass


class Recorder:
    """Writes one run's records to its journal."""

    __slots__ = ("journal",)

    def __init__(self, journal: Journal):
        self.journal = journal

    def read(self) -> list[dict]:
        return self.journal.read()

    def run(self, id: str, graph: str, sent: list[Envelope]) -> None:
        self.journal.write({"kind": "run", "id": id, "graph": graph, "sent": [pack(e) for e in sent]})

    def act(self, act: Act) -> None:
        self.journal.write({"kind": "act", "node": act.node, "round": act.round, "took": list(act.took),
                            "sent": [pack(e) for e in act.sent],
                            "out": [[name, pack_body(payload)] for name, payload in act.out],
                            "limited": act.limited, "calls": act.calls, "snaps": act.snaps,
                            **({"problems": act.problems} if act.problems else {}),
                            **({"changes": act.changes} if act.changes else {})})

    def use(self, node: str, round: int, amounts: Mapping[str, float]) -> None:
        self.journal.write({"kind": "use", "node": node, "round": round, "amounts": dict(amounts)})

    def once(self, node: str, round: int, name: str, value: Any) -> None:
        self.journal.write({"kind": "once", "node": node, "round": round, "name": name, "value": value})

    def change(self, under: str, graph: str, dropped: int) -> None:
        self.journal.write({"kind": "change", "from": under, "graph": graph, "dropped": dropped})

    def end(self) -> None:
        self.journal.write({"kind": "end"})
        try:
            os.rmdir(self.inner_dir)
        except OSError:
            pass                # never made, or something is still in it

    def close(self) -> None:
        self.journal.close()

    @property
    def inner_dir(self) -> Path:
        path = self.journal.path
        return path.with_name(path.name + ".d")

    def inner(self, node: str, round: int) -> Journal:
        """The journal of a run made inside activation ``round`` of ``node``."""
        return Journal(self.inner_dir / f"{node}-{round}.jsonl", sync=self.journal.sync)


def under(records: list[dict]) -> str | None:
    """The digest of the graph the last part of the run was written under."""
    return next((r["graph"] for r in reversed(records) if r["kind"] in ("run", "change")), None)


def restore(records: list[dict], wiring: Wiring, scheduler: Scheduler, ledger: Ledger, result: Result,
            limited: dict[str, None], change: Callable[[list], int]) -> tuple[str | None, set[str], int]:
    """Rebuild counters, node state and waiting envelopes from the journal. Returns the run's id, the
    envelopes already dealt with whose sender must run again, and how many waiting envelopes were dropped
    because their edge is not in this graph. ``change`` makes again a change an activation made to the graph."""
    states = wiring.states
    run_id: str | None = None
    sent: dict[str, dict[int, list]] = {}
    took: set[str] = set()
    snaps: dict[str, Any] = {}
    for record in records:
        kind = record["kind"]
        if kind == "run":
            run_id = run_id or record.get("id") or record["graph"][:16]
        elif kind == "use":
            ledger.add(record["amounts"])
        elif kind == "once":
            ledger.keep(record["node"], record["round"], record["name"], record["value"])
        if kind == "act":
            for described in record.get("changes", ()):
                change(described)
            took.update(record["took"])
            for name, body in record["out"]:
                if name in result.outputs:
                    result.outputs[name].append(unpack_body(body))
            for id in record["limited"]:
                if id in wiring.feeds:
                    limited[id] = None
            result.problems.extend(record.get("problems", ()))
            for id in (record["node"], *record["calls"]):
                state = states.get(id)
                if state is not None:
                    state.round += 1
                    state.started = True
                    result.activations += 1
            scheduler.envelopes += len(record["calls"])
            snaps.update(record["snaps"])
        if kind in ("run", "act"):
            for packed in record["sent"]:
                sent.setdefault(packed[0], {})[packed[1]] = packed
    dropped = 0
    orphans = took - {f"{feed}#{seq}" for feed, by_seq in sent.items() for seq in by_seq}
    for id, by_seq in sent.items():
        seqs = sorted(by_seq)
        waiting = [seq for seq in seqs if f"{id}#{seq}" not in took]
        feed = wiring.feeds.get(id)
        if feed is None:
            dropped += len(waiting)
            continue
        feed.sent = seqs[-1]
        scheduler.envelopes += len(seqs)
        if feed.passive:
            waiting = seqs[-1:]
        elif feed.latest:
            waiting = waiting[-1:] if waiting and waiting[-1] == seqs[-1] else []
        for seq in waiting:
            _, _, round, body, cause = by_seq[seq]
            body = unpack_body(body, feed.protocol, feed.port.port)
            scheduler.place(feed, Envelope(f"{id}#{seq}", id, seq, round, body, "", tuple(cause)))
    for id, snapshot in snaps.items():
        if id in states:
            states[id].node.restore(snapshot)
    return run_id, orphans, dropped
