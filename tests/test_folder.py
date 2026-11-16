"""A whole folder as one node."""
import json

import pytest

from interlacing import Chain, Engine, FolderNode, NodeError, RunError, run
from interlacing.runs import Run
from interlacing.spec import Catalog, SpecError, dump, load


def app(root, name, code, manifest):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "main.py").write_text(code, encoding="utf-8")
    (folder / "node.json").write_text(json.dumps({"command": ["python", "main.py"], **manifest}), encoding="utf-8")
    return folder


SOLVER = """
import json, os, sys
given = json.load(sys.stdin)
print("working on it")                        # standard output is free: the answer goes to the file
with open(os.environ["INTERLACING_OUT"], "w") as out:
    json.dump({"answer": sum(given["numbers"]) * given.get("scale", 1), "where": os.path.basename(os.getcwd())}, out)
"""

PRINTER = """
import json, os, sys
given = json.load(open(os.environ["INTERLACING_IN"]))
print(json.dumps({"out": given["x"] + 1}))
"""


def test_a_folder_is_a_node_with_the_ports_its_manifest_names(tmp_path):
    folder = app(tmp_path, "my-solver", SOLVER, {"inputs": [{"name": "numbers", "type": "list"},
                                                            {"name": "scale", "required": False, "type": "int"}],
                                                 "outputs": ["answer", "where"]})
    node = FolderNode(folder)
    assert node.id == "my_solver" and list(node.inputs) == ["numbers", "scale"] and not node.inputs["scale"].required
    result = run(node, {"numbers": [1, 2, 3], "scale": 2})
    assert result.last("answer") == 12 and result.last("where") == "my-solver"
    with pytest.raises(RunError, match="expected list"):
        run(node, {"numbers": 3})


def test_an_answer_on_standard_output_and_a_folder_in_a_chain(tmp_path):
    first = FolderNode(app(tmp_path, "one", PRINTER, {"inputs": ["x"], "outputs": ["out"]}))
    second = FolderNode(app(tmp_path, "two", PRINTER, {"inputs": ["x"], "outputs": ["out"]}))

    def double(x: int) -> int:
        return x * 2
    assert run(Chain(first, double, second), {"in": 1}).last("out") == 5


def test_a_text_application(tmp_path):
    node = FolderNode(app(tmp_path, "shout", "import sys\nsys.stdout.write(sys.stdin.read().upper())",
                          {"format": "text", "inputs": ["text"], "outputs": ["loud"]}))
    assert run(node, {"text": "quiet"}).last("loud") == "QUIET"
    with pytest.raises(NodeError, match="one output"):
        FolderNode(app(tmp_path, "bad", "", {"format": "text", "inputs": ["a", "b"], "outputs": ["c"]}))


def test_what_goes_wrong_in_an_application(tmp_path):
    failing = FolderNode(app(tmp_path, "failing", "import sys\nsys.stderr.write('no data')\nsys.exit(3)",
                             {"inputs": ["x"], "outputs": ["y"]}))
    with pytest.raises(RunError, match="exited with 3: no data"):
        run(failing, {"x": 1})
    wrong = FolderNode(app(tmp_path, "wrong", "print('{\"z\": 1}')", {"inputs": ["x"], "outputs": ["y"]}))
    with pytest.raises(RunError, match=r"answered on \['z'\]"):
        run(wrong, {"x": 1})
    slow = FolderNode(app(tmp_path, "slow", "import time\ntime.sleep(30)", {"inputs": ["x"], "outputs": ["y"], "timeout": 0.5}))
    with pytest.raises(RunError, match="longer than 0.5 seconds"):
        run(slow, {"x": 1})
    with pytest.raises(NodeError, match="has no node.json"):
        (tmp_path / "empty").mkdir()
        FolderNode(tmp_path / "empty")
    with pytest.raises(NodeError, match="command is a list"):
        FolderNode(app(tmp_path, "nocommand", "", {"command": "python main.py"}))


def test_a_graph_file_reaches_only_the_folders_it_is_given(tmp_path):
    apps = tmp_path / "apps"
    app(apps, "solver", SOLVER, {"inputs": ["numbers"], "outputs": ["answer", "where"]})
    described = {"method": "chain", "nodes": [{"id": "solve", "kind": "folder", "path": "solver"}]}
    with pytest.raises(SpecError, match="does not allow processes"):
        load(described, Catalog(apps=apps))
    with pytest.raises(SpecError, match="no apps directory"):
        load(described, Catalog(processes=True))
    outside = {"method": "chain", "nodes": [{"id": "x", "kind": "folder", "path": "../elsewhere"}]}
    with pytest.raises(SpecError, match="outside the apps directory"):
        load(outside, Catalog(processes=True, apps=apps))
    graph = load({"nodes": [{"id": "solve", "kind": "folder", "path": "solver"}],
                  "inputs": {"numbers": "solve.numbers"}, "outputs": {"answer": "solve.answer"}},
                 Catalog(processes=True, apps=apps))
    assert Engine(graph).run({"numbers": [2, 3]}).last("answer") == 5
    again = load(dump(graph), Catalog(processes=True, apps=apps))
    assert again.digest == graph.digest


def test_a_run_directory_remembers_its_apps(tmp_path):
    apps = tmp_path / "apps"
    app(apps, "solver", SOLVER, {"inputs": ["numbers"], "outputs": ["answer", "where"]})
    described = {"nodes": [{"id": "solve", "kind": "folder", "path": "solver"}],
                 "inputs": {"numbers": "solve.numbers"}, "outputs": {"answer": "solve.answer"}}
    state = Run.create(tmp_path / "run", described, processes=True, apps=apps).start({"numbers": [4, 5]})
    assert state["status"] == "done" and state["outputs"]["answer"] == [9]


def test_the_command_line_takes_apps(tmp_path, capsys):
    from interlacing.cli import main
    apps = tmp_path / "apps"
    app(apps, "solver", SOLVER, {"inputs": ["numbers"], "outputs": ["answer", "where"]})
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"nodes": [{"id": "solve", "kind": "folder", "path": "solver"}],
                                 "inputs": {"numbers": "solve.numbers"}, "outputs": {"answer": "solve.answer"}}))
    code = main(["run", str(graph), "--dir", str(tmp_path / "r"), "--processes", "--apps", str(apps),
                 "--input", "numbers=[1,1]"])
    assert code == 0 and json.loads(capsys.readouterr().out)["outputs"]["answer"] == [2]
