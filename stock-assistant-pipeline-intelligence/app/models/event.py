from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.models.entity_event import EventTypeEnum


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
    aggregation_updated_at: datetime | None = None  # when the merge state last changed
    updated_at: datetime | None = None  # set once persisted
    created_at: datetime | None = None  # set once persisted
    entity_event_ids: list[UUID] | None = None  # constituent EntityEvents; active Event only

    def is_active_event(self) -> bool:
        """Return whether this is a group's active Event, which EntityEvents are merged into."""
        return self.entity_event_ids is not None and len(self.entity_event_ids) > 0
