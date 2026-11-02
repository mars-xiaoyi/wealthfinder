from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.common.stock import Stock
from app.config import EventTypeScore, ScoringConfig
from app.models.entity_event import AnalysisOutcome, EntityEvent, EventTypeEnum, SentimentEnum
from app.models.event import EntitySource, Event
from app.models.event_score import RuleScore
from app.models.versioned_metadata import VersionedMetadata
from app.scoring import rule_score
from app.scoring.rule_score import SentimentAggregation

SEEN_AT = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
TENCENT = Stock("00700", "HKEX", "騰訊控股", "TENCENT")
SOURCE_WEIGHTS = {"HKEX": 10.0, "MINGPAO": 5.0, "AASTOCKS": 5.0}
CONFIG = ScoringConfig(
    event_type_score=[
        EventTypeScore(EventTypeEnum.EARNINGS, 9.0),
        EventTypeScore(EventTypeEnum.BUYBACK, 7.5),
        EventTypeScore(EventTypeEnum.MA, 8.5),
        EventTypeScore(EventTypeEnum.REGULATORY, 8.0),
        EventTypeScore(EventTypeEnum.MANAGEMENT_CHANGE, 7.0),
        EventTypeScore(EventTypeEnum.ANALYST_RATING, 5.0),
        EventTypeScore(EventTypeEnum.DIVIDEND, 6.0),
        EventTypeScore(EventTypeEnum.GENERAL_ANNOUNCEMENT, 4.0),
    ]
)
SENTIMENT_AGG = SentimentAggregation(weighted_signed_score=-0.3, sentiment_strength_score=0.65)


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


def _event(event_type: EventTypeEnum, source_names: list[str]) -> Event:
    return Event(
        event_id=UUID("00000000-0000-0000-0000-0000000000e1"),
        exchange="HKEX",
        stock_code="00700",
        event_type_primary=event_type,
        first_seen_at=SEEN_AT,
        last_seen_at=SEEN_AT,
        source_list=[
            EntitySource(name, f"https://example.com/{name}", SEEN_AT) for name in source_names
        ],
    )


def compute_sentiment_aggregation_success():
    """YAHOO has no weight, so it doesn't count; NEUTRAL counts toward strength only."""
    entity_events = [
        _entity_event("HKEX", SentimentEnum.POSITIVE, 0.8),
        _entity_event("MINGPAO", SentimentEnum.NEGATIVE, 0.4),
        _entity_event("AASTOCKS", SentimentEnum.NEUTRAL, 0.6),
        _entity_event("YAHOO", SentimentEnum.POSITIVE, 0.9),
    ]

    result = rule_score.compute_sentiment_aggregation(entity_events, SOURCE_WEIGHTS)

    # total weight 20: signed (8 - 2 + 0) / 20, strength (8 + 2 + 3) / 20
    assert result.weighted_signed_score == pytest.approx(0.3)
    assert result.sentiment_strength_score == pytest.approx(0.65)


@pytest.mark.parametrize(
    "entity_events",
    [
        pytest.param([], id="no_entity_events"),
        pytest.param(
            [_entity_event("YAHOO", SentimentEnum.POSITIVE, 0.9)], id="no_weighted_source"
        ),
    ],
)
def compute_sentiment_aggregation_zero_total_weight(entity_events):
    result = rule_score.compute_sentiment_aggregation(entity_events, SOURCE_WEIGHTS)

    assert result == SentimentAggregation(weighted_signed_score=0.0, sentiment_strength_score=0.0)


@pytest.mark.parametrize(
    ("event_type", "event_type_score"),
    [
        pytest.param(EventTypeEnum.EARNINGS, 9.0, id="earnings"),
        pytest.param(EventTypeEnum.BUYBACK, 7.5, id="buyback"),
        pytest.param(EventTypeEnum.MA, 8.5, id="ma"),
        pytest.param(EventTypeEnum.REGULATORY, 8.0, id="regulatory"),
        pytest.param(EventTypeEnum.MANAGEMENT_CHANGE, 7.0, id="management_change"),
        pytest.param(EventTypeEnum.ANALYST_RATING, 5.0, id="analyst_rating"),
        pytest.param(EventTypeEnum.DIVIDEND, 6.0, id="dividend"),
        pytest.param(EventTypeEnum.GENERAL_ANNOUNCEMENT, 4.0, id="general_announcement"),
    ],
)
def compute_rule_score_success(event_type, event_type_score):
    """Authority is the highest weight among the sources; YAHOO's missing weight counts as 0."""
    event = _event(event_type, ["MINGPAO", "HKEX", "YAHOO"])

    result = rule_score.compute_rule_score(event, SENTIMENT_AGG, SOURCE_WEIGHTS, 4, CONFIG)

    assert result == VersionedMetadata(
        version="v1.0.0",
        output=RuleScore(
            event_type_score=event_type_score,
            source_authority_score=10.0,
            sentiment_strength_score=pytest.approx(6.5),
            source_heat_score=pytest.approx(7.5),
        ),
    )


def compute_rule_score_no_weighted_source():
    event = _event(EventTypeEnum.EARNINGS, ["YAHOO"])

    result = rule_score.compute_rule_score(event, SENTIMENT_AGG, SOURCE_WEIGHTS, 4, CONFIG)

    assert result == VersionedMetadata(
        version="v1.0.0",
        output=RuleScore(
            event_type_score=9.0,
            source_authority_score=0.0,
            sentiment_strength_score=pytest.approx(6.5),
            source_heat_score=pytest.approx(2.5),
        ),
    )


def compute_rule_score_source_heat_capped():
    """More sources than the active-source count still scores heat at 10."""
    event = _event(EventTypeEnum.EARNINGS, ["HKEX", "MINGPAO", "AASTOCKS"])

    result = rule_score.compute_rule_score(event, SENTIMENT_AGG, SOURCE_WEIGHTS, 2, CONFIG)

    assert result == VersionedMetadata(
        version="v1.0.0",
        output=RuleScore(
            event_type_score=9.0,
            source_authority_score=10.0,
            sentiment_strength_score=pytest.approx(6.5),
            source_heat_score=10.0,
        ),
    )
