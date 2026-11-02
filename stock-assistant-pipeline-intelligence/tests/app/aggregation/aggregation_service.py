import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
import redis.exceptions

from app.aggregation import aggregation_service as aggregation_service_module
from app.aggregation import event_aggregation_store as store_module
from app.aggregation import sliding_window
from app.aggregation.aggregation_service import AggregationService
from app.aggregation.event_aggregation_store import EventAggregationStore
from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.common.stock import Stock
from app.config import AggregationConfig
from app.db.errors import DatabaseError
from app.models.entity_event import AnalysisOutcome, EntityEvent, EventTypeEnum, SentimentEnum
from app.models.event import Event
from app.models.versioned_metadata import VersionedMetadata
from app.persistent import entity_event_repository as repository_module
from app.persistent.entity_event_repository import EntityEventRepository

# A concurrency bug shows up as a hang, so fail fast instead.
pytestmark = [pytest.mark.timeout(5)]

CONFIG = AggregationConfig(
    batch_size=10,
    batch_timeout_ms=200,
    max_concurrent=10,
    group_idle_timeout_s=60,
    db_flush_interval_s=300,
    sliding_window_hours=4,
    event_max_timespan_hours=24,
    consumer_name="pod-1",
)
# The full sliding window plus two flush intervals.
WINDOW_TTL_S = 4 * 3600 + 600
P = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)
TENCENT = Stock("00700", "HKEX", "騰訊控股", "TENCENT")
HSBC = Stock("00005", "HKEX", "匯豐控股", "HSBC HOLDINGS")
HKEX = Stock("00388", "HKEX", "香港交易所", "HKEX")
HANG_SENG = Stock("00011", "HKEX", "恒生銀行", "HANG SENG BANK")
ACTIVE_EVENT_ID = UUID("00000000-0000-0000-0000-0000000000e1")


def _id(n: int) -> UUID:
    return UUID(int=n)


def _entity_event(
    n: int,
    published_at: datetime | None,
    stock: Stock = TENCENT,
    created_at: datetime = CREATED_AT,
) -> EntityEvent:
    return EntityEvent(
        source_id=UUID(int=1000 + n),
        source_url=f"https://example.com/{n}",
        source_name="MINGPAO",
        published_at=published_at,
        analysis_outcome=VersionedMetadata(
            "v1.0.0",
            AnalysisOutcome(
                stock_code=stock.stock_code,
                event_type_primary=EventTypeEnum.EARNINGS,
                event_type_secondary=None,
                sentiment_label=SentimentEnum.POSITIVE,
                sentiment_score=0.8,
                headline="業績超預期",
                entity_summary="季度收入按年增長。",
            ),
        ),
        stock=stock,
        entity_event_id=_id(n),
        created_at=created_at,
    )


def _active_event(
    first_seen_at: datetime, last_seen_at: datetime, entity_event_ids: list[UUID]
) -> Event:
    return Event(
        event_id=ACTIVE_EVENT_ID,
        exchange="HKEX",
        stock_code="00700",
        event_type_primary=EventTypeEnum.EARNINGS,
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        aggregation_updated_at=P,
        entity_event_ids=entity_event_ids,
    )


class _FakeStore:
    """An in-memory EventAggregationStore keeping each group's active Event and its TTL."""

    def __init__(self) -> None:
        self.events: dict[tuple[str, str, str], Event] = {}
        self.ttls: dict[tuple[str, str, str], int] = {}
        self.store = AsyncMock(spec=EventAggregationStore)
        self.store.get_active_event.side_effect = self._get
        self.store.upsert_active_event.side_effect = self._upsert

    def seed(self, event: Event) -> None:
        self.events[(event.exchange, event.stock_code, event.event_type_primary)] = event

    def get(self, stock: Stock) -> Event | None:
        return self.events.get((stock.exchange, stock.stock_code, EventTypeEnum.EARNINGS))

    def ttl(self, stock: Stock) -> int | None:
        return self.ttls.get((stock.exchange, stock.stock_code, EventTypeEnum.EARNINGS))

    async def _get(self, exchange, stock_code, event_type_primary) -> Event | None:
        return self.events.get((exchange, stock_code, event_type_primary))

    async def _upsert(self, event: Event, ttl_s: int) -> None:
        key = (event.exchange, event.stock_code, event.event_type_primary)
        self.events[key] = event
        self.ttls[key] = ttl_s


@pytest.fixture(autouse=True)
def aggregation_config(monkeypatch):
    config = MagicMock()
    config.aggregation = CONFIG
    monkeypatch.setattr(sliding_window, "get_config", lambda: config)


