from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.models.entity_event import EventTypeEnum
from app.models.event import Event

SEEN_AT = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("entity_event_ids", "active"),
    [
        pytest.param([UUID("00000000-0000-0000-0000-0000000000a1")], True, id="active"),
        pytest.param([], False, id="empty"),
        pytest.param(None, False, id="none"),
    ],
)
def is_active_event_success(entity_event_ids, active):
    event = Event(
        event_id=UUID("00000000-0000-0000-0000-0000000000e1"),
        exchange="HKEX",
        stock_code="00700",
        event_type_primary=EventTypeEnum.EARNINGS,
        first_seen_at=SEEN_AT,
        last_seen_at=SEEN_AT,
        entity_event_ids=entity_event_ids,
    )

    assert event.is_active_event() is active
