from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.entity_event import EventTypeEnum
from app.models.versioned_metadata import VersionedMetadata


class BriefSummary(BaseModel):
    """An Event's Traditional Chinese Morning Brief summary."""

    summary_short: str  # <=30 characters
    summary_full: str  # <=150 characters
    key_numbers: list[str]  # at most 3; may be empty


@dataclass
class EntitySource:
    """One source reporting an Event."""

    source_name: str  # e.g. "HKEX", "MINGPAO"
    url: str  # the source's article URL
    published_at: datetime


@dataclass
class Event:
    """One stock's event merged from its EntityEvents: an events row, or a group's active
    Event before it is persisted."""

    event_id: UUID
    exchange: str
    stock_code: str
    event_type_primary: EventTypeEnum
    first_seen_at: datetime  # publication time of the earliest constituent EntityEvent
    last_seen_at: datetime  # publication time of the latest constituent EntityEvent
    event_type_secondary: list[EventTypeEnum] | None = None  # set once persisted
    source_list: list[EntitySource] | None = None  # set once persisted
    brief_summary: VersionedMetadata[BriefSummary] | None = None  # set once scored
    brief_summary_aggregation_updated_at: datetime | None = None  # the merge state summarized
    brief_summary_fallback: bool | None = None  # whether the summary is the extractive fallback
    aggregation_updated_at: datetime | None = None  # when the merge state last changed
    updated_at: datetime | None = None  # set once persisted
    created_at: datetime | None = None  # set once persisted
    entity_event_ids: list[UUID] | None = None  # constituent EntityEvents; active Event only

    def is_active_event(self) -> bool:
        """Return whether this is a group's active Event, which EntityEvents are merged into."""
        return self.entity_event_ids is not None and len(self.entity_event_ids) > 0

    @property
    def source_count(self) -> int | None:
        """Return the number of distinct sources reporting this Event, or None before it is
        persisted."""
        return len(self.source_list) if self.source_list is not None else None
