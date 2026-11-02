import asyncio
import functools
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
import redis.asyncio
import redis.exceptions
import structlog.testing

from app import layer_tasks
from tests.integration.conftest import REFUSED_URL, Infra

pytestmark = [pytest.mark.live, pytest.mark.timeout(120)]

AGGREGATION_ENV = {
    "AGG_BATCH_TIMEOUT_MS": "200",
    "REDIS_RECONNECT_INTERVAL_S": "1",
}
POLL_INTERVAL_S = 0.05
SETTLE_S = 0.5
# A fixed publication time; the sliding window and maximum timespan are the defaults (4 h, 24 h).
P = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
# The full sliding window plus two of the default 300 s flush intervals.
WINDOW_TTL_S = 4 * 3600 + 600
_INSERT_SQL = """
    INSERT INTO entity_events (
        entity_event_id, exchange, source_url, source_name, published_at, stock_code,
        event_type_primary, sentiment_label, sentiment_score, headline, entity_summary,
        additional_outcome, metadata, created_at, updated_at
    )
    VALUES ($1, 'HKEX', $2, 'MINGPAO', $3, $4, 'EARNINGS', 'POSITIVE', 0.8, '業績超預期',
            '季度收入按年增長。', $5, $6, $7, $7)
"""
_COMPANY_NAMES = {"00700": ("騰訊控股", "TENCENT"), "00005": ("匯豐控股", "HSBC HOLDINGS")}


class AggregationProbe:
    """The test's own view of SAPI's storage: entity_events rows in, stream messages in, the
    active Events and pending messages out. Database queries use a fresh connection each, so
    they survive a Postgres restart."""

    def __init__(self, infra: Infra, stream: redis.asyncio.Redis, state: redis.asyncio.Redis):
        self._database_url = infra.env()["DATABASE_URL"]
        self._stream = stream
        self._state = state

    async def seed(self, stock_code: str, published_at: datetime) -> uuid.UUID:
        """Store an entity_events row for an EARNINGS event of stock_code; returns its id."""
        entity_event_id = uuid.uuid4()
        zh, en = _COMPANY_NAMES[stock_code]
        additional_outcome = {
            "version": "v1.0.0",
            "output": {
                "company_name_zh": zh,
                "company_name_en": en,
                "event_type_secondary": None,
                "headline": "業績超預期",
                "entity_summary": "季度收入按年增長。",
            },
        }
        conn = await asyncpg.connect(self._database_url)
        try:
            await conn.execute(
                _INSERT_SQL,
                entity_event_id,
                f"https://example.com/{entity_event_id}",
                published_at,
                stock_code,
                json.dumps(additional_outcome, ensure_ascii=False),
                json.dumps({"cleaned_id": str(uuid.uuid4())}),
                datetime.now(UTC),
            )
        finally:
            await conn.close()
        return entity_event_id

    async def truncate(self) -> None:
        conn = await asyncpg.connect(self._database_url)
        try:
            await conn.execute("TRUNCATE entity_events")
        finally:
            await conn.close()

    async def add(self, entity_event_id: uuid.UUID) -> str:
        """XADD a stream:entity_event_completed message for entity_event_id; returns its id."""
        message_id: str = await self._stream.xadd(
            "stream:entity_event_completed", {"entity_event_id": str(entity_event_id), "v": "1"}
        )
        return message_id

    async def pending(self) -> dict[str, int]:
        """{stream id: delivery count} of every delivered but un-ACKed message."""
        try:
            entries = await self._stream.xpending_range(
                "stream:entity_event_completed", "sapi-aggregation", "-", "+", 100
            )
        except redis.exceptions.ResponseError:  # the group does not exist yet
            return {}
        return {entry["message_id"]: entry["times_delivered"] for entry in entries}

    async def group_created(self) -> bool:
        try:
            groups = await self._stream.xinfo_groups("stream:entity_event_completed")
        except redis.exceptions.ResponseError:  # the stream does not exist yet
            return False
        return [group["name"] for group in groups] == ["sapi-aggregation"]

    async def active_event(self, stock_code: str) -> dict[str, str]:
        """The stored fields of stock_code's active EARNINGS Event; empty if it has none."""
        fields: dict[str, str] = await self._state.hgetall(_key(stock_code))
        return fields

    async def ttl(self, stock_code: str) -> int:
        ttl_s: int = await self._state.ttl(_key(stock_code))
        return ttl_s

    async def reconnect(self) -> None:
        """Drop the probe's Redis connections, e.g. after a Redis restart."""
        await self._stream.connection_pool.disconnect()
        await self._state.connection_pool.disconnect()


