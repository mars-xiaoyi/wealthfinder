import asyncio

import pytest

from app.entity_analysis.worker_pool import WorkerPool

# A concurrency bug shows up as a hang, so fail fast instead.
pytestmark = [pytest.mark.timeout(10)]

BACKOFF_S = 1


def _now() -> float:
    return asyncio.get_running_loop().time()


async def dispatch_success():
    """dispatch() returns once the work has completed."""
    done: list[str] = []

    async def _work() -> None:
        await asyncio.sleep(0)
        done.append("work")

    await WorkerPool("entity_analysis", 1, BACKOFF_S).dispatch(_work)

    assert done == ["work"]


async def dispatch_max_concurrent():
    """No more than max_concurrent pieces of work run at once, and the cap is reached."""
    pool = WorkerPool("entity_analysis", 3, BACKOFF_S)
    running = 0
    peak = 0
    finished = 0

    async def _work() -> None:
        nonlocal running, peak, finished
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        finished += 1

    await asyncio.gather(*(pool.dispatch(_work) for _ in range(7)))

    assert peak == 3
    assert finished == 7


async def dispatch_waiting_for_slot_during_backoff():
    """Work already running when the backoff starts carries on; work waiting for its slot
    still waits out the backoff."""
    pool = WorkerPool("entity_analysis", 1, BACKOFF_S)
    running = asyncio.Event()
    release = asyncio.Event()
    finished_at: dict[str, float] = {}
    started_at: dict[str, float] = {}

    async def _running_work() -> None:
        running.set()
        await release.wait()
        finished_at["running"] = _now()

    async def _waiting_work() -> None:
        started_at["waiting"] = _now()

    first = asyncio.create_task(pool.dispatch(_running_work))
    await running.wait()
    second = asyncio.create_task(pool.dispatch(_waiting_work))
    await asyncio.sleep(0)
    triggered_at = _now()
    pool.trigger_backoff()
    release.set()
    await asyncio.gather(first, second)

    assert finished_at["running"] < triggered_at + BACKOFF_S
    assert started_at["waiting"] >= triggered_at + BACKOFF_S


async def dispatch_backoff_extended():
    """A backoff triggered while work is waiting one out pushes that work's start back to the
    later deadline."""
    pool = WorkerPool("entity_analysis", 1, BACKOFF_S)
    started_at: list[float] = []

    async def _work() -> None:
        started_at.append(_now())

    pool.trigger_backoff()
    waiting = asyncio.create_task(pool.dispatch(_work))
    await asyncio.sleep(BACKOFF_S / 2)
    extended_at = _now()
    pool.trigger_backoff()
    await waiting

    assert len(started_at) == 1
    assert started_at[0] >= extended_at + BACKOFF_S