@pytest.fixture
def store(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr(store_module, "_event_aggregation_store", fake.store)
    return fake


@pytest.fixture
def repository(monkeypatch):
    """An EntityEventRepository serving the EntityEvents a test stores in it."""
    repo = AsyncMock(spec=EntityEventRepository)
    repo.stored = {}
    repo.fetch_entity_event.side_effect = lambda entity_event_id: repo.stored.get(entity_event_id)
    monkeypatch.setattr(repository_module, "_entity_event_repository", repo)
    return repo


def _store_entity_events(repository: AsyncMock, *entity_events: EntityEvent) -> None:
    for entity_event in entity_events:
        repository.stored[entity_event.entity_event_id] = entity_event


class _Outcome:
    """The callbacks for one EntityEvent, recording which one ran."""

    def __init__(self) -> None:
        self.done = asyncio.Event()
        self.on_success = AsyncMock(side_effect=self.done.set)
        self.on_failure = MagicMock(side_effect=self.done.set)


async def _process(service: AggregationService, entity_event_id: UUID) -> _Outcome:
    """Process one EntityEvent and wait until its outcome is reported."""
    outcome = _Outcome()
    await service.process_entity_event(entity_event_id, outcome.on_success, outcome.on_failure)
    await outcome.done.wait()
    return outcome


def _assert_processed(outcome: _Outcome) -> None:
    outcome.on_success.assert_awaited_once_with()
    outcome.on_failure.assert_not_called()


@pytest.mark.parametrize(
    ("published_at", "created_at"),
    [
        pytest.param(P, CREATED_AT, id="published"),
        pytest.param(None, P, id="no_publication_time"),
    ],
)
async def process_entity_event_new_group(store, repository, published_at, created_at):
    """The first EntityEvent of a group starts its active Event, dated by its publication time
    or, without one, by when it was stored, and kept for the full sliding window plus the
    grace period."""
    _store_entity_events(repository, _entity_event(1, published_at, created_at=created_at))
    service = AggregationService(CONFIG)
    before = datetime.now(UTC)

    outcome = await _process(service, _id(1))

    _assert_processed(outcome)
    event = store.get(TENCENT)
    assert event is not None
    assert event.event_id != ACTIVE_EVENT_ID
    assert (event.exchange, event.stock_code) == ("HKEX", "00700")
    assert event.event_type_primary is EventTypeEnum.EARNINGS
    assert event.first_seen_at == event.last_seen_at == P
    assert event.entity_event_ids == [_id(1)]
    assert event.aggregation_updated_at is not None
    assert before <= event.aggregation_updated_at <= datetime.now(UTC)
    assert store.ttl(TENCENT) == WINDOW_TTL_S
    await service.close()


async def process_entity_event_merged_in_order(store, repository):
    """EntityEvents of one group inside the sliding window merge into one Event, in arrival
    order, even when they arrive out of publication order."""
    _store_entity_events(
        repository,
        _entity_event(1, P + timedelta(hours=1)),
        _entity_event(2, P),
        _entity_event(3, P + timedelta(hours=3)),
    )
    service = AggregationService(CONFIG)

    outcomes = [await _process(service, _id(n)) for n in (1, 2, 3)]

    for outcome in outcomes:
        _assert_processed(outcome)
    event = store.get(TENCENT)
    assert event is not None
    assert event.entity_event_ids == [_id(1), _id(2), _id(3)]
    assert event.first_seen_at == P
    assert event.last_seen_at == P + timedelta(hours=3)
    assert store.ttl(TENCENT) == WINDOW_TTL_S
    await service.close()


async def process_entity_event_ttl_near_max_timespan(store, repository):
    """Near the maximum timespan the merge state is kept only for the lifespan left, plus the
    grace period."""
    store.seed(_active_event(P, P + timedelta(hours=20), [_id(1)]))
    _store_entity_events(repository, _entity_event(2, P + timedelta(hours=23, minutes=30)))
    service = AggregationService(CONFIG)

    outcome = await _process(service, _id(2))

    _assert_processed(outcome)
    event = store.get(TENCENT)
    assert event is not None
    assert event.event_id == ACTIVE_EVENT_ID
    assert event.entity_event_ids == [_id(1), _id(2)]
    assert store.ttl(TENCENT) == 30 * 60 + 600
    await service.close()


async def process_entity_event_ttl_at_max_timespan(store, repository):
    """With a second of lifespan left, the merge state is still kept for the grace period, so
    it never expires before a flush."""
    store.seed(_active_event(P, P + timedelta(hours=20), [_id(1)]))
    _store_entity_events(
        repository, _entity_event(2, P + timedelta(hours=23, minutes=59, seconds=59))
    )
    service = AggregationService(CONFIG)

    outcome = await _process(service, _id(2))

    _assert_processed(outcome)
    event = store.get(TENCENT)
    assert event is not None
    assert event.event_id == ACTIVE_EVENT_ID
    assert event.entity_event_ids == [_id(1), _id(2)]
    assert event.last_seen_at == P + timedelta(hours=23, minutes=59, seconds=59)
    ttl = store.ttl(TENCENT)
    assert ttl is not None
    # The lifespan left converts to at most one second, depending on float rounding.
    assert 600 <= ttl <= 601
    await service.close()


@pytest.mark.parametrize(
    ("active_last_seen_at", "published_at"),
    [
        pytest.param(
            P + timedelta(hours=4),
            P + timedelta(hours=8, seconds=1),
            id="outside_sliding_window",
        ),
        pytest.param(P + timedelta(hours=22), P + timedelta(hours=24), id="max_timespan_reached"),
    ],
)
async def process_entity_event_new_event_replaces(
    store, repository, active_last_seen_at, published_at
):
    """An EntityEvent outside the sliding window, or one that would stretch the Event to its
    maximum timespan, starts a new active Event in its place."""
    store.seed(_active_event(P, active_last_seen_at, [_id(1)]))
    _store_entity_events(repository, _entity_event(2, published_at))
    service = AggregationService(CONFIG)

    outcome = await _process(service, _id(2))

    _assert_processed(outcome)
    event = store.get(TENCENT)
    assert event is not None
    assert event.event_id != ACTIVE_EVENT_ID
    assert event.entity_event_ids == [_id(2)]
    assert event.first_seen_at == event.last_seen_at == published_at
    await service.close()


async def process_entity_event_missing(store, repository):
    """An EntityEvent with no stored row is final as it is: nothing is merged."""
    service = AggregationService(CONFIG)

    outcome = await _process(service, _id(9))

    _assert_processed(outcome)
    assert store.events == {}
    store.store.get_active_event.assert_not_awaited()


async def process_entity_event_duplicate_skipped(store, repository):
    """An EntityEvent already merged into the active Event is not merged again."""
    active = _active_event(P, P, [_id(1)])
    store.seed(active)
    _store_entity_events(repository, _entity_event(1, P))
    service = AggregationService(CONFIG)

    outcome = await _process(service, _id(1))

    _assert_processed(outcome)
    assert store.get(TENCENT) is active
    store.store.upsert_active_event.assert_not_awaited()
    await service.close()


@pytest.mark.parametrize(
    ("published_at", "created_at"),
    [
        pytest.param(P + timedelta(hours=1), CREATED_AT, id="published"),
        pytest.param(None, P + timedelta(hours=1), id="no_publication_time"),
    ],
)
async def process_entity_event_late_skipped(store, repository, published_at, created_at):
    """An EntityEvent dated more than the sliding window before the active Event's latest one
    is skipped, leaving the active Event as it was."""
    active = _active_event(P + timedelta(hours=6), P + timedelta(hours=6), [_id(1)])
    store.seed(active)
    _store_entity_events(repository, _entity_event(2, published_at, created_at=created_at))
    service = AggregationService(CONFIG)

    outcome = await _process(service, _id(2))

    _assert_processed(outcome)
    assert store.get(TENCENT) is active
    store.store.upsert_active_event.assert_not_awaited()
    await service.close()


async def process_entity_event_sliding_window_boundary_merged(store, repository):
    """An EntityEvent dated exactly the sliding window before the active Event's latest one is
    not late: it merges."""
    store.seed(_active_event(P + timedelta(hours=4), P + timedelta(hours=4), [_id(1)]))
    _store_entity_events(repository, _entity_event(2, P))
    service = AggregationService(CONFIG)

    outcome = await _process(service, _id(2))

    _assert_processed(outcome)
    event = store.get(TENCENT)
    assert event is not None
    assert event.event_id == ACTIVE_EVENT_ID
    assert event.entity_event_ids == [_id(1), _id(2)]
    assert event.first_seen_at == P
    assert event.last_seen_at == P + timedelta(hours=4)
    assert store.ttl(TENCENT) == WINDOW_TTL_S
    await service.close()


@pytest.mark.parametrize(
    ("failure", "acked"),
    [
        pytest.param("state_redis_read", False, id="state_redis_read"),
        pytest.param("state_redis_write", False, id="state_redis_write"),
        pytest.param("invalid_active_event", False, id="invalid_active_event"),
        pytest.param("stream_redis_ack", True, id="stream_redis_ack"),
    ],
)
async def process_entity_event_merge_failed(store, repository, failure, acked):
    """A failed merge is reported through on_failure, and the group still merges its next
    EntityEvent."""
    _store_entity_events(repository, _entity_event(1, P), _entity_event(2, P))
    error = redis.exceptions.ConnectionError("down")
    failed = _Outcome()
    if failure == "state_redis_read":
        store.store.get_active_event.side_effect = [error, None]
    elif failure == "state_redis_write":
        store.store.upsert_active_event.side_effect = [error, None]
    elif failure == "invalid_active_event":
        # An active Event must hold at least one EntityEvent.
        store.store.get_active_event.side_effect = [_active_event(P, P, []), None]
    else:
        failed.on_success.side_effect = error
    service = AggregationService(CONFIG)

    await service.process_entity_event(_id(1), failed.on_success, failed.on_failure)
    await failed.done.wait()
    later = await _process(service, _id(2))

    failed.on_failure.assert_called_once_with()
    assert failed.on_success.await_count == (1 if acked else 0)
    _assert_processed(later)
    await service.close()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            ServiceUnavailableError("DB connection error", category=ErrorCategory.DB),
            id="database_unreachable",
        ),
        pytest.param(DatabaseError("DB error", category=ErrorCategory.DB), id="database"),
    ],
)
async def process_entity_event_fetch_failed(store, repository, error):
    """A failed read of the EntityEvent is raised, with nothing queued or reported."""
    repository.fetch_entity_event.side_effect = error
    service = AggregationService(CONFIG)
    outcome = _Outcome()

    with pytest.raises(type(error)):
        await service.process_entity_event(_id(1), outcome.on_success, outcome.on_failure)

    outcome.on_success.assert_not_awaited()
    outcome.on_failure.assert_not_called()
    store.store.get_active_event.assert_not_awaited()