def _key(stock_code: str) -> str:
    return f"agg:event:HKEX:{stock_code}:EARNINGS"


def _ids(fields: dict[str, str]) -> list[str]:
    return list(json.loads(fields["entity_event_ids"])) if fields else []


@pytest.fixture
async def probe(infra: Infra) -> AsyncIterator[AggregationProbe]:
    stream = redis.asyncio.Redis.from_url(
        f"redis://127.0.0.1:{infra.redis_stream.port}/0", decode_responses=True
    )
    state = redis.asyncio.Redis.from_url(
        f"redis://127.0.0.1:{infra.redis_state.port}/0", decode_responses=True
    )
    await stream.flushall()
    await state.flushall()
    aggregation_probe = AggregationProbe(infra, stream, state)
    await aggregation_probe.truncate()
    yield aggregation_probe
    await stream.aclose()
    await state.aclose()


@pytest.fixture
def start_aggregation(
    start_app: Callable[..., object], hkex_urls: Callable[..., None]
) -> Callable[..., object]:
    """start_app() with test timings; the HK Stock List is never needed, so HKEX is refused."""
    hkex_urls(every=REFUSED_URL)
    return functools.partial(start_app, **AGGREGATION_ENV)


async def _wait_for(condition: Callable[[], Awaitable[bool]], timeout_s: float) -> None:
    """Poll condition until it holds; fails the test after timeout_s."""
    started = time.monotonic()
    while not await condition():
        if time.monotonic() - started > timeout_s:
            pytest.fail(f"condition not met within {timeout_s}s")
        await asyncio.sleep(POLL_INTERVAL_S)


async def _wait_merged(probe: AggregationProbe, stock_code: str, ids: list[uuid.UUID]) -> None:
    """Wait until stock_code's active Event holds exactly ids and nothing is pending."""

    async def merged() -> bool:
        fields = await probe.active_event(stock_code)
        return _ids(fields) == [str(i) for i in ids] and not await probe.pending()

    await _wait_for(merged, 10)


async def _wait_pending(probe: AggregationProbe, message_id: str) -> None:
    """Wait until message_id has been delivered and left un-ACKed."""

    async def delivered() -> bool:
        return (await probe.pending()).get(message_id) == 1

    await _wait_for(delivered, 10)


async def start_merges_into_active_event(start_aggregation, probe):
    """Two EntityEvents of one group inside the sliding window are merged, in arrival order,
    into one active Event stored with the window's TTL, and both messages are ACKed."""
    first = await probe.seed("00700", P)
    second = await probe.seed("00700", P + timedelta(hours=1))
    started = datetime.now(UTC)

    async with start_aggregation():
        await _wait_for(probe.group_created, 5)
        await probe.add(first)
        await _wait_merged(probe, "00700", [first])
        event_id = (await probe.active_event("00700"))["event_id"]
        await probe.add(second)
        await _wait_merged(probe, "00700", [first, second])

        fields = await probe.active_event("00700")
        ttl_s = await probe.ttl("00700")

    assert set(fields) == {
        "event_id",
        "first_seen_at",
        "last_seen_at",
        "aggregation_updated_at",
        "entity_event_ids",
    }
    assert fields["event_id"] == event_id
    uuid.UUID(fields["event_id"])
    assert datetime.fromisoformat(fields["first_seen_at"]) == P
    assert datetime.fromisoformat(fields["last_seen_at"]) == P + timedelta(hours=1)
    assert started <= datetime.fromisoformat(fields["aggregation_updated_at"]) <= datetime.now(UTC)
    assert _ids(fields) == [str(first), str(second)]
    assert WINDOW_TTL_S - 10 < ttl_s <= WINDOW_TTL_S
    assert await probe.pending() == {}


