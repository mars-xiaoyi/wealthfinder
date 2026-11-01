from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.common.error_categories import ErrorCategory
from app.common.errors import SAPIError
from app.config import get_config
from app.models.entity_event import EntityEvent
from app.models.event import Event

_SECONDS_PER_HOUR = 3600


def merge_or_create(active_event: Event | None, entity_event: EntityEvent) -> Event:
    """
    Return the group's active Event after the entity_event arrives: active_event extended by
    it when it falls inside the sliding window and the Event's maximum timespan, otherwise a
    new Event started from it.
    """
    if active_event is not None and not active_event.is_active_event():
        raise SAPIError(
            f"event is not a group's active event: {active_event.event_id}",
            category=ErrorCategory.AGGREGATION,
        )
    if not entity_event.is_persistent_event():
        raise SAPIError(
            f"entity event is not persisted: {entity_event.source_url}",
            category=ErrorCategory.AGGREGATION,
        )
    published_at = entity_event.get_published_at()
    entity_event_id = entity_event.entity_event_id
    # Both are set on a persisted EntityEvent, checked above.
    assert published_at is not None and entity_event_id is not None
    config = get_config().aggregation
    aggregation_updated_at = datetime.now(UTC)

    if active_event is not None:
        gap_hours = _get_hours(abs(published_at - active_event.last_seen_at))
        span_hours = _get_hours(
            max(published_at, active_event.last_seen_at)
            - min(published_at, active_event.first_seen_at)
        )
        remaining_lifespan_hours = config.event_max_timespan_hours - span_hours
        if gap_hours <= config.sliding_window_hours and remaining_lifespan_hours > 0:
            # Set on an active Event, checked above.
            assert active_event.entity_event_ids is not None
            return Event(
                event_id=active_event.event_id,
                exchange=active_event.exchange,
                stock_code=active_event.stock_code,
                event_type_primary=active_event.event_type_primary,
                # min/max, since EntityEvents can arrive out of publication order
                first_seen_at=min(active_event.first_seen_at, published_at),
                last_seen_at=max(active_event.last_seen_at, published_at),
                aggregation_updated_at=aggregation_updated_at,
                entity_event_ids=[*active_event.entity_event_ids, entity_event_id],
            )

    return Event(
        event_id=uuid4(),
        exchange=entity_event.stock.exchange,
        stock_code=entity_event.stock.stock_code,
        event_type_primary=entity_event.analysis_outcome.output.event_type_primary,
        first_seen_at=published_at,
        last_seen_at=published_at,
        aggregation_updated_at=aggregation_updated_at,
        entity_event_ids=[entity_event_id],
    )


def _get_hours(delta: timedelta) -> float:
    return delta.total_seconds() / _SECONDS_PER_HOUR
