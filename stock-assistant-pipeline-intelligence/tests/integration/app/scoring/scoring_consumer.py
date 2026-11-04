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

from app import layer_tasks
from tests.integration.conftest import REFUSED_URL, Infra

pytestmark = [pytest.mark.live, pytest.mark.timeout(120)]

SCORING_ENV = {
    "SCORING_BATCH_TIMEOUT_MS": "200",
    "REDIS_RECONNECT_INTERVAL_S": "1",
    "TOTAL_ACTIVE_SOURCES_DEFAULT": "4",
}
POLL_INTERVAL_S = 0.05
# A fixed aggregation time for every seeded Event.
T0 = datetime(2026, 9, 8, 3, 0, tzinfo=UTC)
# The reference Event E1's constituents: (source_name, sentiment_label, sentiment_score).
E1_CONSTITUENTS = [("HKEX", "POSITIVE", 0.8), ("MINGPAO", "NEGATIVE", 0.4)]
_INSERT_ENTITY_EVENT_SQL = """
    INSERT INTO entity_events (
        entity_event_id, exchange, source_url, source_name, published_at, stock_code,
        event_type_primary, sentiment_label, sentiment_score, headline, entity_summary,
        additional_outcome, metadata, created_at, updated_at
    )
    VALUES ($1, 'HKEX', $2, $6, $3, '00700', 'EARNINGS', $7, $8, '業績超預期',
            '季度收入按年增長。', $4, $5, $3, $3)
"""
_INSERT_EVENT_SQL = """
    INSERT INTO events (
        event_id, exchange, stock_code, event_type_primary, event_type_secondary, source_list,
        first_seen_at, last_seen_at, brief_summary, aggregation_updated_at,
        published_aggregation_updated_at, updated_at, created_at
    )
    VALUES ($1, 'HKEX', '00700', 'EARNINGS', '[]'::jsonb, $2, $3, $4, NULL, $5, $5, $5, $5)
"""
_INSERT_EVENT_ENTITY_MAP_SQL = """
    INSERT INTO event_entity_map (event_id, entity_event_id, created_at) VALUES ($1, $2, $3)
"""
_ADDITIONAL_OUTCOME = {
    "version": "v1.0.0",
    "output": {
        "company_name_zh": "騰訊控股",
        "company_name_en": "TENCENT",
        "event_type_secondary": None,
        "headline": "業績超預期",
        "entity_summary": "季度收入按年增長。",
    },
}