async def start_postgres_down_left_pending(start_aggregation, probe, infra):
    """With Postgres down, the EntityEvent can't be read: its message is left pending and the
    layer keeps running. Once Postgres is back, a redelivery is merged."""
    entity_event_id = await probe.seed("00700", P)

    async with start_aggregation():
        await _wait_for(probe.group_created, 5)
        layer_task = layer_tasks.get_layer_tasks()["aggregation"]
        infra.postgres.stop()
        failed_id = await probe.add(entity_event_id)
        await _wait_pending(probe, failed_id)
        await asyncio.sleep(SETTLE_S)

        assert await probe.active_event("00700") == {}
        assert not layer_task.done()

        infra.postgres.restore()
        redelivered_id = await probe.add(entity_event_id)

        async def merged() -> bool:
            return _ids(await probe.active_event("00700")) == [str(entity_event_id)]

        await _wait_for(merged, 10)
        await asyncio.sleep(SETTLE_S)
        pending = await probe.pending()

    assert pending == {failed_id: 1}
    assert redelivered_id not in pending
    assert layer_tasks.get_layer_tasks()["aggregation"] is layer_task


async def start_state_redis_down_left_pending(start_aggregation, probe, infra):
    """With the state Redis down, a merge fails inside its group: the message is left pending,
    and the group and the layer keep running. Once the state Redis is back, a redelivery is
    merged into the same active Event."""
    first = await probe.seed("00700", P)
    second = await probe.seed("00700", P + timedelta(hours=1))

    async with start_aggregation():
        await _wait_for(probe.group_created, 5)
        await probe.add(first)
        await _wait_merged(probe, "00700", [first])
        layer_task = layer_tasks.get_layer_tasks()["aggregation"]

        infra.redis_state.stop()
        failed_id = await probe.add(second)
        await _wait_pending(probe, failed_id)
        await asyncio.sleep(SETTLE_S)
        assert not layer_task.done()

        # A graceful stop snapshots the state Redis, so the active Event survives the restart
        # and the redelivery merges into it.
        infra.redis_state.restore()
        await probe.reconnect()
        redelivered_id = await probe.add(second)

        async def merged() -> bool:
            return _ids(await probe.active_event("00700")) == [str(first), str(second)]

        await _wait_for(merged, 10)
        await asyncio.sleep(SETTLE_S)
        pending = await probe.pending()

    assert pending == {failed_id: 1}
    assert redelivered_id not in pending
    assert layer_tasks.get_layer_tasks()["aggregation"] is layer_task


async def start_stream_redis_outage_recovers(start_aggregation, probe, infra):
    """While the stream Redis is down the layer keeps running; once it is back, a new message
    is merged and ACKed on the same layer Task."""
    entity_event_id = await probe.seed("00700", P)

    async with start_aggregation():
        await _wait_for(probe.group_created, 5)
        layer_task = layer_tasks.get_layer_tasks()["aggregation"]
        infra.redis_stream.stop()
        await asyncio.sleep(3)

        assert not layer_task.done()

        infra.redis_stream.restore()
        await probe.reconnect()
        await probe.add(entity_event_id)
        await _wait_merged(probe, "00700", [entity_event_id])

        assert layer_tasks.get_layer_tasks()["aggregation"] is layer_task


def _group_tasks() -> list[asyncio.Task[object]]:
    """The running Tasks of every live group."""
    return [
        task
        for task in asyncio.all_tasks()
        if getattr(task.get_coro(), "__qualname__", "") == "GroupCoroutine._run"
    ]


async def shutdown_stops_layer_and_groups(start_aggregation, probe):
    """Shutdown stops the layer Task and every live group before the clients close, so
    nothing fails against a closed client."""
    tencent = await probe.seed("00700", P)
    hsbc = await probe.seed("00005", P)

    with structlog.testing.capture_logs() as logs:
        async with start_aggregation():
            await _wait_for(probe.group_created, 5)
            await probe.add(tencent)
            await probe.add(hsbc)
            await _wait_merged(probe, "00700", [tencent])
            await _wait_merged(probe, "00005", [hsbc])
            layer_task = layer_tasks.get_layer_tasks()["aggregation"]
            groups = _group_tasks()

            assert len(groups) == 2

    assert layer_task.cancelled()
    assert all(group.cancelled() for group in groups)
    # HKEX is refused on purpose, so the startup load's CRITICAL is expected.
    assert not [
        log
        for log in logs
        if log["log_level"] in ("error", "critical")
        and log["event"] != "hk_stock_list_startup_load_failed"
    ]
