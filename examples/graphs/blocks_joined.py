"""Blocks: a design used several times, and designs joined into larger ones.

    python examples/graphs/blocks_joined.py

``polish`` is a small design: tidy a sentence, then check it, going round until the check passes. It is
joined with another node into ``sentence``, and that is used three times side by side, each use with its
own state.
"""
from interlace import Block, Chain, Engine, FunctionNode, Loop, Port, par, seq
from interlace.protocols import DEFAULT


def node(function):
    """A function that makes a node from an id: the form in which seq and par take a part that is not a block."""
    return lambda id: FunctionNode(id, function, inputs=[Port("x")], outputs=[Port("y")])


def tidy(x):
    x = " ".join(x.split())
    return x if x.endswith(".") else x + "."


polish = Block(lambda: Loop(node(tidy)("tidy"), until=lambda x: x.endswith("."), max_rounds=3), name="polish")
capital = node(lambda x: x.strip().capitalize())
sentence = seq(capital, polish, name="sentence")                    # a block made of a node and a block
panel = par(sentence, sentence, sentence, name="panel")             # the same design three times


def build():
    first = FunctionNode("first", lambda text: text.split(";")[0], inputs=[Port("text", str)], outputs=[Port("y", str)])
    join = FunctionNode("join", lambda x: " | ".join(x), inputs=[Port("x")], outputs=[Port("line", str)])
    return Chain(first, panel("panel"), join).build(DEFAULT)


def main():
    result = Engine(build(), DEFAULT, workers=3).run({"in": "  one  design,   three uses ; the rest"})
    print(result.last("out"))
    return result


if __name__ == "__main__":
    main()