class ScoringProbe:
    """The test's own view of SAPI's storage: Events seeded as Aggregation writes them,
    stream:event_aggregated messages in, pending messages out. Database queries use a fresh
    connection each, so they survive a Postgres restart."""

    def __init__(self, infra: Infra, stream: redis.asyncio.Redis):
        self._database_url = infra.env()["DATABASE_URL"]
        self._stream = stream

    async def seed(
        self,
        constituents: list[tuple[str, str, float]] | None = None,
    ) -> uuid.UUID:
        """Store an Event of two EntityEvents, by default both MINGPAO POSITIVE 0.8, with its
        entity_events, events and event_entity_map rows; returns its event_id."""
        constituents = constituents or [("MINGPAO", "POSITIVE", 0.8)] * 2
        event_id = uuid.uuid4()
        published = [T0 - timedelta(hours=1), T0]
        entity_event_ids = [uuid.uuid4() for _ in published]
        # One entry per source, from its most recent report, as Aggregation writes it.
        latest = {
            source_name: (entity_event_id, published_at)
            for entity_event_id, published_at, (source_name, _, _) in zip(
                entity_event_ids, published, constituents, strict=True
            )
        }
        source_list = [
            {
                "source_name": source_name,
                "url": f"https://example.com/{entity_event_id}",
                "published_at": published_at.isoformat(),
            }
            for source_name, (entity_event_id, published_at) in sorted(latest.items())
        ]
        conn = await asyncpg.connect(self._database_url)
        try:
            async with conn.transaction():
                for entity_event_id, published_at, (source_name, label, score) in zip(
                    entity_event_ids, published, constituents, strict=True
                ):
                    await conn.execute(
                        _INSERT_ENTITY_EVENT_SQL,
                        entity_event_id,
                        f"https://example.com/{entity_event_id}",
                        published_at,
                        json.dumps(_ADDITIONAL_OUTCOME, ensure_ascii=False),
                        json.dumps({"cleaned_id": str(uuid.uuid4())}),
                        source_name,
                        label,
                        score,
                    )
                await conn.execute(
                    _INSERT_EVENT_SQL,
                    event_id,
                    json.dumps(source_list),
                    published[0],
                    published[-1],
                    T0,
                )
                for entity_event_id in entity_event_ids:
                    await conn.execute(_INSERT_EVENT_ENTITY_MAP_SQL, event_id, entity_event_id, T0)
        finally:
            await conn.close()
        return event_id

    async def reaggregate(self, event_id: uuid.UUID, aggregation_updated_at: datetime) -> None:
        """Move the stored Event to a new merge state, as a later Aggregation flush does."""
        conn = await asyncpg.connect(self._database_url)
        try:
            await conn.execute(
                "UPDATE events SET aggregation_updated_at = $2 WHERE event_id = $1",
                event_id,
                aggregation_updated_at,
            )
        finally:
            await conn.close()

    async def scores(self, event_id: uuid.UUID) -> list[asyncpg.Record]:
        """The Event's event_scores rows."""
        conn = await asyncpg.connect(self._database_url)
        try:
            return await conn.fetch("SELECT * FROM event_scores WHERE event_id = $1", event_id)
        finally:
            await conn.close()

    async def scored(self) -> list[dict[str, str]]:
        """The fields of every stream:event_scored entry, oldest first."""
        return [fields for _, fields in await self._stream.xrange("stream:event_scored")]

    async def truncate(self) -> None:
        conn = await asyncpg.connect(self._database_url)
        try:
            await conn.execute("TRUNCATE event_scores, event_entity_map, events, entity_events")
        finally:
            await conn.close()

    async def add(self, event_id: uuid.UUID) -> str:
        """XADD a stream:event_aggregated message for event_id; returns its id."""
        message_id: str = await self._stream.xadd(
            "stream:event_aggregated", {"event_id": str(event_id), "v": "1"}
        )
        return message_id

    async def pending(self) -> dict[str, int]:
        """{stream id: delivery count} of every delivered but un-ACKed message."""
        try:
            entries = await self._stream.xpending_range(
                "stream:event_aggregated", "sapi-scoring", "-", "+", 100
            )
        except redis.exceptions.ResponseError:  # the group does not exist yet
            return {}
        return {entry["message_id"]: entry["times_delivered"] for entry in entries}

    async def delivered(self) -> int:
        """How many messages the sapi-scoring group has read so far."""
        groups = await self._stream.xinfo_groups("stream:event_aggregated")
        return next(
            int(group["entries-read"] or 0) for group in groups if group["name"] == "sapi-scoring"
        )

    async def group_created(self) -> bool:
        try:
            groups = await self._stream.xinfo_groups("stream:event_aggregated")
        except redis.exceptions.ResponseError:  # the stream does not exist yet
            return False
        return [group["name"] for group in groups] == ["sapi-scoring"]

    async def reconnect(self) -> None:
        """Drop the probe's Redis connections, e.g. after a Redis restart."""
        await self._stream.connection_pool.disconnect()


@pytest.fixture
async def probe(infra: Infra) -> AsyncIterator[ScoringProbe]:
    stream = redis.asyncio.Redis.from_url(
        f"redis://127.0.0.1:{infra.redis_stream.port}/0", decode_responses=True
    )
    await stream.flushall()
    scoring_probe = ScoringProbe(infra, stream)
    await scoring_probe.truncate()
    yield scoring_probe
    await stream.aclose()


@pytest.fixture
def start_scoring(
    start_app: Callable[..., object], hkex_urls: Callable[..., None]
) -> Callable[..., object]:
    """start_app() with test timings; the HK Stock List is never needed, so HKEX is refused."""
    hkex_urls(every=REFUSED_URL)
    return functools.partial(start_app, **SCORING_ENV)


async def _wait_for(condition: Callable[[], Awaitable[bool]], timeout_s: float) -> None:
    """Poll condition until it holds; fails the test after timeout_s."""
    started = time.monotonic()
    while not await condition():
        if time.monotonic() - started > timeout_s:
            pytest.fail(f"condition not met within {timeout_s}s")
        await asyncio.sleep(POLL_INTERVAL_S)


async def _wait_acked(probe: ScoringProbe, count: int) -> None:
    """Wait until the group has read count messages and none of them is pending."""

    async def acked() -> bool:
        return await probe.delivered() == count and not await probe.pending()

    await _wait_for(acked, 10)


async def start_acks_loaded_event(start_scoring, probe):
    """Starting with no stream:event_aggregated, the layer creates its group; a message for a
    stored Event is ACKed."""
    event_id = await probe.seed()

    async with start_scoring():
        await _wait_for(probe.group_created, 5)
        await probe.add(event_id)
        await _wait_acked(probe, 1)


async def start_acks_missing_event(start_scoring, probe):
    """A message for an Event with no events row is ACKed, and the next one is scored as
    usual."""
    event_id = await probe.seed()

    async with start_scoring():
        await _wait_for(probe.group_created, 5)
        await probe.add(uuid.uuid4())
        await _wait_acked(probe, 1)
        await probe.add(event_id)
        await _wait_acked(probe, 2)


