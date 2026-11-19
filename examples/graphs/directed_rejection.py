"""A rejection with a direction: the receiver says which of several senders it rejects, and the sender
corrects what else it sent along with it.

    python examples/graphs/directed_rejection.py

Two estimators feed one port of ``merge`` (``many=True``), each on an edge that takes one rejection. The quick
one also sends a note to a log. ``merge`` sees that the quick estimate is far from the careful one and
rejects that one by its sender (``which="quick"``). The quick estimator is told what was rejected and what
else it sent with it (``rejection["sent"]``): it corrects the estimate and the note together. The log, which
had already read the note, runs again with the corrected one.
"""
from interlacing import Engine, FunctionNode, GraphBuilder, Port


def quick(rooms):
    guess = rooms * 10
    return {"estimate": guess, "note": f"quick: {guess} m2, by rule of thumb"}


def answer(ctx, rejection):
    """Asked again: measure properly, and put right the note that went with the estimate."""
    measured = 74
    ctx.correct(rejection["message"], measured)
    for name in rejection["sent"].get("note", []):
        ctx.correct(name, f"quick: {measured} m2, measured after a rejection ({rejection['reason']})")


def merge(ctx, estimates):
    senders = [taken["from"] for taken in ctx.taken("estimates")]
    by = dict(zip(senders, estimates))
    if abs(by["quick"] - by["careful"]) > 10:
        verdict = ctx.reject("estimates", f"{by['quick']} is far from {by['careful']}", "quick")
        if verdict["answer"] == "corrected":
            by["quick"] = verdict["value"]
    return (by["quick"] + by["careful"]) / 2


def main():
    fast = FunctionNode("quick", quick, inputs=[Port("rooms")], outputs=[Port("estimate"), Port("note")])
    fast.challenged = answer
    slow = FunctionNode("careful", lambda plan: 76, inputs=[Port("plan")], outputs=[Port("estimate")])
    join = FunctionNode("merge", merge, inputs=[Port("estimates", many=True)], outputs=[Port("area")], context=True)
    log = FunctionNode("log", lambda note: f"logged: {note}", inputs=[Port("note")], outputs=[Port("line")])
    graph = (GraphBuilder().add(fast, slow, join, log)
             .connect("quick.estimate", "merge.estimates", challenges=1)
             .connect("careful.estimate", "merge.estimates", challenges=1)
             .connect("quick.note", "log.note")
             .input("rooms", "quick.rooms").input("plan", "careful.plan")
             .output("area", "merge.area").output("log", "log.line").compile())
    result = Engine(graph).run({"rooms": 5, "plan": "floor plan"})
    print("area:", result.last("area"))
    print(result.last("log"))
    return result


if __name__ == "__main__":
    main()
