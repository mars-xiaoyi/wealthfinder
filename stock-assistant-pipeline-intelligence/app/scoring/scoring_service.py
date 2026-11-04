import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.config import ScoringConfig
from app.logger import get_logger
from app.models.event_score import EventScore, LLMScore, RuleScore
from app.models.versioned_metadata import VersionedMetadata
from app.persistent.entity_event_repository import get_entity_event_repository
from app.persistent.event_repository import get_event_repository
from app.persistent.event_score_repository import get_event_score_repository
from app.redis.stream_client import get_redis_stream_client
from app.redis.stream_enum import EventScoredStream
from app.scoring.rule_score import (
    SentimentAggregation,
    compute_rule_score,
    compute_sentiment_aggregation,
    count_active_sources,
    get_source_weights,
)

logger = get_logger(__name__)

# Stands in for the LLM's score, as if it had fallen back: no impact, so direction comes from
# the sentiment.
_LLM_SCORE_PLACEHOLDER = VersionedMetadata(
    version="none", output=LLMScore(stock_impact_score=0.0, adjustment_reason="")
)


@dataclass
class _FusedScore:
    direction: int  # 1 or -1
    base_rule_score: float
    abs_final_score: float


def _score_fusion(
    rule_score: RuleScore, llm_score: LLMScore, sentiment_agg: SentimentAggregation
) -> _FusedScore:
    if llm_score.stock_impact_score > 0:
        direction = 1
    elif llm_score.stock_impact_score < 0:
        direction = -1
    else:
        direction = 1 if sentiment_agg.weighted_signed_score >= 0 else -1
    base_rule_score = rule_score.base_rule_score
    raw_final_score = base_rule_score * direction * 0.67 + llm_score.stock_impact_score * 0.33 * 2
    return _FusedScore(direction, base_rule_score, abs(raw_final_score))


class ScoringService:
    """Scores the Events aggregated from EntityEvents."""

    def __init__(self, config: ScoringConfig) -> None:
        self._event_repo = get_event_repository()
        self._entity_event_repo = get_entity_event_repository()
        self._event_score_repo = get_event_score_repository()
        self._redis_stream = get_redis_stream_client()
        self._config = config

    async def score_event(self, event_id: UUID) -> None:
        """
        Score the stored Event with this id and announce its new score. An Event no longer
        stored is skipped, and so is one re-aggregated since it was read.
        Raises ServiceUnavailableError or DatabaseError.
        """
        event = await self._event_repo.fetch_event(event_id)
        if event is None:
            logger.warning("scoring_event_missing", event_id=str(event_id))
            return
        # Set on every persisted Event.
        assert event.aggregation_updated_at is not None and event.source_list is not None
        expected = event.aggregation_updated_at
        constituent_entity_events = await self._entity_event_repo.fetch_entity_events_for_event(
            event_id
        )
        source_weights = await get_source_weights(
            [source.source_name for source in event.source_list]
        )

        total_active_sources = await count_active_sources(self._config)
        sentiment_agg = compute_sentiment_aggregation(constituent_entity_events, source_weights)
        rule_score_detail = compute_rule_score(
            event, sentiment_agg, source_weights, total_active_sources, self._config
        )

        llm_score_detail = _LLM_SCORE_PLACEHOLDER
        scored_at = datetime.now(UTC)
        fused = _score_fusion(rule_score_detail.output, llm_score_detail.output, sentiment_agg)
        score = EventScore(
            score_id=uuid.uuid4(),
            event_id=event_id,
            abs_final_score=fused.abs_final_score,
            base_rule_score=fused.base_rule_score,
            stock_impact_score=llm_score_detail.output.stock_impact_score,
            direction=fused.direction,
            llm_fallback=True,
            rule_score_detail=rule_score_detail,
            llm_score_detail=llm_score_detail,
            scored_at=scored_at,
            scored_aggregation_updated_at=expected,
            # Not written: the database sets both.
            updated_at=scored_at,
            created_at=scored_at,
        )
        if await self._event_score_repo.persist_score_if_not_stale(score, expected) is None:
            return
        await self._redis_stream.publish(
            EventScoredStream.NAME,
            {"event_id": str(event_id), "v": EventScoredStream.VERSION},
        )


_scoring_service: ScoringService | None = None


def get_scoring_service() -> ScoringService:
    """Return the process's one ScoringService instance."""
    if _scoring_service is None:
        raise ServiceUnavailableError(
            "scoring service is not initialized", category=ErrorCategory.SCORING
        )
    return _scoring_service


def create_scoring_service(config: ScoringConfig) -> ScoringService:
    """Create the ScoringService and register it as this module's singleton."""
    global _scoring_service
    _scoring_service = ScoringService(config)
    return _scoring_service
