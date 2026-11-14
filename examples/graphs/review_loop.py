"""A loop: a writer drafts, a reviewer sends the draft back until it is satisfied, at most four times.

    python examples/graphs/review_loop.py

Both are agents. The reviewer has two outputs, ``again`` and ``ok``, and chooses between them. The models
here are scripted stand-ins, so the example runs without any model service; put a real provider in their
place and nothing else changes.
"""
from interlacing.base import Port
from interlacing.engine import Engine
from interlacing.methods import Loop
from interlacing.nodes import AgentNode
from interlacing.protocols import DEFAULT


class Scripted:
    """Stands in for a model: ``answer(messages)`` returns ``(text, [(tool, args), ...])``."""

    model = "scripted"

    def __init__(self, answer):
        self.answer = answer

    def chat(self, system, messages, tools, max_tokens=4096):
        text, calls = self.answer(messages)
        return type("Result", (), {"text": text, "tool_calls": [{"id": f"c{i}", "name": n, "args": a} for i, (n, a) in enumerate(calls)]})()


def writer(messages):
    drafts = sum(1 for m in messages if m["role"] == "user")
    return f"draft {drafts}: " + "a careful argument, " * drafts, []


def reviewer(messages):
    last = messages[-1]
    if last["role"] == "tool":
        return "", []
    long_enough = last["content"].count("careful") >= 3
    return "", [("send_ok", {"content": last["content"]})] if long_enough else [("send_again", {"content": "longer, please"})]


def build():
    write = AgentNode("writer", Scripted(writer), system="You write.")
    review = AgentNode("reviewer", Scripted(reviewer), system="You review.", outputs=[Port("again", str), Port("ok", str)])
    return Loop(write, review, max_rounds=4).build(DEFAULT)


def main():
    result = Engine(build(), DEFAULT).run({"in": "write about graphs"})
    print(f"accepted after {result.rounds['writer']} drafts: {result.last('out')}")
    return result


if __name__ == "__main__":
    main()
