import asyncio
import dataclasses
from unittest import mock
from unittest.mock import MagicMock

import pytest

from app.aggregation import group_coroutine as group_coroutine_module
from app.aggregation.group_coroutine import GroupCoroutine, GroupKey
from app.config import AggregationConfig
from app.models.entity_event import EventTypeEnum

# A concurrency bug shows up as a hang, so fail fast instead.
pytestmark = [pytest.mark.timeout(5)]

CONFIG = AggregationConfig(
    batch_size=10,
    batch_timeout_ms=200,
    max_concurrent=2,
    group_idle_timeout_s=60,
    db_flush_interval_s=300,
    sliding_window_hours=4,
    event_max_timespan_hours=24,
    consumer_name="pod-1",
)
IDLE_CONFIG = dataclasses.replace(CONFIG, group_idle_timeout_s=1)
GROUP_KEY = GroupKey("HKEX", "00700", EventTypeEnum.EARNINGS)


def _recording_work(
    record: list[str],
    name: str,
    release: asyncio.Event | None = None,
    started: asyncio.Event | None = None,
    done: asyncio.Event | None = None,
):
    """A unit of work that records its start and end, waiting for release in between, and sets
    started/done as it goes."""

    async def work() -> None:
        record.append(f"{name}_started")
        if started is not None:
            started.set()
        if release is not None:
            await release.wait()
        record.append(f"{name}_done")
        if done is not None:
            done.set()

    return work


def _idle_callback() -> tuple[MagicMock, asyncio.Event]:
    """An on_idle that records its calls and sets the returned event."""
    idle = asyncio.Event()
    return MagicMock(side_effect=lambda _key: idle.set()), idle


async def enqueue_runs_in_order():
    """Work runs one unit at a time, in enqueue order: the next starts only once the one
    before it is done."""
    record: list[str] = []
    release, a_started, c_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    group = GroupCoroutine(GROUP_KEY, CONFIG, MagicMock())

    await group.enqueue(_recording_work(record, "a", release, started=a_started))
    await group.enqueue(_recording_work(record, "b"))
    await group.enqueue(_recording_work(record, "c", done=c_done))
    await a_started.wait()
    await asyncio.sleep(0.05)
    assert record == ["a_started"]
    release.set()
    await c_done.wait()

    assert record == ["a_started", "a_done", "b_started", "b_done", "c_started", "c_done"]
    await group.close()


async def enqueue_work_failure_continues():
    """A unit of work that raises doesn't stop the group: the next one still runs."""
    record: list[str] = []
    b_done = asyncio.Event()
    on_idle = MagicMock()
    group = GroupCoroutine(GROUP_KEY, CONFIG, on_idle)

    async def failing_work() -> None:
        raise RuntimeError("bug")

    await group.enqueue(failing_work)
    await group.enqueue(_recording_work(record, "b", done=b_done))
    await b_done.wait()

    assert record == ["b_started", "b_done"]
    on_idle.assert_not_called()
    await group.close()


async def enqueue_idle_group_ends():
    """A group with no work for the idle timeout reports its key through on_idle, once."""
    on_idle, idle = _idle_callback()
    record: list[str] = []
    group = GroupCoroutine(GROUP_KEY, IDLE_CONFIG, on_idle)

    await group.enqueue(_recording_work(record, "a"))
    await idle.wait()
    await asyncio.sleep(0.05)

    assert record == ["a_started", "a_done"]
    on_idle.assert_called_once_with(GROUP_KEY)


async def enqueue_at_idle_timeout_still_runs():
    """Work enqueued just as the idle timeout fires still runs before the group ends."""
    on_idle, idle = _idle_callback()
    record: list[str] = []
    real_wait_for = asyncio.wait_for
    waits = 0

    async def timeout_after_enqueue(awaitable, **_kwargs):
        nonlocal waits
        waits += 1
        if waits == 1:
            awaitable.close()
            await group.enqueue(_recording_work(record, "late"))
            raise TimeoutError
        return await real_wait_for(awaitable, timeout=0.05)

    with mock.patch.object(group_coroutine_module.asyncio, "wait_for", timeout_after_enqueue):
        group = GroupCoroutine(GROUP_KEY, CONFIG, on_idle)
        await idle.wait()

    assert record == ["late_started", "late_done"]
    on_idle.assert_called_once_with(GROUP_KEY)


async def close_success():
    """Closing stops the group's running work and abandons what is queued, without reporting
    it idle."""
    record: list[str] = []
    a_started = asyncio.Event()
    on_idle = MagicMock()
    group = GroupCoroutine(GROUP_KEY, CONFIG, on_idle)
    await group.enqueue(_recording_work(record, "a", asyncio.Event(), started=a_started))
    await group.enqueue(_recording_work(record, "b"))
    await a_started.wait()

    await group.close()
    await asyncio.sleep(0.05)

    assert record == ["a_started"]
    on_idle.assert_not_called()
