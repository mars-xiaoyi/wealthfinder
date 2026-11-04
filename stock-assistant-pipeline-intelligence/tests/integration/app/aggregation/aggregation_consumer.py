import asyncio
import contextlib
import functools
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
import redis.asyncio
import redis.exceptions
import structlog.testing

from app import layer_tasks
from app.models.entity_event import EventTypeEnum
from app.models.event import Event
from app.persistent import event_repository as event_repository_module
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
    VALUES ($1, 'HKEX', $2, $8, $3, $4, 'EARNINGS', 'POSITIVE', 0.8, '業績超預期',
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

    async def seed(
        self,
        stock_code: str,
        published_at: datetime | None,
        source_name: str = "MINGPAO",
        event_type_secondary: str | None = None,
        created_at: datetime | None = None,
    ) -> uuid.UUID:
        """Store an entity_events row for an EARNINGS event of stock_code; returns its id. Its
        source_url is _url(id)."""
        entity_event_id = uuid.uuid4()
        zh, en = _COMPANY_NAMES[stock_code]
        additional_outcome = {
            "version": "v1.0.0",
            "output": {
                "company_name_zh": zh,
                "company_name_en": en,
                "event_type_secondary": event_type_secondary,
                "headline": "業績超預期",
                "entity_summary": "季度收入按年增長。",
            },
        }
        conn = await asyncpg.connect(self._database_url)
        try:
            await conn.execute(
                _INSERT_SQL,
                entity_event_id,
                _url(entity_event_id),
                published_at,
                stock_code,
                json.dumps(additional_outcome, ensure_ascii=False),
                json.dumps({"cleaned_id": str(uuid.uuid4())}),
                created_at or datetime.now(UTC),
                source_name,
            )
        finally:
            await conn.close()
        return entity_event_id

    async def truncate(self) -> None:
        conn = await asyncpg.connect(self._database_url)
        try:
            await conn.execute("TRUNCATE event_scores, event_entity_map, events, entity_events")
        finally:
            await conn.close()

    async def add(self, entity_event_id: uuid.UUID) -> str:
        """XADD a stream:entity_event_completed message for entity_event_id; returns its id."""
        message_id: str = await self._stream.xadd(
            "stream:entity_event_completed", {"entity_event_id": str(entity_event_id), "v": "1"}
        )
        return message_id

    async def add_fields(self, fields: dict[str, str]) -> str:
        """XADD a stream:entity_event_completed message with exactly these fields; returns its
        id."""
        message_id: str = await self._stream.xadd("stream:entity_event_completed", fields)
        return message_id

    async def hold_as_ghost(self, *messages: dict[str, str]) -> list[str]:
        """XADD messages and read them as consumer ghost, so they are left pending the way a
        consumer that died would leave them (delivery count 1). Call before the app starts.
        Returns their stream ids."""
        with contextlib.suppress(redis.exceptions.ResponseError):  # the group already exists
            await self._stream.xgroup_create(
                "stream:entity_event_completed", "sapi-aggregation", id="0", mkstream=True
            )
        message_ids = [await self.add_fields(fields) for fields in messages]
        await self._stream.xreadgroup(
            "sapi-aggregation", "ghost", {"stream:entity_event_completed": ">"}
        )
        return message_ids

    async def dead_letters(self) -> list[dict[str, str]]:
        """The fields of every stream:aggregation_dead_letter entry, oldest first."""
        entries = await self._stream.xrange("stream:aggregation_dead_letter")
        return [fields for _id, fields in entries]

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

    async def execute(self, query: str, *args: object) -> None:
        conn = await asyncpg.connect(self._database_url)
        try:
            await conn.execute(query, *args)
        finally:
            await conn.close()

    async def events(self) -> dict[str, dict[str, object]]:
        """Every events row by event_id, JSONB columns decoded."""
        conn = await asyncpg.connect(self._database_url)
        try:
            rows = await conn.fetch("SELECT * FROM events")
        finally:
            await conn.close()
        decoded = {}
        for row in rows:
            fields = dict(row)
            for column in ("event_type_secondary", "source_list", "brief_summary"):
                if fields[column] is not None:
                    fields[column] = json.loads(fields[column])
            decoded[str(row["event_id"])] = fields
        return decoded

    async def memberships(self) -> dict[str, list[str]]:
        """{event_id: its entity_event_ids, sorted} from event_entity_map."""
        conn = await asyncpg.connect(self._database_url)
        try:
            rows = await conn.fetch("SELECT event_id, entity_event_id FROM event_entity_map")
        finally:
            await conn.close()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(str(row["event_id"]), []).append(str(row["entity_event_id"]))
        return {event_id: sorted(ids) for event_id, ids in result.items()}

    async def announced(self) -> list[dict[str, str]]:
        """The fields of every stream:event_aggregated message, oldest first."""
        entries = await self._stream.xrange("stream:event_aggregated")
        return [fields for _id, fields in entries]

    async def set_active_event(self, stock_code: str, fields: dict[str, str]) -> None:
        await self._state.hset(_key(stock_code), mapping=fields)
        await self._state.expire(_key(stock_code), 3600)

    async def reconnect(self) -> None:
        """Drop the probe's Redis connections, e.g. after a Redis restart."""
        await self._stream.connection_pool.disconnect()
        await self._state.connection_pool.disconnect()


def _url(entity_event_id: uuid.UUID) -> str:
    return f"https://example.com/{entity_event_id}"


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


async def _wait_announced(probe: AggregationProbe, count: int, timeout_s: float = 10) -> None:
    """Wait until stream:event_aggregated holds count messages."""

    async def announced() -> bool:
        return len(await probe.announced()) == count

    await _wait_for(announced, timeout_s)


def _sources(row: dict[str, object]) -> list[tuple[str, str, datetime]]:
    source_list = row["source_list"]
    assert isinstance(source_list, list)
    return [
        (source["source_name"], source["url"], datetime.fromisoformat(source["published_at"]))
        for source in source_list
    ]


async def start_flush_persists_and_announces(start_aggregation, probe):
    """The drained stream's flush persists the active Event with its memberships, secondary
    types and one source entry per source from its freshest report, and announces it once. A
    row without a publication time is dated by when it was stored."""
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    e1 = await probe.seed("00700", base, "HKEX", "DIVIDEND")
    e2 = await probe.seed("00700", base + timedelta(minutes=10), "HKEX", "DIVIDEND")
    e3 = await probe.seed("00700", base + timedelta(minutes=20), "MINGPAO", "BUYBACK")
    e4 = await probe.seed("00700", base + timedelta(minutes=30), "AASTOCKS", "MA")
    e5 = await probe.seed("00700", None, "MINGPAO", None, created_at=base + timedelta(minutes=40))
    for entity_event_id in (e1, e2, e3, e4, e5):
        await probe.add(entity_event_id)

    async with start_aggregation():
        await _wait_announced(probe, 1)
        await asyncio.sleep(SETTLE_S * 2)
        fields = await probe.active_event("00700")
        events = await probe.events()
        memberships = await probe.memberships()
        announced = await probe.announced()

    event_id = fields["event_id"]
    assert list(events) == [event_id]
    row = events[event_id]
    assert (row["exchange"], row["stock_code"], row["event_type_primary"]) == (
        "HKEX",
        "00700",
        "EARNINGS",
    )
    assert row["first_seen_at"] == base
    assert row["last_seen_at"] == base + timedelta(minutes=40)
    assert row["aggregation_updated_at"] == datetime.fromisoformat(fields["aggregation_updated_at"])
    assert row["published_aggregation_updated_at"] == row["aggregation_updated_at"]
    # DIVIDEND is reported twice; BUYBACK and MA once each, and MA more recently.
    assert row["event_type_secondary"] == ["DIVIDEND", "MA"]
    assert _sources(row) == [
        ("AASTOCKS", _url(e4), base + timedelta(minutes=30)),
        ("HKEX", _url(e2), base + timedelta(minutes=10)),
        ("MINGPAO", _url(e5), base + timedelta(minutes=40)),
    ]
    assert row["brief_summary"] is None
    assert isinstance(row["created_at"], datetime)
    assert isinstance(row["updated_at"], datetime)
    assert memberships == {event_id: sorted(str(i) for i in (e1, e2, e3, e4, e5))}
    assert announced == [{"event_id": event_id, "v": "1"}]


async def start_flush_only_changed(start_aggregation, probe):
    """Later flushes leave unchanged Events as stored and unannounced. A merge into one Event
    rewrites only that Event, keeping its created_at, and announces it again."""
    tencent = await probe.seed("00700", P)
    hsbc = await probe.seed("00005", P)
    await probe.add(tencent)
    await probe.add(hsbc)

    async with start_aggregation():
        await _wait_announced(probe, 2)
        before = await probe.events()
        await asyncio.sleep(SETTLE_S * 2)
        settled = await probe.events()
        settled_announced = await probe.announced()
        tencent_id = (await probe.active_event("00700"))["event_id"]
        hsbc_id = (await probe.active_event("00005"))["event_id"]

        merged = await probe.seed("00700", P + timedelta(hours=1))
        await probe.add(merged)
        await _wait_announced(probe, 3)
        await asyncio.sleep(SETTLE_S * 2)
        after = await probe.events()
        memberships = await probe.memberships()
        announced = await probe.announced()

    assert settled == before
    assert len(settled_announced) == 2
    tencent_before, tencent_after = before[tencent_id], after[tencent_id]
    assert tencent_after["aggregation_updated_at"] > tencent_before["aggregation_updated_at"]
    assert tencent_after["updated_at"] > tencent_before["updated_at"]
    assert tencent_after["created_at"] == tencent_before["created_at"]
    assert tencent_after["last_seen_at"] == P + timedelta(hours=1)
    assert (
        tencent_after["published_aggregation_updated_at"]
        == (tencent_after["aggregation_updated_at"])
    )
    assert after[hsbc_id] == before[hsbc_id]
    assert memberships == {
        tencent_id: sorted([str(tencent), str(merged)]),
        hsbc_id: [str(hsbc)],
    }
    assert announced[2] == {"event_id": tencent_id, "v": "1"}
    assert [message["event_id"] for message in announced].count(hsbc_id) == 1


async def start_replaced_event_persisted_first(start_aggregation, probe):
    """A replaced Event is persisted and announced before the new Event takes its place,
    without waiting for a flush; stopping the layer then flushes the new one."""
    first = await probe.seed("00700", P)
    # Outside the 4 h sliding window.
    second = await probe.seed("00700", P + timedelta(hours=5))
    await probe.add(first)
    await probe.add(second)

    # Long enough that no drained-stream or interval flush runs during the test.
    async with start_aggregation(AGG_BATCH_TIMEOUT_MS="5000", AGG_DB_FLUSH_INTERVAL_S="60"):
        await _wait_announced(probe, 1, timeout_s=3)
        await _wait_merged(probe, "00700", [second])
        events = await probe.events()
        memberships = await probe.memberships()
        fields = await probe.active_event("00700")

    replaced_id = next(iter(events))
    assert list(events) == [replaced_id]
    assert events[replaced_id]["first_seen_at"] == events[replaced_id]["last_seen_at"] == P
    assert (
        events[replaced_id]["published_aggregation_updated_at"]
        == (events[replaced_id]["aggregation_updated_at"])
    )
    assert memberships == {replaced_id: [str(first)]}
    assert fields["event_id"] != replaced_id
    assert _ids(fields) == [str(second)]
    after_stop = await probe.events()
    assert set(after_stop) == {replaced_id, fields["event_id"]}
    assert await probe.memberships() == {
        replaced_id: [str(first)],
        fields["event_id"]: [str(second)],
    }
    assert await probe.announced() == [
        {"event_id": replaced_id, "v": "1"},
        {"event_id": fields["event_id"], "v": "1"},
    ]


_INSERT_EVENT_SQL = """
    INSERT INTO events (
        event_id, exchange, stock_code, event_type_primary, event_type_secondary, source_list,
        first_seen_at, last_seen_at, aggregation_updated_at, updated_at, created_at
    )
    VALUES ($1, 'HKEX', $2, 'EARNINGS', '[]', '[]', $3, $3, $4, $5, $5)
"""


async def start_unannounced_version_announced_after_restart(start_aggregation, probe):
    """A version committed but never announced, as a crash between the two leaves it, is
    announced by the first flush after startup, without rewriting it. Only the active Events'
    versions are looked at, and a later restart announces nothing again."""
    entity_event_id = await probe.seed("00700", P)
    event_id, unrelated_id = uuid.uuid4(), uuid.uuid4()
    aggregation_updated_at = datetime(2026, 9, 8, 1, 0, 5, tzinfo=UTC)
    written_at = datetime(2026, 9, 8, 1, 0, 6, tzinfo=UTC)
    await probe.execute(_INSERT_EVENT_SQL, event_id, "00700", P, aggregation_updated_at, written_at)
    await probe.execute(
        "INSERT INTO event_entity_map (event_id, entity_event_id, created_at) VALUES ($1, $2, $3)",
        event_id,
        entity_event_id,
        written_at,
    )
    # No longer active, so no flush looks at it.
    await probe.execute(
        _INSERT_EVENT_SQL, unrelated_id, "00005", P, aggregation_updated_at, written_at
    )
    await probe.set_active_event(
        "00700",
        {
            "event_id": str(event_id),
            "first_seen_at": P.isoformat(),
            "last_seen_at": P.isoformat(),
            "aggregation_updated_at": aggregation_updated_at.isoformat(),
            "entity_event_ids": json.dumps([str(entity_event_id)]),
        },
    )

    async with start_aggregation():
        await _wait_announced(probe, 1)
        await asyncio.sleep(SETTLE_S * 2)
        events = await probe.events()
    async with start_aggregation():
        await _wait_for(probe.group_created, 5)
        await asyncio.sleep(SETTLE_S * 2)
        restarted_events = await probe.events()

    assert await probe.announced() == [{"event_id": str(event_id), "v": "1"}]
    assert events[str(event_id)]["published_aggregation_updated_at"] == aggregation_updated_at
    assert events[str(event_id)]["updated_at"] == written_at
    assert events[str(unrelated_id)]["published_aggregation_updated_at"] is None
    assert restarted_events == events


async def start_flush_failed_retried(start_aggregation, probe, infra):
    """A flush that fails with Postgres down writes and announces nothing, and the layer keeps
    running; once Postgres is back, a later flush persists the same active Event whole."""
    first = await probe.seed("00700", P)
    second = await probe.seed("00700", P + timedelta(hours=1))

    with structlog.testing.capture_logs() as logs:
        async with start_aggregation(AGG_BATCH_TIMEOUT_MS="5000", AGG_DB_FLUSH_INTERVAL_S="60"):
            await _wait_for(probe.group_created, 5)
            layer_task = layer_tasks.get_layer_tasks()["aggregation"]
            await probe.add(first)
            await probe.add(second)
            await _wait_merged(probe, "00700", [first, second])
            fields = await probe.active_event("00700")
            infra.postgres.stop()

            async def flush_failed() -> bool:
                return any(log["log_level"] == "error" and "exc_info" in log for log in logs)

            await _wait_for(flush_failed, 15)
            assert not layer_task.done()
            assert await probe.announced() == []

            infra.postgres.restore()
            await _wait_announced(probe, 1, timeout_s=30)
            events = await probe.events()
            memberships = await probe.memberships()
            fields_after = await probe.active_event("00700")

    assert list(events) == [fields["event_id"]]
    assert memberships == {fields["event_id"]: sorted([str(first), str(second)])}
    assert await probe.announced() == [{"event_id": fields["event_id"], "v": "1"}]
    assert fields_after == fields
    assert layer_tasks.get_layer_tasks()["aggregation"] is layer_task


async def persist_events_only_newer_state(start_aggregation, probe):
    """Against the stored row, an older snapshot of an Event writes nothing, an older
    published mark never un-marks a newer one, and a version newer than the mark reads as
    unannounced."""
    entity_event_id = await probe.seed("00700", P)
    await probe.add(entity_event_id)

    async with start_aggregation():
        await _wait_announced(probe, 1)
        fields = await probe.active_event("00700")
        event_id = uuid.UUID(fields["event_id"])
        stored_at = datetime.fromisoformat(fields["aggregation_updated_at"])
        before = (await probe.events())[str(event_id)]
        repository = event_repository_module.get_event_repository()

        older = Event(
            event_id=event_id,
            exchange="HKEX",
            stock_code="00700",
            event_type_primary=EventTypeEnum.EARNINGS,
            first_seen_at=P,
            last_seen_at=P + timedelta(hours=1),
            aggregation_updated_at=stored_at - timedelta(seconds=1),
            entity_event_ids=[entity_event_id],
        )
        written = await repository.persist_events([older])
        after_older = (await probe.events())[str(event_id)]
        await repository.mark_published(event_id, stored_at - timedelta(seconds=5))
        after_older_mark = (await probe.events())[str(event_id)]
        unannounced_before = await repository.fetch_unpublished_events([event_id])
        await probe.execute(
            "UPDATE events SET aggregation_updated_at = $2 WHERE event_id = $1",
            event_id,
            stored_at + timedelta(seconds=1),
        )
        unannounced_after = await repository.fetch_unpublished_events([event_id])

    assert written == []
    assert after_older == before
    assert after_older_mark["published_aggregation_updated_at"] == stored_at
    assert unannounced_before == []
    assert [event.event_id for event in unannounced_after] == [event_id]
    assert unannounced_after[0].aggregation_updated_at == stored_at + timedelta(seconds=1)


CLAIM_TIMEOUT_S = 1
RECLAIM_ENV = {"STREAM_CLAIM_TIMEOUT_MS": "1000", "AGG_MAX_RETRY": "3"}
# Enough retries that an outage of a few claim timeouts can't use them up.
MANY_RETRIES_ENV = {**RECLAIM_ENV, "AGG_MAX_RETRY": "20"}


def _dead_lettered(logs: list[MutableMapping[str, Any]]) -> list[tuple[object, ...]]:
    """(log_level, message_id, delivery_count, reason) of each Aggregation dead-letter
    record."""
    return [
        (log["log_level"], log["message_id"], log["delivery_count"], log["reason"])
        for log in logs
        if log["event"] == "message_dead_lettered"
        and log["dead_letter_stream"] == "stream:aggregation_dead_letter"
    ]


def _assert_dead_letter(
    fields: dict[str, str],
    message_id: str,
    original: dict[str, str],
    delivery_count: int,
    started: datetime,
) -> None:
    """fields is the dead-letter entry for message_id, whose original fields were original."""
    assert set(fields) == {"message_id", "fields", "delivery_count", "failed_at", "v"}
    assert fields["message_id"] == message_id
    assert json.loads(fields["fields"]) == original
    assert fields["delivery_count"] == str(delivery_count)
    assert fields["v"] == "1"
    failed_at = datetime.fromisoformat(fields["failed_at"])
    assert failed_at.tzinfo == UTC
    assert started <= failed_at <= datetime.now(UTC)


async def start_abandoned_message_recovered(start_aggregation, probe):
    """A message left pending by a consumer that died is claimed no sooner than the claim
    timeout, merged and ACKed."""
    entity_event_id = await probe.seed("00700", P)
    await probe.hold_as_ghost({"entity_event_id": str(entity_event_id), "v": "1"})
    started = time.monotonic()

    async with start_aggregation(**RECLAIM_ENV):
        await _wait_merged(probe, "00700", [entity_event_id])
        waited_s = time.monotonic() - started

    assert waited_s >= CLAIM_TIMEOUT_S
    assert await probe.pending() == {}
    assert await probe.dead_letters() == []


async def start_reclaimed_until_cause_clears(start_aggregation, probe, infra):
    """A message whose merge fails (here Postgres down) stays pending and is reclaimed every
    claim timeout; once the cause clears, the same message is merged and ACKed, without being
    dead-lettered."""
    entity_event_id = await probe.seed("00700", P)

    with structlog.testing.capture_logs() as logs:
        async with start_aggregation(**MANY_RETRIES_ENV):
            await _wait_for(probe.group_created, 5)
            infra.postgres.stop()
            message_id = await probe.add(entity_event_id)

            async def reclaimed() -> bool:
                return (await probe.pending()).get(message_id, 0) >= 2

            await _wait_for(reclaimed, 15)
            assert await probe.active_event("00700") == {}

            infra.postgres.restore()
            await _wait_merged(probe, "00700", [entity_event_id])

    assert await probe.dead_letters() == []
    assert _dead_lettered(logs) == []


async def start_retries_exhausted_dead_lettered(start_aggregation, probe, infra):
    """A message that keeps failing (here Postgres down) is dead-lettered with its original
    fields once delivered AGG_MAX_RETRY times, then ACKed, and is not processed again."""
    entity_event_id = await probe.seed("00700", P)
    started = datetime.now(UTC)

    with structlog.testing.capture_logs() as logs:
        async with start_aggregation(**RECLAIM_ENV):
            await _wait_for(probe.group_created, 5)
            infra.postgres.stop()
            message_id = await probe.add(entity_event_id)

            async def dead_lettered() -> bool:
                return bool(await probe.dead_letters())

            await _wait_for(dead_lettered, 20)
            await asyncio.sleep(2 * CLAIM_TIMEOUT_S)  # two more sweeps
            infra.postgres.restore()
            await asyncio.sleep(2 * CLAIM_TIMEOUT_S)
            fields = await probe.active_event("00700")

    [dead_letter] = await probe.dead_letters()
    _assert_dead_letter(
        dead_letter, message_id, {"entity_event_id": str(entity_event_id), "v": "1"}, 3, started
    )
    assert await probe.pending() == {}
    # Deliveries 1 and 2 are processed and fail; delivery 3 is dead-lettered unprocessed.
    assert len([log for log in logs if log.get("entity_event_id") == entity_event_id]) == 2
    assert _dead_lettered(logs) == [("warning", message_id, 3, "retries_exhausted")]
    assert fields == {}


@pytest.mark.parametrize(
    "reclaimed",
    [pytest.param(False, id="read_path"), pytest.param(True, id="reclaim_path")],
)
async def start_malformed_messages_dead_lettered(start_aggregation, probe, reclaimed):
    """Messages this layer can never process are dead-lettered with their original fields and
    ACKed without being processed, on either path; the valid message behind them is merged."""
    entity_event_id = await probe.seed("00700", P)
    malformed = [
        {"entity_event_id": "not-a-uuid", "v": "1"},
        {"v": "1"},
        {"entity_event_id": str(uuid.uuid4())},
        {"entity_event_id": str(uuid.uuid4()), "v": "2"},
    ]
    valid = {"entity_event_id": str(entity_event_id), "v": "1"}
    started = datetime.now(UTC)

    with structlog.testing.capture_logs() as logs:
        if reclaimed:
            *malformed_ids, _valid_id = await probe.hold_as_ghost(*malformed, valid)
            async with start_aggregation(**RECLAIM_ENV):
                await _wait_merged(probe, "00700", [entity_event_id])
        else:
            async with start_aggregation(**RECLAIM_ENV):
                await _wait_for(probe.group_created, 5)
                malformed_ids = [await probe.add_fields(fields) for fields in malformed]
                await probe.add_fields(valid)
                await _wait_merged(probe, "00700", [entity_event_id])

    delivery_count = 2 if reclaimed else 1
    dead_letters = await probe.dead_letters()
    assert len(dead_letters) == len(malformed)
    for dead_letter, message_id, original in zip(
        dead_letters, malformed_ids, malformed, strict=True
    ):
        _assert_dead_letter(dead_letter, message_id, original, delivery_count, started)
    assert _dead_lettered(logs) == [
        ("warning", message_id, delivery_count, "malformed") for message_id in malformed_ids
    ]
    assert await probe.pending() == {}
