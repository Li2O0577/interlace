"""A worker: an agent that works freely in its own part of a workspace, with tools taken from a tool pool.

    python examples/graphs/worker_in_workspace.py

The workspace is laid out by node, and a block is one more layer: ``study/writer`` is the directory of the
writer inside the block ``study``, and ``study/shared`` what the nodes of that block share. The writer
writes a file, runs a command, uses a tool from the pool (``pool/word_count.py``, a plain Python file), and
sends its file on by path. The workspace remembers which node wrote each file.

The model here is a stand-in that follows a script; a real one decides for itself what to do.
"""
import json
import sys
import tempfile
from pathlib import Path

from interlacing import Catalog, ChatResult, Engine, FakeProvider, Workspace, load

POOL = Path(__file__).resolve().parent / "pool"


def scripted(*steps):
    left = list(steps)

    def policy(system, messages, tools):
        step = left.pop(0)
        return ChatResult(text=step) if isinstance(step, str) else ChatResult(tool_calls=[{"id": "1", "name": step[0], "args": step[1]}])
    return FakeProvider(policy)


writer = scripted(
    ("write_file", {"path": "study/writer/notes.md", "content": "Tides rise and fall twice a day.\n"}),
    ("run_command", {"command": f'"{sys.executable}" -c "print(open(\'notes.md\').read().upper())"'}),
    ("word_count", {"path": "study/writer/notes.md"}),
    ("write_file", {"path": "study/shared/summary.md", "content": "Seven words on tides.\n"}),
    "study/shared/summary.md",
)

block = {"method": "chain", "nodes": [
    {"id": "writer", "kind": "worker", "model": "writer", "tools": ["word_count.py"],
     "outputs": [{"name": "summary", "schema": {"file": {"suffixes": [".md"]}}}]}]}
graph_file = {"method": "chain", "protocol": "file", "nodes": [
    {"id": "study", "kind": "block", "graph": block},
    {"id": "received", "kind": "pass", "inputs": [{"name": "x", "schema": "dict"}]}]}

with tempfile.TemporaryDirectory() as top:
    catalog = Catalog(models=lambda name: writer, workspace=top, tools=POOL, processes=True)
    graph = load(graph_file, catalog)
    result = Engine(graph, catalog.protocols).run({"in": "write about tides"})
    sent = result.last("out")
    print("sent on:", sent["path"], sent["size"], "bytes")
    for message in graph.node("study").graph.node("writer").messages:
        if message["role"] == "tool":
            print(f"  {message['name']}: {message['content'].strip()[:60]}")
    print(json.dumps([(f["path"], f["node"]) for f in Workspace(top).files()]))
    assert sent["path"] == "study/shared/summary.md"
