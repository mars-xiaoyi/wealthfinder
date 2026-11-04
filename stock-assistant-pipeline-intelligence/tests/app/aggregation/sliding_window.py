import dataclasses
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.aggregation import sliding_window
from app.common.error_categories import ErrorCategory
from app.common.errors import SAPIError
from app.common.stock import Stock
from app.config import AggregationConfig
from app.models.entity_event import AnalysisOutcome, EntityEvent, EventTypeEnum, SentimentEnum
from app.models.event import Event
from app.models.versioned_metadata import VersionedMetadata

CONFIG = AggregationConfig(
    batch_size=10,
    batch_timeout_ms=5000,
    max_concurrent=100,
    group_idle_timeout_s=60,
    db_flush_interval_s=300,
    max_retry=3,
    sliding_window_hours=4,
    event_max_timespan_hours=24,
    consumer_name="sapi-aggregation-1",
)
T0 = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)
TENCENT = Stock("00700", "HKEX", "騰訊控股", "TENCENT")
ACTIVE_EVENT_ID = UUID("00000000-0000-0000-0000-0000000000e1")
ACTIVE_ENTITY_EVENT_ID = UUID("00000000-0000-0000-0000-0000000000a1")
ENTITY_EVENT_ID = UUID("00000000-0000-0000-0000-0000000000a2")
NEW_EVENT_ID = UUID("00000000-0000-0000-0000-0000000000e2")


@pytest.fixture(autouse=True)
def aggregation_config(monkeypatch):
    """Every test runs with CONFIG unless it sets its own."""
    _set_config(monkeypatch, CONFIG)


def _set_config(monkeypatch, aggregation: AggregationConfig) -> None:
    config = MagicMock()
    config.aggregation = aggregation
    monkeypatch.setattr(sliding_window, "get_config", lambda: config)


@pytest.fixture(autouse=True)
def fixed_event_id(monkeypatch):
    """Every new Event gets NEW_EVENT_ID, so tests can assert it."""
    monkeypatch.setattr(sliding_window, "uuid4", lambda: NEW_EVENT_ID)


def _entity_event(
    published_at: datetime | None,
    entity_event_id: UUID | None = ENTITY_EVENT_ID,
    created_at: datetime | None = CREATED_AT,
) -> EntityEvent:
    return EntityEvent(
        source_id=UUID("00000000-0000-0000-0000-00000000000c"),
        source_url="https://example.com/a",
        source_name="MINGPAO",
        published_at=published_at,
        analysis_outcome=VersionedMetadata(
            "v1.0.0",
            AnalysisOutcome(
                stock_code="00700",
                event_type_primary=EventTypeEnum.EARNINGS,
                event_type_secondary=None,
                sentiment_label=SentimentEnum.POSITIVE,
                sentiment_score=0.8,
                headline="業績超預期",
                entity_summary="季度收入按年增長。",
            ),
        ),
        stock=TENCENT,
        entity_event_id=entity_event_id,
        created_at=created_at,
    )


def _active_event(first_seen_at: datetime, last_seen_at: datetime) -> Event:
    return Event(
        event_id=ACTIVE_EVENT_ID,
        exchange="HKEX",
        stock_code="00700",
        event_type_primary=EventTypeEnum.EARNINGS,
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        aggregation_updated_at=T0,
        entity_event_ids=[ACTIVE_ENTITY_EVENT_ID],
    )


def _assert_merged(
    result: Event, first_seen_at: datetime, last_seen_at: datetime, before: datetime
) -> None:
    assert result.event_id == ACTIVE_EVENT_ID
    assert result.exchange == "HKEX"
    assert result.stock_code == "00700"
    assert result.event_type_primary is EventTypeEnum.EARNINGS
    assert result.first_seen_at == first_seen_at
    assert result.last_seen_at == last_seen_at
    assert result.aggregation_updated_at is not None
    assert before <= result.aggregation_updated_at <= datetime.now(UTC)
    assert result.entity_event_ids == [ACTIVE_ENTITY_EVENT_ID, ENTITY_EVENT_ID]
    assert result.event_type_secondary is None
    assert result.source_list is None
    assert result.updated_at is None
    assert result.created_at is None


def _assert_new(result: Event, published_at: datetime, before: datetime) -> None:
    assert result.event_id == NEW_EVENT_ID
    assert result.exchange == "HKEX"
    assert result.stock_code == "00700"
    assert result.event_type_primary is EventTypeEnum.EARNINGS
    assert result.first_seen_at == published_at
    assert result.last_seen_at == published_at
    assert result.aggregation_updated_at is not None
    assert before <= result.aggregation_updated_at <= datetime.now(UTC)
    assert result.entity_event_ids == [ENTITY_EVENT_ID]
    assert result.event_type_secondary is None
    assert result.source_list is None
    assert result.updated_at is None
    assert result.created_at is None


def merge_or_create_no_active_event():
    """No active Event: a new Event starts from the EntityEvent."""
    before = datetime.now(UTC)

    result = sliding_window.merge_or_create(None, _entity_event(T0))

    _assert_new(result, T0, before)


@pytest.mark.parametrize(
    ("gap", "merged"),
    [
        pytest.param(timedelta(hours=4) - timedelta(seconds=1), True, id="just_inside"),
        pytest.param(timedelta(hours=4), True, id="exactly_at"),
        pytest.param(timedelta(hours=4) + timedelta(seconds=1), False, id="just_outside"),
    ],
)
def merge_or_create_sliding_window(gap, merged):
    published_at = T0 + gap
    before = datetime.now(UTC)

    result = sliding_window.merge_or_create(_active_event(T0, T0), _entity_event(published_at))

    if merged:
        _assert_merged(result, T0, published_at, before)
    else:
        _assert_new(result, published_at, before)