async def start_postgres_down_left_pending(start_scoring, probe, infra):
    """With Postgres down, the Event can't be loaded: its message is left pending and the layer
    keeps running. Once Postgres is back, a new message is ACKed without a restart."""
    failed_event_id = await probe.seed()
    event_id = await probe.seed()

    async with start_scoring():
        await _wait_for(probe.group_created, 5)
        layer_task = layer_tasks.get_layer_tasks()["scoring"]
        infra.postgres.stop()
        failed_id = await probe.add(failed_event_id)
        await asyncio.sleep(3)

        assert await probe.pending() == {failed_id: 1}
        assert not layer_task.done()

        infra.postgres.restore()
        await probe.add(event_id)

        async def only_failed_pending() -> bool:
            return await probe.delivered() == 2 and await probe.pending() == {failed_id: 1}

        await _wait_for(only_failed_pending, 10)
        assert layer_tasks.get_layer_tasks()["scoring"] is layer_task
        assert not layer_task.done()


async def start_stream_redis_outage_recovers(start_scoring, probe, infra):
    """While the stream Redis is down the layer keeps running; once it is back, a new message
    is ACKed on the same layer Task."""
    event_id = await probe.seed()

    async with start_scoring():
        await _wait_for(probe.group_created, 5)
        layer_task = layer_tasks.get_layer_tasks()["scoring"]
        infra.redis_stream.stop()
        await asyncio.sleep(4)

        assert not layer_task.done()

        infra.redis_stream.restore()
        await probe.reconnect()
        await probe.add(event_id)
        await _wait_acked(probe, 1)

        assert layer_tasks.get_layer_tasks()["scoring"] is layer_task
        assert not layer_task.done()


async def _wait_scored(probe: ScoringProbe, event_id: uuid.UUID, count: int) -> asyncpg.Record:
    """Wait until stream:event_scored holds count entries and the stream:event_aggregated
    messages are all ACKed; returns the Event's one event_scores row."""

    async def announced() -> bool:
        return len(await probe.scored()) == count and not await probe.pending()

    await _wait_for(announced, 10)
    rows = await probe.scores(event_id)
    assert len(rows) == 1
    return rows[0]


async def start_scores_fresh_event(start_scoring, probe):
    """
    E1 (EARNINGS, HKEX POSITIVE 0.8, MINGPAO NEGATIVE 0.4) with the tier weights HKEX 9.0 and
    MINGPAO 6.0 and 4 active sources: event type 9.0, authority 9.0, strength 6.4, heat 5.0, so
    base 6.41; the weighted sentiment 0.32 gives direction 1 and a final score 6.41 * 0.67.
    """
    event_id = await probe.seed(E1_CONSTITUENTS)

    async with start_scoring():
        await _wait_for(probe.group_created, 5)
        await probe.add(event_id)
        row = await _wait_scored(probe, event_id, 1)

    assert row["base_rule_score"] == pytest.approx(6.41, abs=1e-6)
    assert row["abs_final_score"] == pytest.approx(4.2947, abs=1e-6)
    assert row["stock_impact_score"] == 0.0
    assert row["direction"] == 1
    assert row["llm_fallback"] is True
    assert row["scored_aggregation_updated_at"] == T0
    rule_score_detail = json.loads(row["rule_score_detail"])
    assert rule_score_detail["version"] == "v1.0.0"
    assert rule_score_detail["output"] == {
        "event_type_score": pytest.approx(9.0, abs=1e-6),
        "source_authority_score": pytest.approx(9.0, abs=1e-6),
        "sentiment_strength_score": pytest.approx(6.4, abs=1e-6),
        "source_heat_score": pytest.approx(5.0, abs=1e-6),
    }
    assert json.loads(row["llm_score_detail"]) == {
        "version": "none",
        "output": {"stock_impact_score": 0.0, "adjustment_reason": ""},
    }
    assert await probe.scored() == [{"event_id": str(event_id), "v": "1"}]
    assert await probe.pending() == {}


async def start_rescore_keeps_one_row(start_scoring, probe):
    """Re-scoring an Event after a new merge state updates its one row in place."""
    event_id = await probe.seed(E1_CONSTITUENTS)
    t1 = T0 + timedelta(minutes=5)

    async with start_scoring():
        await _wait_for(probe.group_created, 5)
        await probe.add(event_id)
        first = await _wait_scored(probe, event_id, 1)
        await probe.reaggregate(event_id, t1)
        await probe.add(event_id)
        second = await _wait_scored(probe, event_id, 2)

    assert second["score_id"] == first["score_id"]
    assert second["created_at"] == first["created_at"]
    assert second["scored_at"] > first["scored_at"]
    assert second["scored_aggregation_updated_at"] == t1
    assert await probe.scored() == [{"event_id": str(event_id), "v": "1"}] * 2
    assert await probe.pending() == {}