async def process_entity_event_live_groups_bounded(store, repository):
    """At most max_concurrent groups are live at once: a new group waits until a live one has
    been idle for the idle timeout, and an idled-out group is started again by its next
    EntityEvent, merging into the same active Event."""
    config = dataclasses.replace(CONFIG, max_concurrent=2, group_idle_timeout_s=1)
    _store_entity_events(
        repository,
        _entity_event(1, P, TENCENT),
        _entity_event(2, P, HSBC),
        _entity_event(3, P, HKEX),
        _entity_event(4, P + timedelta(hours=1), TENCENT),
    )
    service = AggregationService(config)
    await _process(service, _id(1))
    await _process(service, _id(2))

    third = asyncio.create_task(_process(service, _id(3)))
    await asyncio.sleep(0.5)
    assert not third.done()
    assert store.get(HKEX) is None
    _assert_processed(await third)
    fourth = await _process(service, _id(4))

    _assert_processed(fourth)
    event = store.get(TENCENT)
    assert event is not None
    assert event.entity_event_ids == [_id(1), _id(4)]
    await service.close()


async def process_entity_event_group_started_while_waiting(store, repository):
    """Two EntityEvents of one new group waiting for a permit start the group once, and the
    permit the second one got back is free for another group."""
    config = dataclasses.replace(CONFIG, max_concurrent=2, group_idle_timeout_s=1)
    _store_entity_events(
        repository,
        _entity_event(1, P, TENCENT),
        _entity_event(2, P, HSBC),
        _entity_event(3, P, HKEX),
        _entity_event(4, P + timedelta(minutes=1), HKEX),
        _entity_event(5, P, HANG_SENG),
    )
    service = AggregationService(config)
    await _process(service, _id(1))
    await _process(service, _id(2))

    # Both live groups idle out, freeing their permits, while 3 and 4 wait for one.
    await asyncio.gather(_process(service, _id(3)), _process(service, _id(4)))
    async with asyncio.timeout(0.5):
        fifth = await _process(service, _id(5))

    _assert_processed(fifth)
    event = store.get(HKEX)
    assert event is not None
    assert event.entity_event_ids == [_id(3), _id(4)]
    await service.close()


async def close_success(store, repository):
    """Closing stops every group, abandoning a merge in progress without reporting it."""
    _store_entity_events(repository, _entity_event(1, P))
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def hang(*_args) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    store.store.get_active_event.side_effect = hang
    service = AggregationService(CONFIG)
    outcome = _Outcome()
    await service.process_entity_event(_id(1), outcome.on_success, outcome.on_failure)
    await started.wait()

    await service.close()

    assert cancelled.is_set()
    outcome.on_success.assert_not_awaited()
    outcome.on_failure.assert_not_called()


def get_aggregation_service_not_initialized(monkeypatch):
    monkeypatch.setattr(aggregation_service_module, "_aggregation_service", None)

    with pytest.raises(ServiceUnavailableError):
        aggregation_service_module.get_aggregation_service()


def create_aggregation_service_success(monkeypatch, store, repository):
    monkeypatch.setattr(aggregation_service_module, "_aggregation_service", None)

    service = aggregation_service_module.create_aggregation_service(CONFIG)

    assert aggregation_service_module.get_aggregation_service() is service
