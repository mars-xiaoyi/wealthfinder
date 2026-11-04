from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.versioned_metadata import VersionedMetadata


@dataclass
class RuleScore:
    """An Event's rule-based score, one value in [0, 10] per dimension."""

    event_type_score: float
    source_authority_score: float
    sentiment_strength_score: float
    source_heat_score: float

    @property
    def base_rule_score(self) -> float:
        """Return the weighted sum of the four dimensions, in [0, 8]."""
        # weights sum to 0.80; recency's 0.20 share is added by the Cache layer
        return (
            self.event_type_score * 0.30
            + self.source_authority_score * 0.25
            + self.sentiment_strength_score * 0.15
            + self.source_heat_score * 0.10
        )


class LLMScore(BaseModel):
    """An LLM's judgement of how an Event moves its stock."""

    stock_impact_score: float  # [-5, 5]
    adjustment_reason: str  # <=80 Traditional Chinese characters


@dataclass
class EventScore:
    """An Event's fused score; an Event has at most one."""

    score_id: UUID
    event_id: UUID
    abs_final_score: float  # excludes recency
    base_rule_score: float  # [0, 8], unsigned, excludes recency
    stock_impact_score: float  # [-5, 5]
    direction: int  # 1 or -1
    llm_fallback: bool
    rule_score_detail: VersionedMetadata[RuleScore]
    llm_score_detail: VersionedMetadata[LLMScore]
    scored_at: datetime
    scored_aggregation_updated_at: datetime  # the Event's aggregation_updated_at it scores
    updated_at: datetime
    created_at: datetime
