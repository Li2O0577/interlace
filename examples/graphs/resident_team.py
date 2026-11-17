"""A lead and two members who stay for as long as they have work, and decide themselves when they are done.

    python examples/graphs/resident_team.py

Each is a resident agent: one activation from start to finish, not one per message. The lead hands out two
parts, waits for the reports (waiting holds none of the run's workers), checks the sum with a plain
function it was given as a tool, sends the answer and finishes. Each member plans, sends its report as soon
as it is ready, then waits; told that nothing more can come, it finishes. The lead manages both: it, the
operator or a program could pause, stop or restart them. The models are scripted stand-ins, so the example
runs without any model service.
"""
from interlacing import AgentNode, Engine, GraphBuilder, Port
from interlacing.models import ChatResult, FakeProvider


def adds_up(parts: list[int], total: int) -> str:
    """Says whether the parts add up to the total."""
    return "yes" if sum(parts) == total else f"no: they add up to {sum(parts)}"


def calls(*pairs):
    return ChatResult(tool_calls=[{"id": f"c{i}", "name": name, "args": args} for i, (name, args) in enumerate(pairs)])


def told(messages):
    return next((m["content"] for m in reversed(messages) if m["role"] == "tool"), "")


def lead(system, messages, tools):
    reports = [m["content"] for m in messages if m["role"] == "tool" and "[reports]" in m["content"]]
    last = told(messages)
    if not last:
        return calls(("plan", {"items": ["get both parts", "check the sum"]}),
                     ("send_to_a", {"content": "count the even numbers below 10"}),
                     ("send_to_b", {"content": "count the odd numbers below 10"}), ("wait", {}))
    if len(reports) < 2:
        return calls(("wait", {}))
    if "add up" not in last and last != "yes" and "Your task board" in last:
        return calls(("done", {"item": 1}), ("adds_up", {"parts": [5, 5], "total": 10}))
    return calls(("done", {"item": 2}), ("send_out", {"content": "10 numbers below 10, checked"}), ("finish", {}))


def member(count):
    def policy(system, messages, tools):
        last = told(messages)
        if not last:
            return calls(("plan", {"items": ["count"]}), ("done", {"item": 1}),
                         ("send_report", {"content": count}), ("wait", {}))
        return calls(("finish", {}))
    return policy


def build():
    boss = AgentNode("lead", FakeProvider(lead), resident=True, tools=[adds_up],
                     inputs=[Port("task"), Port("reports", required=False, many=True)],
                     outputs=[Port("to_a"), Port("to_b"), Port("out")])
    a = AgentNode("a", FakeProvider(member(5)), resident=True, outputs=[Port("report")])
    b = AgentNode("b", FakeProvider(member(5)), resident=True, outputs=[Port("report")])
    return (GraphBuilder().add(boss, a, b).manage("lead", "a", "b")
            .connect("lead.to_a", "a.task").connect("lead.to_b", "b.task")
            .connect("a.report", "lead.reports", back=True, max_rounds=1)
            .connect("b.report", "lead.reports", back=True, max_rounds=1)
            .input("in", "lead.task").output("out", "lead.out").compile())


def main():
    result = Engine(build(), workers=1).run({"in": "how many numbers are there below 10?"})
    print(f"{result.last('out')}; activations: {result.rounds}")


if __name__ == "__main__":
    main()
