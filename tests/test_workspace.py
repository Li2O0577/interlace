"""The workspace: where each node works, what it may read and write, who wrote what; tools kept as files in a
pool; and the agent that works freely in its area."""
import json
import sys

import pytest

from interlacing import (DEFAULT, AgentNode, Catalog, Chain, ChatResult, Engine, FakeProvider, FileOf, FunctionNode, Port, SpecError,
                         ToolPool, WorkerNode, Workspace, dump, load, with_files)
from interlacing.cli import main
from interlacing.nodes import AreaError, ToolError
from interlacing.runs import Run

PYTHON = f'"{sys.executable}"'


def scripted(*steps):
    """A model that makes these tool calls one after another and then says the last step, which is text."""
    left = list(steps)

    def policy(system, messages, tools):
        step = left.pop(0)
        if isinstance(step, str):
            return ChatResult(text=step)
        return ChatResult(tool_calls=[{"id": f"c{len(left)}", "name": step[0], "args": step[1]}])
    model = FakeProvider(policy)
    model.name = "fake:scripted"
    return model


class Quiet:
    """A context for using a tool outside a run."""
    node, round = "test", 1

    def stopping(self):
        return False


def answers(node):
    """What the node's tools answered, in order."""
    return [message["content"] for message in node.messages if message["role"] == "tool"]


# ---- the layout and its bounds ---------------------------------------------------------------------------


def test_a_node_has_its_own_directory_and_a_block_is_one_more_layer(tmp_path):
    space = Workspace(tmp_path)
    assert space.area("solver").home == tmp_path / "solver"
    inner = space.area("writer", layer=["review"])
    assert inner.home == tmp_path / "review" / "writer"
    assert set(inner.writes) == {tmp_path / "review" / "writer", tmp_path / "shared", tmp_path / "review" / "shared"}
    assert inner.node == "review/writer"


def test_a_node_reads_everything_and_writes_only_where_it_may(tmp_path):
    space = Workspace(tmp_path)
    one, other = space.area("one"), space.area("other")
    one.write("one/notes.txt", "mine")
    one.write("shared/data.txt", "ours")
    assert other.read("one/notes.txt") == "mine"
    with pytest.raises(AreaError, match="outside what you may write"):
        other.write("one/notes.txt", "not mine")
    with pytest.raises(AreaError, match="outside the workspace"):
        one.write("../escaped.txt", "x")
    with pytest.raises(AreaError, match="not a path relative"):
        one.read(str(tmp_path / "one" / "notes.txt"))
    with pytest.raises(AreaError, match="own record"):
        one.read(".interlacing/written.jsonl")


def test_bounds_can_be_given(tmp_path):
    space = Workspace(tmp_path)
    narrow = space.area("narrow", reads=["shared"], writes=["narrow/out"])
    space.area("other").write("other/secret.txt", "x")
    with pytest.raises(AreaError, match="outside what you may read"):
        narrow.read("other/secret.txt")
    narrow.write("narrow/out/a.txt", "a")
    assert narrow.read("narrow/out/a.txt") == "a"        # what it may write it may read
    assert narrow.sends("other/secret.txt") and narrow.sends({"path": "narrow/out/a.txt"}) is None


def test_the_workspace_knows_who_wrote_what(tmp_path):
    space = Workspace(tmp_path)
    space.area("writer", layer=["review"]).write("review/shared/draft.md", "# draft")
    (tmp_path / "loose.bin").write_bytes(b"\x00\x01")
    files = {entry["path"]: entry for entry in space.files()}
    assert files["review/shared/draft.md"]["node"] == "review/writer" and files["review/shared/draft.md"]["size"] == 7
    assert files["loose.bin"]["node"] is None
    assert ".interlacing/written.jsonl" not in files


def test_edit_move_delete_and_files_that_are_not_text(tmp_path):
    area = Workspace(tmp_path).whole("w")
    area.write("a.txt", "one two three")
    area.edit("a.txt", "two", "2")
    with pytest.raises(AreaError, match="exactly once"):
        area.edit("a.txt", "missing", "x")
    area.move("a.txt", "kept/b.txt")
    assert area.read("kept/b.txt") == "one 2 three"
    (tmp_path / "p.png").write_bytes(b"\x89PNG\xff\xfe")
    assert "not text: it is image/png, 6 bytes" in area.read("p.png")
    assert area.listing(".").splitlines() == ["kept/", "p.png  6 bytes"]
    area.delete("kept")
    assert not (tmp_path / "kept").exists()


# ---- the worker ------------------------------------------------------------------------------------------


