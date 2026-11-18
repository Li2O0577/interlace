"""A lead that builds its own team: it hires members while the run goes on, gives them work, and manages them.

    python examples/graphs/hired_team.py

The graph has one node, the lead. It may hire (``hire=2``: two at most in the whole run) and only from its
templates. Each member it hires becomes a node of the graph, managed by the lead, with a line from the lead
(``send_to_<name>``) and one back (``from_<name>``). The lead splits the work, hires two counters, waits for
both, looks at its team, dismisses them and sends the total. A third hire would be refused. The model is a
scripted stand-in; the members are plain functions.
"""
from interlacing import AgentNode, Engine, FunctionNode, GraphBuilder, Port
from interlacing.models import ChatResult, FakeProvider


def counter(id):
    """A template: a function of the new member's id that makes it."""
    return FunctionNode(id, lambda text: len(text.split()), inputs=[Port("text")], outputs=[Port("words")])


def calls(*pairs):
    return ChatResult(tool_calls=[{"id": f"c{i}", "name": name, "args": args} for i, (name, args) in enumerate(pairs)])


def lead(system, messages, tools):
    heard = [m["content"] for m in messages if m["role"] == "tool" and "[from_" in m["content"]]
    if not heard:
        if not any(m["role"] == "tool" for m in messages):
            words = messages[-1]["content"].split()
            half = len(words) // 2
            return calls(("hire", {"template": "counter", "name": "left", "task": " ".join(words[:half])}),
                         ("hire", {"template": "counter", "name": "right", "task": " ".join(words[half:])}),
                         ("wait", {}))
        return calls(("wait", {}))
    if len(heard) < 2:
        return calls(("wait", {}))
    total = sum(int(part.split("\n")[1]) for part in heard)
    return calls(("team", {}), ("dismiss", {"name": "left"}), ("dismiss", {"name": "right"}),
                 ("send_out", {"content": str(total)}), ("finish", {}))


def main():
    boss = AgentNode("lead", FakeProvider(lead), resident=True, hire=2, templates={"counter": counter})
    graph = GraphBuilder().add(boss).input("text", "lead.task").output("words", "lead.out").compile()
    engine = Engine(graph)
    result = engine.run({"text": "the quick brown fox jumps over the lazy dog"})
    print("words:", result.last("words"))
    print("nodes at the end:", [node.id for node in engine.graph.nodes], "- hired in all:", boss.hires)
    return result, engine, boss


if __name__ == "__main__":
    main()
