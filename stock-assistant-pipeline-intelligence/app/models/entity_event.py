from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel

from app.common.stock import Stock
from app.models.versioned_metadata import VersionedMetadata


class EventTypeEnum(StrEnum):
    EARNINGS = "EARNINGS"
    BUYBACK = "BUYBACK"
    MA = "MA"
    REGULATORY = "REGULATORY"
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    ANALYST_RATING = "ANALYST_RATING"
    DIVIDEND = "DIVIDEND"
    GENERAL_ANNOUNCEMENT = "GENERAL_ANNOUNCEMENT"


class SentimentEnum(StrEnum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"


class AnalysisOutcome(BaseModel):
    """One verified listed company in an article, with the event the article reports for it."""

    stock_code: str
    event_type_primary: EventTypeEnum
    event_type_secondary: EventTypeEnum | None
    sentiment_label: SentimentEnum
    sentiment_score: float  # [0.0, 1.0]
    headline: str  # <=20 Traditional Chinese characters
    entity_summary: str  # <=100 Traditional Chinese characters


@dataclass
class EntityEvent:
    """One listed company's event in one article: an entity_events row."""

    source_id: UUID  # the article's cleaned_id
    source_url: str
    source_name: str
    published_at: datetime | None
    analysis_outcome: VersionedMetadata[AnalysisOutcome]
    stock: Stock
    entity_event_id: UUID | None = None  # set once the row is persisted
    created_at: datetime | None = None  # set once the row is persisted
