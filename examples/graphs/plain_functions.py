"""The short way: plain functions are nodes, and nothing is written that the functions already say.

    python examples/graphs/plain_functions.py

Each function's name is its node's id, its parameters are the inputs, its annotations the schemas, and what
it returns goes out of ``out``. ``@node`` is only needed where something differs: here, a check on the output.
"""
from interlacing import Chain, Fan, Loop, node, run


def clean(text: str) -> str:
    return " ".join(text.split())


def words(text: str) -> int:
    return len(text.split())


def letters(text: str) -> int:
    return sum(ch.isalpha() for ch in text)


@node(check=lambda line: not line.startswith("0 ") or "there were no words")
def report(x: list) -> str:
    return f"{x[0]} words, {x[1]} letters"


def shorten(text: str) -> str:
    return text if len(text) <= 40 else text[: len(text) * 3 // 4]


def main():
    counted = run(Chain(clean, Fan(words, letters), report), {"in": "  the cat   and the dog "})
    print(counted.last("out"))
    # a loop that goes round until the text fits
    short = run(Loop(shorten, until=lambda text: len(text) <= 40, max_rounds=10), {"in": "x" * 100})
    print(len(short.last("out")))
    return counted, short


if __name__ == "__main__":
    main()
