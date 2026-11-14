"""The engine awaited, and nodes whose functions are coroutines."""
import asyncio
import threading
import time

import pytest

from interlacing import DEFAULT, Chain, Engine, FunctionNode, Journal, Port, RunError


def fn(id, function, **options):
    return FunctionNode(id, function, inputs=[Port('x')], outputs=[Port('y')], **options)


def test_a_run_is_awaited_while_the_loop_stays_free():
    async def main():
        ticks = []

        async def tick():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.005)
        ticking = asyncio.create_task(tick())
        graph = Chain(fn('slow', lambda x: (time.sleep(0.1), x + 1)[1]), fn('double', lambda x: x * 2)).build(DEFAULT)
        result = await Engine(graph, DEFAULT).arun({'in': 1})
        ticking.cancel()
        return result, len(ticks)
    result, ticks = asyncio.run(main())
    assert result.outputs == {'out': [4]} and ticks > 5


def test_several_runs_are_awaited_together():
    async def main():
        graphs = [Chain(fn('a', lambda x, i=i: x + i)).build(DEFAULT) for i in range(5)]
        return await asyncio.gather(*(Engine(graph, DEFAULT).arun({'in': 10}) for graph in graphs))
    assert [result.last('out') for result in asyncio.run(main())] == [10, 11, 12, 13, 14]


def test_a_node_function_may_be_a_coroutine():
    async def later(x):
        await asyncio.sleep(0.01)
        return x * 3

    async def with_context(ctx, x):
        await asyncio.sleep(0)
        ctx.emit('y', x)
        ctx.emit('y', x + 1)
    assert Engine(Chain(fn('later', later)).build(DEFAULT), DEFAULT).run({'in': 2}).outputs == {'out': [6]}
    assert Engine(Chain(fn('twice', with_context, context=True)).build(DEFAULT), DEFAULT).run({'in': 2}).outputs == {'out': [2, 3]}

    async def main():
        return await Engine(Chain(fn('later', later)).build(DEFAULT), DEFAULT, workers=2).arun({'in': 5})
    assert asyncio.run(main()).outputs == {'out': [15]}


def test_a_failure_reaches_the_one_who_awaits():
    async def broken(x):
        raise ValueError('no good')

    async def main():
        await Engine(Chain(fn('broken', broken)).build(DEFAULT), DEFAULT).arun({'in': 1})
    with pytest.raises(RunError, match='node broken failed in round 1: no good'):
        asyncio.run(main())


def test_cancelling_the_wait_stops_the_run():
    started, saw = threading.Event(), threading.Event()

    def waits(ctx, x):
        started.set()
        while not ctx.stopping():
            time.sleep(0.005)
        saw.set()

    async def main():
        engine = Engine(Chain(fn('waits', waits, context=True)).build(DEFAULT), DEFAULT)
        task = asyncio.create_task(engine.arun({'in': 1}))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(main())
    assert saw.wait(5)


def test_a_run_is_resumed_and_gone_on_with_awaited(tmp_path):
    def fails_once(x, seen=[]):
        seen.append(x)
        if len(seen) == 1:
            raise ValueError('first time')
        return x + 1

    def build():
        return Chain(fn('first', lambda x: x * 2), fn('second', fails_once)).build(DEFAULT)

    async def main():
        with pytest.raises(RunError):
            await Engine(build(), DEFAULT, journal=Journal(tmp_path / 'run.jsonl')).arun({'in': 4})
        resumed = await Engine(build(), DEFAULT, journal=Journal(tmp_path / 'run.jsonl')).aresume()
        gone = await Engine(build(), DEFAULT, journal=Journal(tmp_path / 'other.jsonl')).ago({'in': 1})
        return resumed, gone
    resumed, gone = asyncio.run(main())
    assert resumed.outputs == {'out': [9]} and gone.outputs == {'out': [3]}
