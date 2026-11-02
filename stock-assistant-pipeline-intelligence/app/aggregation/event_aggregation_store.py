import json
from datetime import datetime
from uuid import UUID

import redis.asyncio.client

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.models.entity_event import EventTypeEnum
from app.models.event import Event
from app.redis.state_client import get_redis_state_client


def _get_key(exchange: str, stock_code: str, event_type_primary: EventTypeEnum) -> str:
    return f"agg:event:{exchange}:{stock_code}:{event_type_primary}"


def _decode(
    exchange: str, stock_code: str, event_type_primary: EventTypeEnum, fields: dict[str, str]
) -> Event:
    return Event(
        event_id=UUID(fields["event_id"]),
        exchange=exchange,
        stock_code=stock_code,
        event_type_primary=event_type_primary,
        first_seen_at=datetime.fromisoformat(fields["first_seen_at"]),
        last_seen_at=datetime.fromisoformat(fields["last_seen_at"]),
        aggregation_updated_at=datetime.fromisoformat(fields["aggregation_updated_at"]),
        entity_event_ids=[UUID(i) for i in json.loads(fields["entity_event_ids"])],
    )


class EventAggregationStore:
    """Holds each group's active Event in Redis."""

    def __init__(self) -> None:
        self._state_client = get_redis_state_client()

    async def get_active_event(
        self, exchange: str, stock_code: str, event_type_primary: EventTypeEnum
    ) -> Event | None:
        """Return the group's active Event, or None if the group has none."""
        fields = await self._state_client.hgetall(
            _get_key(exchange, stock_code, event_type_primary)
        )
        if fields is None:
            return None
        return _decode(exchange, stock_code, event_type_primary, fields)

    async def upsert_active_event(self, active_event: Event, ttl_s: int) -> None:
        """Store the Event as its group's active Event, kept for ttl_s seconds."""
        # Set on an active Event, which is all merge_or_create() returns.
        assert active_event.aggregation_updated_at is not None
        assert active_event.entity_event_ids is not None
        key = _get_key(
            active_event.exchange, active_event.stock_code, active_event.event_type_primary
        )
        fields = {
            "event_id": str(active_event.event_id),
            "first_seen_at": active_event.first_seen_at.isoformat(),
            "last_seen_at": active_event.last_seen_at.isoformat(),
            "aggregation_updated_at": active_event.aggregation_updated_at.isoformat(),
            "entity_event_ids": json.dumps([str(i) for i in active_event.entity_event_ids]),
        }

        # One transaction, so the key never exists without its TTL.
        def queue_upsert(pipe: redis.asyncio.client.Pipeline) -> None:
            pipe.hset(key, mapping=fields)
            pipe.expire(key, ttl_s)

        await self._state_client.multi_exec(queue_upsert)


_event_aggregation_store: EventAggregationStore | None = None


def get_event_aggregation_store() -> EventAggregationStore:
    """Return the process's one EventAggregationStore instance."""
    if _event_aggregation_store is None:
        raise ServiceUnavailableError(
            "event aggregation store is not initialized", category=ErrorCategory.AGGREGATION
        )
    return _event_aggregation_store


def create_event_aggregation_store() -> EventAggregationStore:
    """Create the EventAggregationStore and register it as this module's singleton."""
    global _event_aggregation_store
    _event_aggregation_store = EventAggregationStore()
    return _event_aggregation_store