def test_a_worker_works_in_its_directory_and_sends_a_file_on(tmp_path):
    model = scripted(("write_file", {"path": "make.py", "content": "open('table.csv', 'w').write('a,b\\n1,2\\n')"}),
                     ("run_command", {"command": f"{PYTHON} make.py"}),
                     ("read_file", {"path": "table.csv"}),
                     "table.csv")
    worker = WorkerNode("worker", model, tmp_path, outputs=[Port("table", FileOf(".csv"))])
    seen = FunctionNode("seen", lambda x: x["size"], inputs=[Port("x", dict)], outputs=[Port("y", int)])
    graph = Chain(worker, seen, protocol="file").build(with_files(DEFAULT, tmp_path))
    result = Engine(graph, with_files(DEFAULT, tmp_path)).run({"in": "make the table"})
    assert result.last("out") == 8
    said = answers(worker)
    assert said[0].startswith("wrote make.py") and said[1].startswith("exit code 0") and said[2] == "a,b\n1,2\n"
    assert "Your own directory is its top" in worker.system and "run_command" in [spec["name"] for spec in worker._specs]
    wrote = {entry["path"]: entry["node"] for entry in Workspace(tmp_path).files()}
    assert wrote == {"make.py": "worker", "table.csv": "worker"}       # what the command left is the node's too


def test_a_worker_without_commands_has_the_file_tools_alone(tmp_path):
    worker = WorkerNode("worker", scripted("done"), tmp_path, commands=False)
    assert "run_command" not in [spec["name"] for spec in worker._specs]
    assert worker.config()["commands"] is False and worker.config()["tools"] == []


def test_a_command_that_overruns_is_ended_and_the_model_is_told(tmp_path):
    model = scripted(("run_command", {"command": f"{PYTHON} -c \"import time; time.sleep(30)\""}), "gave up")
    worker = WorkerNode("worker", model, tmp_path, command_timeout=0.5)
    Engine(Chain(worker).build(DEFAULT), DEFAULT).run({"in": "wait"})
    assert "ran longer than 0.5 seconds and was ended" in answers(worker)[0]


def test_a_file_outside_the_area_is_not_sent_and_the_model_puts_it_right(tmp_path):
    space = Workspace(tmp_path)
    space.area("other").write("other/secret.csv", "x")
    area = space.area("worker", reads=["worker"])
    model = scripted("other/secret.csv", ("write_file", {"path": "worker/mine.csv", "content": "1"}), "worker/mine.csv")
    worker = WorkerNode("worker", model, area, commands=False, outputs=[Port("table", FileOf(".csv"))])
    protocols = with_files(DEFAULT, tmp_path)
    seen = FunctionNode("seen", lambda x: x["path"], inputs=[Port("x", dict)], outputs=[Port("y", str)])
    result = Engine(Chain(worker, seen, protocol="file").build(protocols), protocols).run({"in": "send a table"})
    assert result.last("out") == "worker/mine.csv"
    assert any("outside what you may read" in str(m["content"]) for m in worker.messages if m["role"] == "user")


# ---- the tool pool ---------------------------------------------------------------------------------------


@pytest.fixture
def pool(tmp_path):
    tools = tmp_path / "tools"
    (tools / "charts").mkdir(parents=True)
    (tools / "add.py").write_text('def add(a: int, b: int = 1):\n    """Add two numbers."""\n    return a + b\n', encoding="utf-8")
    (tools / "where.py").write_text('import os\n\ndef run():\n    """Say where the tool runs."""\n'
                                    '    return [os.path.basename(os.getcwd()), os.path.basename(os.environ.get("INTERLACING_WORKSPACE", ""))]\n',
                                    encoding="utf-8")
    (tools / "broken.py").write_text('def broken(x: str):\n    raise ValueError("no " + x)\n', encoding="utf-8")
    (tools / "slow.py").write_text('import time\n\ndef slow():\n    time.sleep(30)\n', encoding="utf-8")
    (tools / "_helper.py").write_text("X = 1\n", encoding="utf-8")
    (tools / "charts" / "echo.json").write_text(json.dumps({
        "description": "Give back what was given.", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
        "command": [sys.executable, "-c", "import sys, json; print(json.load(sys.stdin)['text'].upper())"]}), encoding="utf-8")
    return tools


def test_a_python_file_is_a_tool_and_is_read_not_run(pool):
    tool = ToolPool(pool).tool("add.py")
    assert tool.spec() == {"name": "add", "description": "Add two numbers.",
                           "parameters": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                                          "required": ["a"]}}
    assert tool.file == "add.py" and tool.run({"a": 2, "b": 3}, Quiet()) == 5
    (pool / "loud.py").write_text('open("loaded", "w").write("x")\n\ndef loud():\n    return 1\n', encoding="utf-8")
    assert ToolPool(pool).tool("loud.py").name == "loud" and not (pool / "loaded").exists()
    (pool / "two.py").write_text("def a(): pass\ndef b(): pass\n", encoding="utf-8")
    with pytest.raises(ToolError, match="named two or run"):
        ToolPool(pool).tool("two.py")


def test_tools_of_a_directory_and_what_is_not_a_tool(pool):
    assert [tool.name for tool in ToolPool(pool).tools()] == ["add", "broken", "slow", "where"]
    assert [tool.name for tool in ToolPool(pool).tools("charts")] == ["echo"]
    with pytest.raises(ToolError, match="outside the tool pool"):
        ToolPool(pool).tool("../elsewhere.py")
    with pytest.raises(ToolError, match="no tool file"):
        ToolPool(pool).tool("missing.py")


