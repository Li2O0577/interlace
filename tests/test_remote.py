"""A node served in another process (here: another thread, over a real socket), and reached through RemoteNode."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from interlacing import Chain, Engine, FunctionNode, Port, RemoteNode, RunError, serve_node
from interlacing.base import NodeError
from interlacing.runs import Run
from interlacing.spec import Catalog, SpecError, dump, load

HERE = os.path.dirname(__file__)


def square(x):
    return x * x


def counter():
    seen = {"n": 0}

    def count(x):
        seen["n"] += 1
        return seen["n"]
    return FunctionNode("count", count, inputs=[Port("x")], outputs=[Port("y")])


@pytest.mark.parametrize("workers", [0, 4])
def test_a_served_function_runs_in_a_graph(workers):
    with serve_node(square) as server:
        far = RemoteNode("sq", server.address)
        assert list(far.inputs) == ["x"] and len(far.outputs) == 1
        result = Engine(Chain(far).build(), workers=workers).run({"in": 7})
        assert result.last("out") == 49


def test_a_served_node_keeps_what_it_holds_between_activations():
    with serve_node(counter()) as server:
        far = RemoteNode("count", server.address)
        engine_graph = Chain(far).build()
        assert Engine(engine_graph).run({"in": 0}).last("out") == 1
        assert Engine(engine_graph).run({"in": 0}).last("out") == 2


def test_emits_and_what_is_used_come_back():
    def several(ctx, x):
        for i in range(x):
            ctx.emit("y", i)
        ctx.use(dollars=1.5)

    node = FunctionNode("several", several, inputs=[Port("x")], outputs=[Port("y")], context=True)
    with serve_node(node) as server:
        result = Engine(Chain(RemoteNode("several", server.address)).build()).run({"in": 3})
        assert result.outputs["out"] == [0, 1, 2] and result.used == {"dollars": 1.5}
        with pytest.raises(RunError, match="budget"):
            Engine(Chain(RemoteNode("several", server.address)).build(), budget={"dollars": 1}).run({"in": 1})


def test_a_failure_on_the_far_side_fails_the_activation():
    def broken(x):
        raise ValueError("bad input")
    with serve_node(broken) as server:
        with pytest.raises(RunError, match="bad input"):
            Engine(Chain(RemoteNode("b", server.address)).build()).run({"in": 1})


def test_stopping_the_run_reaches_the_far_side():
    told = threading.Event()

    def slow(ctx, x):
        for _ in range(100):
            if ctx.stopping():
                told.set()
                return
            time.sleep(0.05)

    node = FunctionNode("slow", slow, inputs=[Port("x")], outputs=[Port("y")], context=True)
    with serve_node(node) as server:
        with pytest.raises(RunError):
            Engine(Chain(RemoteNode("slow", server.address)).build(), time_limit=0.3).run({"in": 1})
        assert told.wait(3)


def test_tokens():
    with pytest.raises(NodeError, match="needs a token"):
        serve_node(square, host="0.0.0.0")
    with serve_node(square, token="secret") as server:
        with pytest.raises(NodeError, match="wrong token"):
            RemoteNode("sq", server.address)
        with pytest.raises(NodeError, match="wrong token"):
            RemoteNode("sq", server.address, token="guess")
        assert Engine(Chain(RemoteNode("sq", server.address, token="secret")).build()).run({"in": 3}).last("out") == 9


def test_a_lost_server_fails_the_activation():
    server = serve_node(square)
    far = RemoteNode("sq", server.address)
    server.close()
    with pytest.raises(RunError, match="lost|cannot reach"):
        Engine(Chain(far).build()).run({"in": 2})
    with pytest.raises(NodeError, match="cannot reach"):
        RemoteNode("sq", server.address, connect_timeout=1)
    with pytest.raises(NodeError, match="tcp://host:port"):
        RemoteNode("sq", "nowhere")


def test_what_crosses_must_be_plain_data():
    with serve_node(square) as server:
        with pytest.raises(RunError, match="plain data"):
            Engine(Chain(RemoteNode("sq", server.address)).build()).run({"in": object()})


def test_a_graph_file_reaches_only_the_addresses_it_is_allowed(tmp_path, monkeypatch):
    with serve_node(square, token="t0k") as server:
        monkeypatch.setenv("SQ_TOKEN", "t0k")
        described = {"nodes": [{"id": "sq", "kind": "remote", "address": server.address, "token_env": "SQ_TOKEN"}],
                     "inputs": {"in": "sq.x"}, "outputs": {"out": "sq.out"}}
        with pytest.raises(SpecError, match="not among the addresses"):
            load(described)
        graph = load(described, Catalog(remotes=[server.address]))
        assert Engine(graph).run({"in": 5}).last("out") == 25
        written = json.dumps(dump(graph))
        assert "SQ_TOKEN" in written and "t0k" not in written      # the file names where the token is, never the token
        state = Run.create(tmp_path / "run", described, remotes=[server.address]).start({"in": 6})
        assert state["status"] == "done" and state["outputs"]["out"] == [36]


def test_the_command_line_serves_a_node(tmp_path):
    (tmp_path / "far_square.py").write_text("def square(x):\n    return x * x\n")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), os.environ.get("PYTHONPATH", "")])}
    command = [sys.executable, "-c", "import sys; from interlacing.cli import main; sys.exit(main())",
               "node-serve", "far_square:square"]
    process = subprocess.Popen(command, cwd=tmp_path, env=env, stdout=subprocess.PIPE, text=True)
    try:
        said = json.loads(process.stdout.readline())
        assert said["node"]["id"] == "square"
        assert Engine(Chain(RemoteNode("sq", said["address"])).build()).run({"in": 4}).last("out") == 16
    finally:
        process.terminate()
        process.wait(10)
