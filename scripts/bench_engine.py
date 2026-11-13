"""How long the engine takes per message, and whether that depends on the size of the graph.

    python scripts/bench_engine.py

Each row runs the same number of messages through chains of different lengths. If scheduling costs the same
per message whatever the graph's size, the microseconds per message stay level from row to row.
"""
import time

from interlace.base import Port
from interlace.engine import Engine
from interlace.methods import Chain
from interlace.nodes import FunctionNode
from interlace.protocols import DEFAULT


def source(ctx, x):
    for i in range(x):
        ctx.emit("y", i)


def passing(x):
    return x


def measure(nodes: int, messages: int, workers: int = 0):
    parts = [FunctionNode("n0", source, inputs=[Port("x")], outputs=[Port("y")], context=True)]
    parts += [FunctionNode(f"n{i}", passing, inputs=[Port("x")], outputs=[Port("y")]) for i in range(1, nodes)]
    started = time.perf_counter()
    graph = Chain(*parts).build(DEFAULT)
    compiled = time.perf_counter() - started
    engine = Engine(graph, DEFAULT, workers=workers)
    started = time.perf_counter()
    result = engine.run({"in": messages})
    ran = time.perf_counter() - started
    assert len(result.outputs["out"]) == messages
    return compiled, ran, result.envelopes


if __name__ == "__main__":
    print(f"{'nodes':>7} {'messages':>9} {'workers':>7} {'compile s':>10} {'run s':>8} {'envelopes':>10} {'us/envelope':>12}")
    for nodes, messages, workers in [(10, 10_000, 0), (100, 1_000, 0), (1_000, 100, 0), (10_000, 10, 0), (10_000, 10, 4)]:
        compiled, ran, envelopes = measure(nodes, messages, workers)
        print(f"{nodes:>7} {messages:>9} {workers:>7} {compiled:>10.3f} {ran:>8.3f} {envelopes:>10} {ran / envelopes * 1e6:>12.2f}")
