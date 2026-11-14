"""A chain: a function, an external program, a function.

    python examples/graphs/chain_with_program.py

The middle node is another process. It could be any program that reads its standard input and writes its
standard output; here it is a few lines of Python that count words.
"""
import sys

from interlacing.base import Port
from interlacing.engine import Engine
from interlacing.methods import Chain
from interlacing.nodes import FunctionNode, ProcessNode
from interlacing.protocols import DEFAULT

COUNT = "import sys, json, collections; print(json.dumps(collections.Counter(json.load(sys.stdin)).most_common(3)))"


def build():
    split = FunctionNode("split", lambda text: text.lower().split(), inputs=[Port("text", str)], outputs=[Port("words", [str])])
    count = ProcessNode("count", [sys.executable, "-c", COUNT], format="json")
    report = FunctionNode("report", lambda x: ", ".join(f"{word} x{n}" for word, n in x), inputs=[Port("x")], outputs=[Port("line", str)])
    return Chain(split, count, report).build(DEFAULT)


def main():
    result = Engine(build(), DEFAULT).run({"in": "the cat and the dog and the bird"})
    print(result.last("out"))
    return result


if __name__ == "__main__":
    main()
