from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.common.errors import ServiceUnavailableError
from app.common.stock import Stock
from app.config import EventTypeScore, ScoringConfig
from app.models.entity_event import AnalysisOutcome, EntityEvent, EventTypeEnum, SentimentEnum
from app.models.event import EntitySource, Event
from app.models.event_score import EventScore, LLMScore, RuleScore
from app.models.versioned_metadata import VersionedMetadata
from app.persistent import entity_event_repository as entity_event_repository_module
from app.persistent import event_repository as event_repository_module
from app.persistent import event_score_repository as event_score_repository_module
from app.persistent.entity_event_repository import EntityEventRepository
from app.persistent.event_repository import EventRepository
from app.persistent.event_score_repository import EventScoreRepository
from app.redis import stream_client as stream_client_module
from app.redis.stream_client import RedisStreamClient
from app.scoring import scoring_service as scoring_service_module
from app.scoring.scoring_service import ScoringService

CONFIG = ScoringConfig(
    batch_size=5,
    batch_timeout_ms=200,
    max_retry=3,
    consumer_name="pod-1",
    total_active_sources_default=4,
    event_type_score=[
        EventTypeScore(EventTypeEnum.EARNINGS, 9.0),
        EventTypeScore(EventTypeEnum.BUYBACK, 7.0),
        EventTypeScore(EventTypeEnum.MA, 8.5),
        EventTypeScore(EventTypeEnum.REGULATORY, 8.0),
        EventTypeScore(EventTypeEnum.MANAGEMENT_CHANGE, 7.0),
        EventTypeScore(EventTypeEnum.ANALYST_RATING, 5.0),
        EventTypeScore(EventTypeEnum.DIVIDEND, 6.0),
        EventTypeScore(EventTypeEnum.GENERAL_ANNOUNCEMENT, 4.0),
    ],
)
EVENT_ID = UUID("00000000-0000-0000-0000-0000000000e1")
SEEN_AT = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
AGGREGATION_UPDATED_AT = datetime(2026, 9, 8, 3, 0, 5, tzinfo=UTC)
TENCENT = Stock("00700", "HKEX", "騰訊控股", "TENCENT")


def _event() -> Event:
    return Event(
        event_id=EVENT_ID,
        exchange="HKEX",
        stock_code="00700",
        event_type_primary=EventTypeEnum.EARNINGS,
        first_seen_at=SEEN_AT,
        last_seen_at=SEEN_AT,
        event_type_secondary=[],
        source_list=[
            EntitySource(name, f"https://example.com/{name}", SEEN_AT)
            for name in ["HKEX", "MINGPAO"]
        ],
        aggregation_updated_at=AGGREGATION_UPDATED_AT,
        updated_at=AGGREGATION_UPDATED_AT,
        created_at=SEEN_AT,
    )


def _entity_event(
    source_name: str, sentiment_label: SentimentEnum, sentiment_score: float
) -> EntityEvent:
    return EntityEvent(
        source_id=UUID("00000000-0000-0000-0000-00000000000c"),
        source_url=f"https://example.com/{source_name}",
        source_name=source_name,
        published_at=SEEN_AT,
        analysis_outcome=VersionedMetadata(
            "v1.0.0",
            AnalysisOutcome(
                stock_code="00700",
                event_type_primary=EventTypeEnum.EARNINGS,
                event_type_secondary=None,
                sentiment_label=sentiment_label,
                sentiment_score=sentiment_score,
                headline="騰訊業績",
                entity_summary="騰訊公布季度業績。",
            ),
        ),
        stock=TENCENT,
    )


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    monkeypatch.setattr(scoring_service_module, "_scoring_service", None)


@pytest.fixture(autouse=True)
def event_repository(monkeypatch):
    """An EventRepository holding the Event, unless a test says otherwise."""
    repo = AsyncMock(spec=EventRepository)
    repo.fetch_event.return_value = _event()
    monkeypatch.setattr(event_repository_module, "_event_repository", repo)
    return repo


@pytest.fixture(autouse=True)
def entity_event_repository(monkeypatch):
    repo = AsyncMock(spec=EntityEventRepository)
    repo.fetch_entity_events_for_event.return_value = [
        _entity_event("HKEX", SentimentEnum.POSITIVE, 0.8),
        _entity_event("MINGPAO", SentimentEnum.NEGATIVE, 0.4),
    ]
    monkeypatch.setattr(entity_event_repository_module, "_entity_event_repository", repo)
    return repo


@pytest.fixture(autouse=True)
def event_score_repository(monkeypatch):
    """An EventScoreRepository that stores every score it is given, unless a test says
    otherwise."""
    repo = AsyncMock(spec=EventScoreRepository)
    repo.persist_score_if_not_stale.side_effect = lambda score, expected: score
    monkeypatch.setattr(event_score_repository_module, "_event_score_repository", repo)
    return repo


