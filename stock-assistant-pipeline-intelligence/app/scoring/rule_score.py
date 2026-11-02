from dataclasses import dataclass

from app.config import ScoringConfig
from app.models.entity_event import EntityEvent, EventTypeEnum, SentimentEnum
from app.models.event import Event
from app.models.event_score import RuleScore
from app.models.versioned_metadata import VersionedMetadata

RULE_SCORE_VERSION = "v1.0.0"

_DIRECTION_COEFFICIENT = {
    SentimentEnum.POSITIVE: 1,
    SentimentEnum.NEUTRAL: 0,
    SentimentEnum.NEGATIVE: -1,
}


@dataclass
class SentimentAggregation:
    """The source-weighted sentiment of an Event's constituent EntityEvents."""

    weighted_signed_score: float  # [-1, 1]
    sentiment_strength_score: float  # [0, 1]


def compute_sentiment_aggregation(
    constituent_entity_events: list[EntityEvent], source_weights: dict[str, float]
) -> SentimentAggregation:
    """Return the source-weighted mean sentiment of the EntityEvents, signed by label and
    unsigned, or zeros when their total weight is 0. A source missing from source_weights has
    weight 0."""
    total_weight = 0.0
    weighted_signed_sum = 0.0
    weighted_strength_sum = 0.0
    for entity_event in constituent_entity_events:
        weight = source_weights.get(entity_event.source_name, 0.0)
        outcome = entity_event.analysis_outcome.output
        weighted_signed_sum += (
            outcome.sentiment_score * _DIRECTION_COEFFICIENT[outcome.sentiment_label] * weight
        )
        weighted_strength_sum += outcome.sentiment_score * weight
        total_weight += weight
    if total_weight == 0:
        return SentimentAggregation(weighted_signed_score=0.0, sentiment_strength_score=0.0)
    return SentimentAggregation(
        weighted_signed_score=weighted_signed_sum / total_weight,
        sentiment_strength_score=weighted_strength_sum / total_weight,
    )


def compute_rule_score(
    event: Event,
    sentiment_agg: SentimentAggregation,
    source_weights: dict[str, float],
    total_active_sources: int,
    config: ScoringConfig,
) -> VersionedMetadata[RuleScore]:
    """Return the persisted Event's versioned Rule Score. A source missing from source_weights
    has weight 0."""
    assert event.source_list is not None and event.source_count is not None
    rule_score = RuleScore(
        event_type_score=_get_event_type_score(event.event_type_primary, config),
        source_authority_score=max(
            source_weights.get(source.source_name, 0.0) for source in event.source_list
        ),
        sentiment_strength_score=sentiment_agg.sentiment_strength_score * 10,
        source_heat_score=min(10.0, event.source_count / total_active_sources * 10),
    )
    return VersionedMetadata(version=RULE_SCORE_VERSION, output=rule_score)


def _get_event_type_score(event_type: EventTypeEnum, config: ScoringConfig) -> float:
    return next(e.score for e in config.event_type_score if e.event_type == event_type)