def test_an_agent_uses_tools_from_the_pool(pool, tmp_path):
    tools = ToolPool(pool, timeout=1)
    model = scripted(("add", {"a": 2, "b": 40}), ("echo", {"text": "quiet"}), ("broken", {"x": "luck"}), ("slow", {}), "42")
    agent = AgentNode("agent", model, tools=[*tools.tools(), *tools.tools("charts")])
    assert not any(port.call for port in agent.outputs.values())           # tools of its own need no edges
    assert Engine(Chain(agent).build(DEFAULT), DEFAULT).run({"in": "add"}).last("out") == "42"
    said = answers(agent)
    assert said[0] == "42" and said[1].strip() == "QUIET"
    assert said[2] == "error: ValueError: no luck" and "ran longer than 1 seconds and was ended" in said[3]


# ---- written as data -------------------------------------------------------------------------------------


def catalog(tmp_path, pool, model, **more):
    return Catalog(models=lambda name: model, workspace=tmp_path / "files", tools=pool, **more)


def test_a_worker_and_its_tools_written_in_a_graph_file(pool, tmp_path):
    model = scripted(("where", {}), ("write_file", {"path": "solver/answer.txt", "content": "42"}), "done")
    description = {"method": "chain", "nodes": [
        {"id": "solver", "kind": "worker", "model": "fake:x", "commands": False, "tools": ["where.py", "charts"]}]}
    graph = load(description, catalog(tmp_path, pool, model))
    solver = graph.node("solver")
    assert [tool.name for tool in solver.more] == ["where", "echo"]
    assert Engine(graph, DEFAULT).run({"in": "go"}).last("out") == "done"
    assert json.loads(answers(solver)[0]) == ["solver", "files"]           # in the node's directory, told the top
    assert (tmp_path / "files" / "solver" / "answer.txt").read_text() == "42"
    again = load(dump(graph), catalog(tmp_path, pool, scripted("x")))
    assert again.digest == graph.digest
    assert dump(graph)["nodes"][0]["config"]["tools"][0]["file"] == "where.py"


def test_what_a_graph_file_may_not_do(pool, tmp_path):
    worker = {"id": "solver", "kind": "worker", "model": "fake:x"}
    with pytest.raises(SpecError, match="does not allow processes"):
        load({"method": "chain", "nodes": [worker]}, catalog(tmp_path, pool, scripted("x")))
    load({"method": "chain", "nodes": [worker]}, catalog(tmp_path, pool, scripted("x"), processes=True))
    with pytest.raises(SpecError, match="no tool pool"):
        load({"method": "chain", "nodes": [{**worker, "commands": False, "tools": ["add.py"]}]},
             Catalog(models=lambda name: scripted("x"), workspace=tmp_path / "files"))
    with pytest.raises(SpecError, match="outside the tool pool"):
        load({"method": "chain", "nodes": [{"id": "a", "kind": "agent", "model": "fake:x", "tools": ["../x.py"]}]},
             catalog(tmp_path, pool, scripted("x")))
    with pytest.raises(SpecError, match="leads outside the workspace"):
        load({"method": "chain", "nodes": [{**worker, "commands": False, "writes": ["../.."]}]}, catalog(tmp_path, pool, scripted("x")))


def test_a_block_is_one_more_layer_of_directories(pool, tmp_path):
    model = scripted(("write_file", {"path": "review/writer/draft.md", "content": "draft"}),
                     ("write_file", {"path": "review/shared/notes.md", "content": "notes"}),
                     ("write_file", {"path": "writer/draft.md", "content": "wrong layer"}), "done")
    inner = {"method": "chain", "nodes": [{"id": "writer", "kind": "worker", "model": "fake:x", "commands": False}]}
    description = {"method": "chain", "nodes": [{"id": "review", "kind": "block", "graph": inner}]}
    graph = load(description, catalog(tmp_path, pool, model))
    assert Engine(graph, DEFAULT).run({"in": "go"}).last("out") == "done"
    files = {entry["path"]: entry["node"] for entry in Workspace(tmp_path / "files").files()}
    assert files == {"review/writer/draft.md": "review/writer", "review/shared/notes.md": "review/writer"}
    assert load(dump(graph), catalog(tmp_path, pool, scripted("x"))).digest == graph.digest


def test_a_run_lists_its_files(pool, tmp_path, capsys):
    description = {"method": "chain", "nodes": [{"id": "solver", "kind": "worker", "model": "fake:x", "commands": False,
                                                 "tools": ["add.py"]}]}
    model = scripted(("add", {"a": 1}), ("write_file", {"path": "shared/sum.txt", "content": "2"}), "done")
    run = Run.create(tmp_path / "run", description, models=lambda name: model, tools=pool)
    assert run.start({"in": "go"})["status"] == "done"
    assert [(entry["path"], entry["node"], entry["round"]) for entry in run.files()] == [("shared/sum.txt", "solver", 1)]
    assert main(["files", "--dir", str(tmp_path / "run")]) == 0
    assert json.loads(capsys.readouterr().out)["files"][0]["path"] == "shared/sum.txt"
