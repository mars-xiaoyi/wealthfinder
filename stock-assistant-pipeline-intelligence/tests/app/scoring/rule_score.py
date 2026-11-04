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
    batch_size=5,
    batch_timeout_ms=200,
    max_retry=3,
    consumer_name="pod-1",
    total_active_sources_default=3,
    event_type_score=[
        EventTypeScore(EventTypeEnum.EARNINGS, 9.0),
        EventTypeScore(EventTypeEnum.BUYBACK, 7.5),
        EventTypeScore(EventTypeEnum.MA, 8.5),
        EventTypeScore(EventTypeEnum.REGULATORY, 8.0),
        EventTypeScore(EventTypeEnum.MANAGEMENT_CHANGE, 7.0),
        EventTypeScore(EventTypeEnum.ANALYST_RATING, 5.0),
        EventTypeScore(EventTypeEnum.DIVIDEND, 6.0),
        EventTypeScore(EventTypeEnum.GENERAL_ANNOUNCEMENT, 4.0),
    ],
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


def _expected_rule_score(
    event_type_score: float, source_authority_score: float, source_heat_score: float
) -> RuleScore:
    return RuleScore(
        event_type_score=event_type_score,
        source_authority_score=source_authority_score,
        sentiment_strength_score=pytest.approx(6.5),
        source_heat_score=pytest.approx(source_heat_score),
    )


@pytest.mark.parametrize(
    ("event_type", "source_names", "total_active_sources", "expected"),
    [
        pytest.param(
            event_type,
            ["MINGPAO", "HKEX", "YAHOO"],
            4,
            _expected_rule_score(event_type_score, 10.0, 7.5),
            id=event_type.lower(),
        )
        for event_type, event_type_score in [
            (EventTypeEnum.EARNINGS, 9.0),
            (EventTypeEnum.BUYBACK, 7.5),
            (EventTypeEnum.MA, 8.5),
            (EventTypeEnum.REGULATORY, 8.0),
            (EventTypeEnum.MANAGEMENT_CHANGE, 7.0),
            (EventTypeEnum.ANALYST_RATING, 5.0),
            (EventTypeEnum.DIVIDEND, 6.0),
            (EventTypeEnum.GENERAL_ANNOUNCEMENT, 4.0),
        ]
    ]
    + [
        pytest.param(
            EventTypeEnum.EARNINGS,
            ["YAHOO"],
            4,
            _expected_rule_score(9.0, 0.0, 2.5),
            id="no_weighted_source",
        ),
        pytest.param(
            EventTypeEnum.EARNINGS,
            ["HKEX", "MINGPAO", "AASTOCKS"],
            2,
            _expected_rule_score(9.0, 10.0, 10.0),
            id="source_heat_capped",
        ),
    ],
)
def compute_rule_score_success(event_type, source_names, total_active_sources, expected):
    """
    Authority is the highest weight among the sources, a source missing from the weights (YAHOO)
    counting as 0. Heat is the share of active sources reporting the Event, capped at 10.
    """
    event = _event(event_type, source_names)

    result = rule_score.compute_rule_score(
        event, SENTIMENT_AGG, SOURCE_WEIGHTS, total_active_sources, CONFIG
    )

    assert result == VersionedMetadata(version="v1.0.0", output=expected)


async def get_source_weights_success():
    """Each known source gets its tier's default weight; an unknown one gets 0."""
    result = await rule_score.get_source_weights(
        ["HKEX", "MINGPAO", "AASTOCKS", "YAHOO_HK", "UNKNOWN"]
    )

    assert result == {"HKEX": 9.0, "MINGPAO": 6.0, "AASTOCKS": 6.0, "YAHOO_HK": 6.0, "UNKNOWN": 0.0}


async def count_active_sources_success():
    assert await rule_score.count_active_sources(CONFIG) == 3
