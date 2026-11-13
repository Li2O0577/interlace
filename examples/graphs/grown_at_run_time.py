"""A graph that is only decided while the run is going on.

    python examples/graphs/grown_at_run_time.py

The outer graph is two nodes and stays two nodes. ``plan`` reads the task and writes, as data, the graph the
task needs: one node for each measure that was asked for, side by side. ``grow`` builds that graph, runs it
and sends back what came out. Ask for two measures and two nodes exist; ask for four and four do.

What the written graph may reach is decided by the catalog ``grow`` was given, not by ``plan``: here, the
functions of this module and no others.
"""
from indeterminate import Chain, Engine, FunctionNode, Port
from indeterminate.protocols import DEFAULT
from indeterminate.spec import Catalog, SpawnNode

HERE = __name__


def mean(x):
    return f"mean {sum(x) / len(x):g}"


def largest(x):
    return f"largest {max(x):g}"


def spread(x):
    return f"spread {max(x) - min(x):g}"


def plan(task):
    """One node for each measure the task names; the graph is written, not built, here."""
    nodes = [{"id": name, "kind": "function", "function": f"{HERE}.{name}", "inputs": ["x"], "outputs": ["y"]}
             for name in task["measures"]]
    return {"graph": {"method": "fan", "nodes": nodes}, "inputs": {"in": task["numbers"]}}


def build():
    planner = FunctionNode("plan", plan, inputs=[Port("task", dict)], outputs=[Port("request")])
    return Chain(planner, SpawnNode("grow", Catalog(allow=[HERE]), max_nodes=8, workers=4)).build(DEFAULT)


def main():
    seen = []
    engine = Engine(build(), DEFAULT, observe=seen.append)
    result = engine.run({"in": {"measures": ["mean", "largest", "spread"], "numbers": [3, 9, 4, 8]}})
    answer = result.last("out")
    print("made during the run:", sorted({e["node"] for e in seen if e.get("within") and e["event"] == "started"}))
    print(answer.get("outputs") or answer)
    refused = Engine(build(), DEFAULT).run({"in": {"measures": ["mean", "exit"], "numbers": [1]}}).last("out")
    print("refused:", refused["error"])
    return result, refused


if __name__ == "__main__":
    main()
