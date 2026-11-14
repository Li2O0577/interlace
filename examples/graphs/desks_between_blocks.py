"""Desks: a block takes work in at one desk, shares it among its members, and gives one answer at another.

    python examples/graphs/desks_between_blocks.py

Two survey blocks each have a desk at the way in, which gives each member its own part of the brief, and a
desk at the way out, which makes one report of what the members found. A third block has a desk that takes
both reports, from two different blocks, and shares them among its own members.
"""
from interlace import Block, Desk, Engine, Fan, FunctionNode, GraphBuilder, Port
from interlace.protocols import DEFAULT


def member(id, function):
    return FunctionNode(id, function, inputs=[Port("x")], outputs=[Port("y")])


def brief(received):
    """The desk at the way into a survey: the place to one member, the question to the other."""
    place, question = received["x"].split(": ")
    return {"where": place, "what": question}


def report(received):
    """The desk at the way out: one line made of what both members gave."""
    return {"y": f"{received['what']} at {received['where']}"}


def survey():
    return Fan(member("where", lambda x: x.upper()), member("what", lambda x: f"measured {x}"),
               split=Desk("door", gives=["where", "what"], sort=brief),
               join=Desk("exit", takes=["where", "what"], sort=report))


def summary():
    """Takes the reports of two blocks at one desk; one member counts them, the other puts them in order."""
    both = lambda received: dict.fromkeys(["count", "order"], [received["north"], received["south"]])   # noqa: E731
    return Fan(member("count", lambda x: len(x)), member("order", lambda x: sorted(x)),
               split=Desk("door", takes=["north", "south"], gives=["count", "order"], sort=both),
               join=Desk("exit", takes=["count", "order"]))


def build():
    surveys = Block(survey, name="survey")
    return (GraphBuilder().add(surveys("north"), surveys("south"), Block(summary, name="summary")("summary"))
            .connect("north.out", "summary.north", "value").connect("south.out", "summary.south", "value")
            .input("north", "north.in").input("south", "south.in").output("out", "summary.out").compile(DEFAULT))


def main():
    result = Engine(build(), DEFAULT, workers=2).run({"north": "harbour: tide", "south": "estuary: salt"})
    print(result.last("out"))
    return result


if __name__ == "__main__":
    main()
