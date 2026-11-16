"""A graph that changes while it runs: a planner adds one solver for each part of the task it is given.

    python examples/graphs/changed_while_running.py

The graph starts with the planner alone. In its activation it asks for a change, one Change with every node,
edge and graph output it needs; the change is checked like a graph being built and made at once, and the
solvers it added run as if they had been there from the start.
"""
from interlacing import Change, Engine, FunctionNode, GraphBuilder, Port


def solve(x):
    return f"{x}: done"


def plan(ctx, task):
    parts = task.split(", ")
    change = Change()
    for i in range(len(parts)):
        change.add(FunctionNode(f"solve{i}", solve, inputs=[Port("x")], outputs=[Port("y")]))
        change.connect(f"plan.p{i}", f"solve{i}.x").output(f"part{i}", f"solve{i}.y")
    ctx.change(change)
    for i, part in enumerate(parts):
        ctx.emit(f"p{i}", part)


def main():
    planner = FunctionNode("plan", plan, inputs=[Port("task", str)], outputs=[Port(f"p{i}") for i in range(8)],
                           context=True)
    engine = Engine(GraphBuilder().add(planner).input("in", "plan.task").compile(), workers=2,
                    observe=lambda event: event["event"] == "changed" and print("added", event["added"]))
    result = engine.run({"in": "read the data, fit the model, write it up"})
    for name, values in sorted(result.outputs.items()):
        print(name, values)
    return result


if __name__ == "__main__":
    main()
