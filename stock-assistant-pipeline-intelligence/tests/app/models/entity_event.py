from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.common.stock import Stock
from app.models.entity_event import AnalysisOutcome, EntityEvent, EventTypeEnum, SentimentEnum
from app.models.versioned_metadata import VersionedMetadata

PUBLISHED_AT = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)
ENTITY_EVENT_ID = UUID("00000000-0000-0000-0000-0000000000a1")


def _entity_event(
    published_at: datetime | None = PUBLISHED_AT,
    entity_event_id: UUID | None = ENTITY_EVENT_ID,
    created_at: datetime | None = CREATED_AT,
) -> EntityEvent:
    return EntityEvent(
        source_id=UUID("00000000-0000-0000-0000-00000000000c"),
        source_url="https://example.com/a",
        source_name="MINGPAO",
        published_at=published_at,
        analysis_outcome=VersionedMetadata(
            "v1.0.0",
            AnalysisOutcome(
                stock_code="00700",
                event_type_primary=EventTypeEnum.EARNINGS,
                event_type_secondary=None,
                sentiment_label=SentimentEnum.POSITIVE,
                sentiment_score=0.8,
                headline="業績超預期",
                entity_summary="季度收入按年增長。",
            ),
        ),
        stock=Stock("00700", "HKEX", "騰訊控股", "TENCENT"),
        entity_event_id=entity_event_id,
        created_at=created_at,
    )


@pytest.mark.parametrize(
    ("entity_event", "persistent"),
    [
        pytest.param(_entity_event(), True, id="persisted"),
        pytest.param(_entity_event(entity_event_id=None), False, id="no_entity_event_id"),
        pytest.param(_entity_event(created_at=None), False, id="no_created_at"),
    ],
)
def is_persistent_event_success(entity_event, persistent):
    assert entity_event.is_persistent_event() is persistent


@pytest.mark.parametrize(
    ("entity_event", "published_at"),
    [
        pytest.param(_entity_event(), PUBLISHED_AT, id="published_at"),
        pytest.param(_entity_event(published_at=None), CREATED_AT, id="without_published_at"),
        pytest.param(_entity_event(published_at=None, created_at=None), None, id="not_persisted"),
    ],
)
def get_published_at_success(entity_event, published_at):
    assert entity_event.get_published_at() == published_at
