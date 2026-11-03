from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass
class EventEntityMap:
    """An EntityEvent's membership in the Event it was merged into: an event_entity_map row."""

    event_id: UUID
    entity_event_id: UUID  # unique: an EntityEvent belongs to one Event
    created_at: datetime  # when the membership was first persisted
