"""A tree with consent: a lead hands the job to two workers and must ask a person before it releases the result.

    python examples/graphs/governed_tree.py            # answers "yes" by itself
    python examples/graphs/governed_tree.py --ask      # asks you on the console

The run is journaled. Stop it with Ctrl-C while it is asking and start it again: the workers do not run a
second time, and the lead still knows what they replied.
"""
import sys
import tempfile
from pathlib import Path

from inter1ace.base import Firing, Port
from inter1ace.engine import Engine, Journal
from inter1ace.methods import Governed
from inter1ace.nodes import AgentNode, FunctionNode, HumanNode, Tool
from inter1ace.protocols import DEFAULT

from review_loop import Scripted


def lead(messages):
    last, arrived = messages[-1], [m["content"] for m in messages if m["role"] == "user"]
    if last["role"] == "tool":
        if last["name"] == "approve":
            return "", [("send_out", {"content": "released" if '"approved":true' in last["content"] else "held back"})]
        return "", []
    if len(arrived) == 1:
        return "", [("send_send", {"content": arrived[0].split("\n", 1)[1]})]
    if len(arrived) == 3:
        return "", [("approve", {"results": [text.split("\n", 1)[1] for text in arrived[1:]]})]
    return "", []


def build(ask):
    coordinator = AgentNode("lead", Scripted(lead), system="You coordinate.", firing=Firing.ANY,
                            inputs=[Port("task"), Port("reply", required=False)], outputs=[Port("out"), Port("send")],
                            tools=[Tool("approve", "Ask the operator before releasing.")])
    worker = lambda id, verb: FunctionNode(id, lambda x: f"{id} {verb} {x}", inputs=[Port("x")], outputs=[Port("y")])  # noqa: E731
    return Governed(coordinator, worker("survey", "surveyed"), worker("build", "built"),
                    operator=HumanNode("operator", ask), max_rounds=2).build(DEFAULT)


def console(request):
    print("the lead asks to release:", *request["results"], sep="\n  ")
    return {"approved": input("approve? [y/n] ").strip().lower().startswith("y")}


def main(ask=lambda request: {"approved": True}, journal=None):
    journal = Path(journal or Path(tempfile.mkdtemp()) / "run.jsonl")
    engine = Engine(build(ask), DEFAULT, journal=Journal(journal))
    result = engine.resume() if journal.exists() else engine.run({"in": "the bridge"})
    print(result.last("out"), result.rounds)
    return result


if __name__ == "__main__":
    main(console if "--ask" in sys.argv else (lambda request: {"approved": True}), "governed-run.jsonl" if "--ask" in sys.argv else None)
