import asyncio
from uuid import UUID

import redis.exceptions

from app.aggregation.aggregation_service import get_aggregation_service
from app.common.errors import SAPIError
from app.config import AggregationConfig, RedisConfig
from app.logger import get_logger
from app.redis.stream_client import StreamMessage, get_redis_stream_client
from app.redis.stream_enum import EntityEventCompletedStream

logger = get_logger(__name__)

_REDIS_UNREACHABLE = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


class AggregationConsumer:
    """Consumes stream:entity_event_completed and merges each EntityEvent into its group's
    active Event."""

    def __init__(self, config: AggregationConfig, redis_config: RedisConfig) -> None:
        self._service = get_aggregation_service()
        self._redis_stream = get_redis_stream_client()
        self._config = config
        self._reconnect_interval_s = redis_config.reconnect_interval_s
        self._enqueued: set[UUID] = set()

    async def start(self) -> None:
        """Consume stream:entity_event_completed until cancelled."""
        await self._redis_stream.create_group_if_not_exists(
            EntityEventCompletedStream.NAME, EntityEventCompletedStream.CONSUMER_GROUP
        )
        await self._consumer_loop()

    async def _consumer_loop(self) -> None:
        """Route every message of the stream until cancelled."""
        while True:
            try:
                messages = await self._redis_stream.read_from(
                    EntityEventCompletedStream.NAME,
                    EntityEventCompletedStream.CONSUMER_GROUP,
                    self._config.consumer_name,
                    count=self._config.batch_size,
                    block_ms=self._config.batch_timeout_ms,
                )
                # One at a time, so each group's EntityEvents are queued in stream order.
                for message in messages:
                    await self._route(message)
            except _REDIS_UNREACHABLE:
                # Any unrouted rest of the batch stays pending, un-ACKed.
                logger.warning(
                    "aggregation_redis_stream_unreachable",
                    retry_in_s=self._reconnect_interval_s,
                    exc_info=True,
                )
                await asyncio.sleep(self._reconnect_interval_s)

    async def _route(self, message: StreamMessage) -> None:
        """Route the message's EntityEvent, skipping a message without a valid
        entity_event_id."""
        try:
            entity_event_id = UUID(message.fields["entity_event_id"])
        except (KeyError, ValueError):
            logger.warning(
                "aggregation_message_invalid", message_id=message.id, fields=message.fields
            )
            return
        await self._route_one(entity_event_id, message.id)

    async def _route_one(self, entity_event_id: UUID, message_id: str) -> None:
        """Route the EntityEvent to be merged, unless it is already in flight."""
        # No await between this check and the insert. An EntityEvent waiting in its group's
        # queue counts as in flight.
        if entity_event_id in self._enqueued:
            return
        self._enqueued.add(entity_event_id)
        try:
            await self._service.process_entity_event(
                entity_event_id,
                lambda: self._on_success(entity_event_id, message_id),
                lambda: self._enqueued.discard(entity_event_id),
            )
        except _REDIS_UNREACHABLE:
            self._enqueued.discard(entity_event_id)
            raise
        except SAPIError as exc:
            self._enqueued.discard(entity_event_id)
            logger.log(
                exc.log_level,
                exc.dev_message,
                entity_event_id=entity_event_id,
                message_id=message_id,
                exc_info=exc,
            )
        except Exception as exc:
            self._enqueued.discard(entity_event_id)
            logger.error(
                f"Unhandled exception: {exc!r}",
                entity_event_id=entity_event_id,
                message_id=message_id,
                exc_info=exc,
            )

    async def _on_success(self, entity_event_id: UUID, message_id: str) -> None:
        self._enqueued.discard(entity_event_id)
        await self._redis_stream.ack(
            EntityEventCompletedStream.NAME, EntityEventCompletedStream.CONSUMER_GROUP, message_id
        )
