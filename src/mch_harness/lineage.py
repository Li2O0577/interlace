"""File lineage: which file was made from which, so a change shows exactly what is now out of date.

How people actually model: a script turns data into results, results into figures and numbers in the paper.
Later something turns out wrong, and they either fix the file in place or write a new one next to it. Either
way the question is the same: *what has to be redone?* The stage tree cannot answer it (it only knows the
order of stages), and people answer it from memory, which is how stale figures end up in papers.

So every time the team produces a file we write one line to ``state/lineage.jsonl``::

    {"ts", "actor", "via": "code.run" | "fs.write" | …, "step": [active stage leaves],
     "outputs": {path: hash}, "inputs": {path: hash}, "strong": bool, "replaces": [old paths]}

* ``code.run`` gives strong edges: the script and the project files it names → the files that changed while it ran.
* ``fs.write`` / ``fs.edit`` give weak edges: a document → the files its author read just before writing it.
* ``exp.run`` gives strong edges too: the scripts and files the command names, hashed when the run was created →
  the run's metrics (``runs/<id>/metrics.jsonl``) and outputs (``runs/<id>/outputs/…``), recorded when it ends.
  Paper numbers imported from a run (source ``runs/<id>``) are flagged when that run's inputs change.
* ``replaces`` (fs.write) or a new version of a versioned name (``v002`` next to ``v001``, ``_v2``) says a new file
  takes over from an old one.

Nothing has to be declared up front and nothing is stored twice: whether a file is out of date is computed from
content hashes when asked. Edit in place → the hash of an input differs from the one recorded when the output was
made. New file instead → the old one is marked as replaced, and everything still made from it is flagged.
Out-of-date files are traced on downstream (results → figures → paper numbers).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

LEDGER = "state/lineage.jsonl"
SKIP_DIRS = (".git/", "state/", "agents/", "runs/", "paper/.build/")
SKIP_PARTS = {"__pycache__", ".ipynb_checkpoints", ".pytest_cache"}
MAX_HASH = 64_000_000
MAX_INPUTS = 24
_reads: dict[tuple[str, str], dict[str, str]] = {}     # (project root, actor) → files read since the last write
_lock = threading.Lock()


# ------------------------------------------------------------------ hashing and scanning

_hashes: dict[str, tuple[tuple, str]] = {}    # path → ((mtime_ns, size), hash): unchanged files are not re-read
_entries: dict[str, tuple[tuple, list]] = {}  # ledger path → (signature, parsed entries)


def file_hash(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    if st.st_size > MAX_HASH:   # huge data files: size + mtime is enough to notice a change
        return f"s{st.st_size}-{int(st.st_mtime)}"
    key, sig = str(path), (st.st_mtime_ns, st.st_size)
    hit = _hashes.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()[:12]
    if len(_hashes) > 20_000:
        _hashes.clear()
    _hashes[key] = (sig, digest)
    return digest


_RUN_PRODUCT = re.compile(r"^runs/[\w-]+/(?:metrics\.jsonl|outputs/.+)$")


def tracked(rel: str) -> bool:
    if rel.startswith("runs/"):
        return bool(_RUN_PRODUCT.match(rel)) and not SKIP_PARTS & set(rel.split("/"))   # what experiments produce
    return bool(rel) and not rel.startswith(SKIP_DIRS) and not SKIP_PARTS & set(rel.split("/")) \
        and not rel.endswith((".pyc", ".tmp", ".gitkeep"))


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """mtime and size of every tracked file: compared before/after a run to see what it wrote."""
    out = {}
    root = root.resolve()
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if (rel + "/").startswith(SKIP_DIRS) or not tracked(rel):
            continue
        try:
            if p.is_file():
                st = p.stat()
                out[rel] = (st.st_mtime_ns, st.st_size)
        except OSError:
            continue
    return out


def changed(before: dict, after: dict) -> list[str]:
    return sorted(rel for rel, sig in after.items() if before.get(rel) != sig)


_PATH_RX = re.compile(r"""["']([\w./\-一-鿿]+\.[A-Za-z0-9]{1,6})["']""")
_IMPORT_RX = re.compile(r"^\s*(?:from|import)\s+([\w.]+)", re.M)


def referenced(root: Path, text: str, base: Path | None = None) -> list[str]:
    """Project files a script names: quoted paths that exist and modules it imports from the project."""
    root = root.resolve()
    found = []
    for m in _PATH_RX.finditer(text):
        for cand in ((root / m.group(1)), (base / m.group(1)) if base else None):
            if cand is not None and cand.is_file():
                try:
                    found.append(cand.resolve().relative_to(root).as_posix())
                except ValueError:
                    pass
                break
    for m in _IMPORT_RX.finditer(text):
        mod = m.group(1).replace(".", "/") + ".py"
        for cand in ((base / mod) if base else None, root / mod):
            if cand is not None and cand.is_file():
                found.append(cand.resolve().relative_to(root).as_posix())
                break
    return [f for f in dict.fromkeys(found) if tracked(f)]


def script_inputs(root: Path, code: str = "", command: str = "", workdir: str = "") -> list[str]:
    """What a code.run or an experiment reads: the scripts it runs, the files they name and the project modules
    they import (paths in the command are taken relative to its working directory, then to the project)."""
    root = root.resolve()
    scripts = []
    if command:
        for tok in re.split(r"\s+", command):
            tok = tok.strip("\"'")
            if not tok or tok.startswith("-"):
                continue
            for base in ((root / workdir) if workdir and workdir != "." else None, root):
                if base is None:
                    continue
                p = (base / tok).resolve()
                if p.is_file() and root in p.parents:
                    scripts.append(p.relative_to(root).as_posix())
                    break
        if workdir and workdir != ".":
            scripts += referenced(root, command, root / workdir)
        else:
            scripts += referenced(root, command)
    inputs = list(scripts)
    if code:
        inputs += referenced(root, code)
    for rel in list(inputs):
        if rel.endswith(".py"):
            try:
                inputs += referenced(root, (root / rel).read_text(encoding="utf-8", errors="replace"), (root / rel).parent)
            except OSError:
                pass
    return [p for p in dict.fromkeys(inputs) if tracked(p)][:MAX_INPUTS]


# ------------------------------------------------------------------ recording

def note_read(root: Path, actor: str, rel: str) -> None:
    if tracked(rel):
        with _lock:
            reads = _reads.setdefault((str(root.resolve()), actor), {})
            reads[rel] = file_hash(root / rel)
            if len(reads) > 60:
                reads.pop(next(iter(reads)))


def _take_reads(root: Path, actor: str, exclude: set[str]) -> dict[str, str]:
    with _lock:
        reads = _reads.pop((str(root.resolve()), actor), {})
    return {k: v for k, v in list(reads.items())[-MAX_INPUTS:] if k not in exclude}


def _active_steps(root: Path) -> list[str]:
    try:
        nodes = json.loads((root / "state" / "workflow_state.json").read_text(encoding="utf-8"))["nodes"]
    except (OSError, ValueError, KeyError):
        return []
    out = []

    def walk(ns):
        for n in ns:
            if "children" in n:
                walk(n["children"])
            elif n.get("status") == "active":
                out.append(n["id"])
    walk(nodes)
    return out[:6]


_VERSION_RX = re.compile(r"^(.*?)(?:[_\-]?v(\d+)|[_\-](\d+))$", re.I)


def previous_version(root: Path, rel: str) -> str | None:
    """``assumptions/v002.md`` → ``assumptions/v001.md``; ``solve_v3.py`` → ``solve_v2.py`` / ``solve.py``."""
    p = Path(rel)
    m = _VERSION_RX.match(p.stem)
    if not m:
        return None
    stem, num = m.group(1), int(m.group(2) or m.group(3))
    width = len(m.group(2) or m.group(3))
    tag = "v" if m.group(2) else ""
    for n in range(num - 1, max(0, num - 21), -1):
        for cand in (f"{stem}{sep}{tag}{n:0{width}d}{p.suffix}" for sep in ("", "_", "-")):
            if (root / p.parent / cand).is_file():
                return (p.parent / cand).as_posix()
    if stem and (root / p.parent / f"{stem.rstrip('_-')}{p.suffix}").is_file():
        return (p.parent / f"{stem.rstrip('_-')}{p.suffix}").as_posix()
    return None


def record(root: Path, actor: str, via: str, outputs: list[str], inputs: list[str] | dict | None = None,
           strong: bool = False, replaces: list[str] | None = None, note: str = "", step: list[str] | None = None) -> dict | None:
    root = root.resolve()
    outs = {o: file_hash(root / o) for o in outputs if tracked(o) and (root / o).is_file()}
    if not outs:
        return None
    if isinstance(inputs, dict):
        ins = {k: v for k, v in inputs.items() if k not in outs}
    else:
        ins = {i: file_hash(root / i) for i in inputs or [] if i not in outs and (root / i).is_file()}
    reps = [r for r in replaces or [] if r not in outs]
    for o in outs:
        prev = previous_version(root, o)
        if prev and prev not in reps and prev not in outs and not _was_output_before(root, o):
            reps.append(prev)
    entry = {"ts": round(time.time(), 3), "actor": actor, "via": via, "step": step or _active_steps(root), "outputs": outs,
             "inputs": ins, "strong": strong}
    if reps:
        entry["replaces"] = reps
    if note:
        entry["note"] = note[:200]
    path = root / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def record_write(root: Path, actor: str, via: str, rel: str, replaces: list[str] | None = None) -> dict | None:
    """A document written by hand or by an agent: made from what its author read just before."""
    return record(root, actor, via, [rel], _take_reads(root, actor, {rel}), strong=False, replaces=replaces)


def run_inputs(root: Path, command: str, workdir: str = "") -> dict[str, str]:
    """The files an experiment reads, hashed when it is created (the code it runs is that commit's)."""
    root = root.resolve()
    return {p: file_hash(root / p) for p in script_inputs(root, command=command, workdir=workdir)}


def record_run(root: Path, run: dict) -> dict | None:
    """An experiment that ended: its metrics and outputs were made from the files it read."""
    root = root.resolve()
    base = f"runs/{run['id']}"
    outputs = [f"{base}/metrics.jsonl"] + [f"{base}/outputs/{o['path']}" for o in run.get("outputs") or []
                                           if isinstance(o, dict) and o.get("path")]
    inputs = run.get("lineage_inputs")
    if not isinstance(inputs, dict):
        inputs = run_inputs(root, run.get("command") or "", run.get("workdir") or "")
    node = run.get("node")
    return record(root, run.get("actor") or "system", "exp.run", outputs[:200], inputs, strong=True,
                  note=f"{run['id']} {run.get('name', '')}", step=[node] if node else None)


def _was_output_before(root: Path, rel: str) -> bool:
    return any(rel in e["outputs"] for e in entries(root))


def entries(root: Path) -> list[dict]:
    """The ledger, parsed once per version of the file (it is read on every write and every run)."""
    path = root / LEDGER
    try:
        st = path.stat()
    except OSError:
        return []
    sig = (st.st_mtime_ns, st.st_size)
    hit = _entries.get(str(path))
    if hit and hit[0] == sig:
        return hit[1]
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    _entries[str(path)] = (sig, out)
    return out


# ------------------------------------------------------------------ queries

def _latest(es: list[dict]) -> dict[str, dict]:
    """The record that last produced each file."""
    made: dict[str, dict] = {}
    for e in es:
        for o in e["outputs"]:
            made[o] = e
    return made


def _replaced(es: list[dict], root: Path) -> dict[str, str]:
    """old file → the file that took over from it (only while the new one exists)."""
    out = {}
    for e in es:
        for old in e.get("replaces", []):
            new = next(iter(e["outputs"]))
            if (root / new).exists():
                out[old] = new
    return out


def status(root: Path) -> dict:
    """Every file that is out of date, with the reason, traced downstream; plus paper numbers built on them."""
    root = root.resolve()
    es = entries(root)
    made = _latest(es)
    replaced = _replaced(es, root)
    now: dict[str, str] = {}

    def current(p: str) -> str:
        if p not in now:
            now[p] = file_hash(root / p) if (root / p).exists() else ""
        return now[p]

    stale: dict[str, dict] = {}
    for out, e in made.items():
        if not (root / out).exists() or current(out) != e["outputs"][out]:
            continue   # changed by hand since: whoever changed it owns it now
        reasons = []
        for inp, h in e["inputs"].items():
            cur = current(inp)
            if not cur:
                reasons.append({"input": inp, "why": "deleted"})
            elif cur != h:
                reasons.append({"input": inp, "why": "changed"})
            if inp in replaced and replaced[inp] != out:
                reasons.append({"input": inp, "why": "replaced", "by": replaced[inp]})
        if reasons:
            stale[out] = {"path": out, "reasons": reasons, "made": e["ts"], "via": e["via"], "actor": e["actor"],
                          "step": e.get("step", []), "strong": e.get("strong", False)}
    # downstream: anything made from an out-of-date file is out of date as well
    queue = list(stale)
    while queue:
        src = queue.pop()
        for out, e in made.items():
            if src in e["inputs"] and out not in stale and (root / out).exists() and current(out) == e["outputs"][out]:
                stale[out] = {"path": out, "reasons": [{"input": src, "why": "upstream"}], "made": e["ts"], "via": e["via"],
                              "actor": e["actor"], "step": e.get("step", []), "strong": e.get("strong", False)}
                queue.append(out)
    numbers = []
    try:
        results = json.loads((root / "paper" / "results.json").read_text(encoding="utf-8"))
        items = results.items() if isinstance(results, dict) else ((r.get("key"), r) for r in results)
        for key, r in items:
            src = str((r or {}).get("source") or "")
            run = re.match(r"runs/([\w-]+)", src)
            hit = next((p for p in list(stale) + list(replaced) if p and (src == p or src.endswith(p)
                        or (run and p.startswith(f"runs/{run.group(1)}/")))), None)
            if hit:
                numbers.append({"key": key, "source": src, "why": "replaced" if hit in replaced else "stale"})
    except (OSError, ValueError, AttributeError):
        pass
    return {"stale": sorted(stale.values(), key=lambda s: s["path"]),
            "replaced": [{"old": o, "new": n, "still_used_by": sorted(out for out, e in made.items() if o in e["inputs"])}
                         for o, n in replaced.items()],
            "paper_numbers": numbers}


def trace(root: Path, rel: str, depth: int = 4) -> dict:
    """Where a file came from (upstream) and what was made from it (downstream)."""
    root = root.resolve()
    es = entries(root)
    made = _latest(es)

    def up(p, d, seen):
        e = made.get(p)
        if not e or d == 0 or p in seen:
            return {"path": p}
        seen = seen | {p}
        return {"path": p, "via": e["via"], "actor": e["actor"], "ts": e["ts"], "step": e.get("step", []),
                "inputs": [up(i, d - 1, seen) for i in e["inputs"]]}

    def down(p, d, seen):
        if d == 0 or p in seen:
            return []
        seen = seen | {p}
        return [{"path": out, "via": e["via"], "made_from": [i for i in e["inputs"]],
                 "then": down(out, d - 1, seen)} for out, e in made.items() if p in e["inputs"]]
    replaced = _replaced(es, root)
    return {"path": rel, "upstream": up(rel, depth, frozenset()), "downstream": down(rel, depth, frozenset()),
            **({"replaced_by": replaced[rel]} if rel in replaced else {}),
            **({"replaces": [o for o, n in replaced.items() if n == rel]} if rel in replaced.values() else {})}


def consumed(root: Path, paths: list[str]) -> bool:
    """Whether anything recorded was made from one of ``paths`` (only then can changing them make something stale)."""
    ps = set(paths)
    return any(ps & set(e["inputs"]) for e in entries(root))


def stale_paths(root: Path) -> set[str]:
    return {s["path"] for s in status(root)["stale"]}


def newly_stale_steps(root: Path, before: set[str]) -> tuple[list[dict], dict[str, list[str]]]:
    """Files that became out of date since ``before`` and the stage leaves that produced them by running code."""
    fresh = [s for s in status(root)["stale"] if s["path"] not in before]
    steps: dict[str, list[str]] = {}
    for s in fresh:
        if s["strong"]:
            for leaf in s["step"]:
                steps.setdefault(leaf, []).append(s["path"])
    return fresh, steps