@pytest.fixture(autouse=True)
def redis_stream(monkeypatch):
    client = AsyncMock(spec=RedisStreamClient)
    monkeypatch.setattr(stream_client_module, "_redis_stream_client", client)
    return client


@pytest.mark.parametrize(
    ("hkex_label", "mingpao_label", "direction"),
    [
        pytest.param(SentimentEnum.POSITIVE, SentimentEnum.NEGATIVE, 1, id="positive"),
        pytest.param(SentimentEnum.NEGATIVE, SentimentEnum.POSITIVE, -1, id="negative"),
        pytest.param(SentimentEnum.NEUTRAL, SentimentEnum.NEUTRAL, 1, id="neutral"),
    ],
)
async def score_event_success(
    event_repository,
    entity_event_repository,
    event_score_repository,
    redis_stream,
    hkex_label,
    mingpao_label,
    direction,
):
    """
    HKEX (weight 9) at 0.8 and MINGPAO (weight 6) at 0.4: strength 9.6 / 15 = 0.64, authority 9,
    heat 2 of 4 sources, so base 9 * 0.30 + 9 * 0.25 + 6.4 * 0.15 + 5 * 0.10 = 6.41. Without an
    LLM impact, direction is the sign of the weighted sentiment, +1 when it is 0, and the final
    score is 6.41 * 0.67.
    """
    entity_event_repository.fetch_entity_events_for_event.return_value = [
        _entity_event("HKEX", hkex_label, 0.8),
        _entity_event("MINGPAO", mingpao_label, 0.4),
    ]
    before = datetime.now(UTC)

    await ScoringService(CONFIG).score_event(EVENT_ID)

    event_repository.fetch_event.assert_awaited_once_with(EVENT_ID)
    entity_event_repository.fetch_entity_events_for_event.assert_awaited_once_with(EVENT_ID)
    score, expected = event_score_repository.persist_score_if_not_stale.await_args.args
    assert expected == AGGREGATION_UPDATED_AT
    assert isinstance(score.score_id, UUID)
    assert before <= score.scored_at <= datetime.now(UTC)
    assert score == EventScore(
        score_id=score.score_id,
        event_id=EVENT_ID,
        abs_final_score=pytest.approx(4.2947),
        base_rule_score=pytest.approx(6.41),
        stock_impact_score=0.0,
        direction=direction,
        llm_fallback=True,
        rule_score_detail=VersionedMetadata(
            version="v1.0.0",
            output=RuleScore(
                event_type_score=9.0,
                source_authority_score=9.0,
                sentiment_strength_score=pytest.approx(6.4),
                source_heat_score=5.0,
            ),
        ),
        llm_score_detail=VersionedMetadata(
            version="none", output=LLMScore(stock_impact_score=0.0, adjustment_reason="")
        ),
        scored_at=score.scored_at,
        scored_aggregation_updated_at=AGGREGATION_UPDATED_AT,
        updated_at=score.scored_at,
        created_at=score.scored_at,
    )
    redis_stream.publish.assert_awaited_once_with(
        "stream:event_scored", {"event_id": str(EVENT_ID), "v": "1"}
    )


async def score_event_stale(event_score_repository, redis_stream):
    """An Event re-aggregated since it was read gets no score, so none is announced."""
    event_score_repository.persist_score_if_not_stale.side_effect = None
    event_score_repository.persist_score_if_not_stale.return_value = None

    await ScoringService(CONFIG).score_event(EVENT_ID)

    score, expected = event_score_repository.persist_score_if_not_stale.await_args.args
    assert expected == AGGREGATION_UPDATED_AT
    assert score.scored_aggregation_updated_at == AGGREGATION_UPDATED_AT
    redis_stream.publish.assert_not_awaited()


async def score_event_missing(
    event_repository, entity_event_repository, event_score_repository, redis_stream
):
    """An Event no longer stored is skipped without loading or writing anything else."""
    event_repository.fetch_event.return_value = None

    await ScoringService(CONFIG).score_event(EVENT_ID)

    event_repository.fetch_event.assert_awaited_once_with(EVENT_ID)
    entity_event_repository.fetch_entity_events_for_event.assert_not_awaited()
    event_score_repository.persist_score_if_not_stale.assert_not_awaited()
    redis_stream.publish.assert_not_awaited()


def get_scoring_service_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        scoring_service_module.get_scoring_service()


def create_scoring_service_success():
    service = scoring_service_module.create_scoring_service(CONFIG)

    assert scoring_service_module.get_scoring_service() is service