@pytest.mark.parametrize(
    ("span", "merged"),
    [
        pytest.param(timedelta(hours=24) - timedelta(seconds=1), True, id="just_inside"),
        pytest.param(timedelta(hours=24), False, id="exactly_at"),
        pytest.param(timedelta(hours=24) + timedelta(seconds=1), False, id="just_outside"),
    ],
)
def merge_or_create_max_timespan(span, merged):
    """The EntityEvent is inside the sliding window; only the Event's total span decides."""
    published_at = T0 + span
    before = datetime.now(UTC)

    result = sliding_window.merge_or_create(
        _active_event(T0, T0 + timedelta(hours=22)), _entity_event(published_at)
    )

    if merged:
        _assert_merged(result, T0, published_at, before)
    else:
        _assert_new(result, published_at, before)


@pytest.mark.parametrize(
    ("published_at", "first_seen_at"),
    [
        pytest.param(T0 + timedelta(hours=1), T0 + timedelta(hours=1), id="before_first_seen"),
        pytest.param(T0 + timedelta(hours=3), T0 + timedelta(hours=2), id="between"),
    ],
)
def merge_or_create_out_of_order(published_at, first_seen_at):
    """An EntityEvent dated before the active Event's latest one never moves last_seen_at
    back, and moves first_seen_at earlier only when it is dated before it."""
    last_seen_at = T0 + timedelta(hours=4)
    before = datetime.now(UTC)

    result = sliding_window.merge_or_create(
        _active_event(T0 + timedelta(hours=2), last_seen_at), _entity_event(published_at)
    )

    _assert_merged(result, first_seen_at, last_seen_at, before)


def merge_or_create_max_timespan_out_of_order(monkeypatch):
    """An EntityEvent dated before first_seen_at counts towards the Event's span. Only
    reachable when the maximum timespan is no longer than the sliding window."""
    _set_config(
        monkeypatch,
        dataclasses.replace(CONFIG, event_max_timespan_hours=3),
    )
    published_at = T0 - timedelta(hours=1, minutes=30)
    before = datetime.now(UTC)

    result = sliding_window.merge_or_create(
        _active_event(T0, T0 + timedelta(hours=2)), _entity_event(published_at)
    )

    _assert_new(result, published_at, before)


@pytest.mark.parametrize(
    "active_event",
    [
        pytest.param(None, id="new"),
        pytest.param(
            _active_event(CREATED_AT - timedelta(hours=1), CREATED_AT - timedelta(hours=1)),
            id="merged",
        ),
    ],
)
def merge_or_create_without_published_at(active_event):
    """An EntityEvent without a publication time is placed at its created_at."""
    before = datetime.now(UTC)

    result = sliding_window.merge_or_create(active_event, _entity_event(None))

    if active_event is None:
        _assert_new(result, CREATED_AT, before)
    else:
        _assert_merged(result, CREATED_AT - timedelta(hours=1), CREATED_AT, before)


@pytest.mark.parametrize(
    ("active_event", "entity_event"),
    [
        pytest.param(None, _entity_event(T0, entity_event_id=None), id="no_entity_event_id"),
        pytest.param(None, _entity_event(T0, created_at=None), id="no_created_at"),
        pytest.param(
            dataclasses.replace(_active_event(T0, T0), entity_event_ids=[]),
            _entity_event(T0),
            id="active_event_without_entity_event_ids",
        ),
        pytest.param(
            dataclasses.replace(_active_event(T0, T0), entity_event_ids=None),
            _entity_event(T0),
            id="active_event_with_entity_event_ids_none",
        ),
    ],
)
def merge_or_create_invalid_event(active_event, entity_event):
    with pytest.raises(SAPIError) as exc_info:
        sliding_window.merge_or_create(active_event, entity_event)

    assert type(exc_info.value) is SAPIError
    assert exc_info.value.error_code == "COMMON-5000"
    assert exc_info.value.category is ErrorCategory.AGGREGATION


@pytest.mark.parametrize(
    ("published_at", "created_at", "late"),
    [
        pytest.param(T0, CREATED_AT, True, id="more_than_window_before"),
        pytest.param(T0 + timedelta(hours=1), CREATED_AT, False, id="exactly_window_before"),
        pytest.param(T0 + timedelta(hours=6), CREATED_AT, False, id="after_last_seen"),
        pytest.param(None, T0, True, id="no_publication_time_stored_before"),
        pytest.param(None, T0 + timedelta(hours=1), False, id="no_publication_time_in_window"),
    ],
)
def is_late_success(published_at, created_at, late):
    """An EntityEvent is late when dated more than the sliding window before the active Event's
    latest one, dated by when it was stored if it has no publication time."""
    active_event = _active_event(T0 + timedelta(hours=5), T0 + timedelta(hours=5))

    result = sliding_window.is_late(
        active_event, _entity_event(published_at, created_at=created_at)
    )

    assert result is late


@pytest.mark.parametrize(
    ("span", "ttl_s"),
    [
        pytest.param(timedelta(0), 4 * 3600 + 600, id="sliding_window"),
        pytest.param(timedelta(hours=20), 4 * 3600 + 600, id="window_ends_first"),
        pytest.param(timedelta(hours=23, minutes=30), 30 * 60 + 600, id="lifespan_ends_first"),
        pytest.param(timedelta(hours=24), 600, id="max_timespan_reached"),
    ],
)
def ttl_for_success(span, ttl_s):
    """The merge state is kept until the sliding window or the maximum timespan ends, whichever
    is sooner, plus two flush intervals, so it is never 0."""
    result = sliding_window.ttl_for(_active_event(T0, T0 + span))

    assert result == ttl_s
