"""A receiver that does not take what it is given on trust: it may reject it, and the sender must answer.

    python examples/graphs/rejected_answer.py

The edge from the planner to the reviewer takes up to two rejections (``challenges=2``). The reviewer, an
agent, is told to judge what came and has a ``reject`` tool; it rejects the first plan because a step is
missing, and waits. The planner, also an agent, has finished by then: it is called back to answer, and either
explains why its plan stands or corrects it (here it corrects). The correction is the reviewer's answer, and
it goes on with it. The models are scripted stand-ins.
"""
from interlacing import AgentNode, Engine, GraphBuilder, Port
from interlacing.models import ChatResult, FakeProvider


def planner(system, messages, tools):
    said = messages[-1]["content"]
    if "[rejected]" in said:
        return ChatResult(tool_calls=[{"id": "c1", "name": "correct",
                                       "args": {"content": "1. measure 2. cut 3. check the fit 4. glue"}}])
    return ChatResult(text="1. measure 2. cut 3. glue")


def reviewer(system, messages, tools):
    answered = next((m["content"] for m in reversed(messages) if m["role"] == "tool"), "")
    if answered:
        return ChatResult(text="approved: " + answered.rsplit("\n", 1)[-1])
    if "check" not in messages[-1]["content"]:
        return ChatResult(tool_calls=[{"id": "r1", "name": "reject",
                                       "args": {"port": "plan", "reason": "nothing checks the fit before gluing"}}])
    return ChatResult(text="approved as it came")


def main():
    plan = AgentNode("planner", FakeProvider(planner), inputs=[Port("task")], outputs=[Port("plan")])
    review = AgentNode("reviewer", FakeProvider(reviewer), inputs=[Port("plan")], outputs=[Port("verdict")])
    graph = (GraphBuilder().add(plan, review).connect("planner.plan", "reviewer.plan", challenges=2)
             .input("task", "planner.task").output("verdict", "reviewer.verdict").compile())
    result = Engine(graph).run({"task": "fix the shelf"})
    print(result.last("verdict"))
    return result


if __name__ == "__main__":
    main()
