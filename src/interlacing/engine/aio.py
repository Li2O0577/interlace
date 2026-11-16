"""What the engine needs to run on an event loop: a lock that also wakes the loop, threads for the nodes that
are not coroutines, and a way to run a coroutine to its end from code that is not one."""
from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any, Awaitable, Callable, Generator


_notify, _notify_all = threading.Condition.notify, threading.Condition.notify_all


class Signal(threading.Condition):
    """The engine's lock. Besides waking threads that wait on it, a notify calls ``hook``: that is how the
    event loop learns that something changed in a thread of its own (a node became ready, the run is
    stopping)."""

    hook: Callable[[], None] | None = None

    def notify(self, n: int = 1) -> None:
        _notify(self, n)
        hook = self.hook
        if hook is not None:
            hook()

    def notify_all(self) -> None:
        _notify_all(self)
        hook = self.hook
        if hook is not None:
            hook()


class Threads:
    """At most ``size`` daemon threads that run plain functions for an event loop. Daemon threads, because an
    activation that overran is left behind and must not keep the process alive."""

    def __init__(self, size: int, loop: asyncio.AbstractEventLoop):
        self.size, self.loop = size, loop
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        self.started = 0

    def run(self, function: Callable, *args: Any) -> asyncio.Future:
        future = self.loop.create_future()
        if self.started < self.size:
            self.started += 1
            threading.Thread(target=self._work, name=f"engine-{self.started}", daemon=True).start()
        self.queue.put((future, function, args))
        return future

    def _work(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                return
            future, function, args = item
            try:
                outcome, failed = function(*args), False
            except BaseException as exc:   # noqa: BLE001 - it goes back to whoever awaits it
                outcome, failed = exc, True
            try:
                self.loop.call_soon_threadsafe(_settle, future, outcome, failed)
            except RuntimeError:
                pass                       # the loop has closed: nobody is waiting any more

    def close(self) -> None:
        for _ in range(self.started):
            self.queue.put(None)


def _settle(future: asyncio.Future, outcome: Any, failed: bool) -> None:
    if not future.done():
        if failed:
            future.set_exception(outcome)
        else:
            future.set_result(outcome)


def to_end(make: Callable[[], Awaitable]) -> Any:
    """Run the coroutine ``make()`` gives to its end and return what it returns. In a thread where an event
    loop is already running, it runs in a thread of its own, on a loop of its own."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_wrapped(make))
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = asyncio.run(_wrapped(make))
        except BaseException as exc:   # noqa: BLE001 - raised again in the caller's thread
            box["error"] = exc
    thread = threading.Thread(target=run, name="engine-loop", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


async def _wrapped(make: Callable[[], Awaitable]) -> Any:
    return await make()


def through(work: Generator, wait: Callable[[Any], Any]) -> Any:
    """Run ``work``, a generator that yields what it needs awaited and is sent what that came to, giving
    each yielded thing to ``wait``. Returns what the generator returns; what ``wait`` raises goes into it."""
    sent: Any = None
    error: BaseException | None = None
    while True:
        try:
            item = work.throw(error) if error is not None else work.send(sent)
        except StopIteration as stop:
            return stop.value
        try:
            sent, error = wait(item), None
        except BaseException as exc:   # noqa: BLE001 - it goes into the generator
            sent, error = None, exc


async def athrough(work: Generator) -> Any:
    """``through``, awaiting what the generator yields."""
    sent: Any = None
    error: BaseException | None = None
    while True:
        try:
            item = work.throw(error) if error is not None else work.send(sent)
        except StopIteration as stop:
            return stop.value
        try:
            sent, error = await item, None
        except BaseException as exc:   # noqa: BLE001 - it goes into the generator
            sent, error = None, exc
