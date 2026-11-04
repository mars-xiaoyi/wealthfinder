import asyncio
import time
from uuid import UUID

import redis.exceptions

from app.aggregation.aggregation_service import get_aggregation_service
from app.common.errors import SAPIError
from app.config import AggregationConfig, DatabaseConfig, RedisConfig
from app.logger import get_logger
from app.redis.stream_client import (
    DeadLetterReason,
    ReclaimedMessage,
    StreamMessage,
    get_redis_stream_client,
)
from app.redis.stream_enum import AggregationDeadLetterStream, EntityEventCompletedStream

logger = get_logger(__name__)

_REDIS_UNREACHABLE = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


def _get_valid_entity_event_id(message: StreamMessage) -> UUID | None:
    """The message's entity_event_id, or None if the message is not one this layer can
    process."""
    if message.fields.get("v") != EntityEventCompletedStream.VERSION:
        return None
    try:
        return UUID(message.fields["entity_event_id"])
    except (KeyError, ValueError):
        return None


class AggregationConsumer:
    """Consumes stream:entity_event_completed and merges each EntityEvent into its group's
    active Event."""

    def __init__(
        self, config: AggregationConfig, redis_config: RedisConfig, db_config: DatabaseConfig
    ) -> None:
        self._service = get_aggregation_service()
        self._redis_stream = get_redis_stream_client()
        self._config = config
        self._reconnect_interval_s = redis_config.reconnect_interval_s
        self._claim_timeout_ms = redis_config.stream_claim_timeout_ms
        self._final_flush_timeout_s = db_config.close_timeout_s
        self._enqueued: set[UUID] = set()

    async def start(self) -> None:
        """Consume stream:entity_event_completed until cancelled, persisting the active Events
        once more when it stops. Raises ExceptionGroup."""
        await self._redis_stream.create_group_if_not_exists(
            EntityEventCompletedStream.NAME, EntityEventCompletedStream.CONSUMER_GROUP
        )
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._consumer_loop())
                group.create_task(self._reclaim_loop())
        finally:
            await self._flush_final()

    async def _consumer_loop(self) -> None:
        """Route every message of the stream and flush the active Events, until cancelled."""
        last_flush = time.monotonic()
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
                continue
            # The interval bounds how long a flush waits under steady input.
            if not messages or time.monotonic() - last_flush >= self._config.db_flush_interval_s:
                last_flush = time.monotonic()
                await self._flush()

    async def _reclaim_loop(self) -> None:
        """Reclaim the messages left pending longer than the claim timeout, until cancelled."""
        while True:
            await asyncio.sleep(self._claim_timeout_ms / 1000)
            try:
                messages = await self._redis_stream.autoclaim(
                    EntityEventCompletedStream.NAME,
                    EntityEventCompletedStream.CONSUMER_GROUP,
                    self._config.consumer_name,
                    min_idle_ms=self._claim_timeout_ms,
                    count=self._config.batch_size,
                )
                # One at a time, so each group's EntityEvents are queued in claim order.
                for message in messages:
                    await self._route(message)
            except _REDIS_UNREACHABLE:
                logger.warning(
                    "aggregation_redis_stream_unreachable",
                    retry_in_s=self._claim_timeout_ms / 1000,
                    exc_info=True,
                )
            except Exception:
                logger.warning("aggregation_reclaim_sweep_failed", exc_info=True)

    async def _flush_final(self) -> None:
        """Flush the active Events one last time."""
        # Bounded, since announcing retries for as long as the stream Redis is unreachable. An
        # Event left unannounced is announced by the first flush after the next start.
        try:
            async with asyncio.timeout(self._final_flush_timeout_s):
                await self._flush()
        except TimeoutError:
            logger.warning(
                "aggregation_final_flush_timed_out", timeout_s=self._final_flush_timeout_s
            )

    async def _flush(self) -> None:
        """Flush the active Events."""
        # The next flush retries from the same active Events.
        try:
            await self._service.flush()
        except SAPIError as exc:
            logger.log(exc.log_level, exc.dev_message, exc_info=exc)
        except Exception as exc:
            logger.error(f"Unhandled exception: {exc!r}", exc_info=exc)

    async def _route(self, message: StreamMessage) -> None:
        """Dead-letter a message that is out of retries or malformed, and route any other's
        EntityEvent."""
        try:
            if (
                isinstance(message, ReclaimedMessage)
                and message.delivery_count >= self._config.max_retry
            ):
                await self._push_dead_letter(message, DeadLetterReason.RETRIES_EXHAUSTED)
                return
            entity_event_id = _get_valid_entity_event_id(message)
            if entity_event_id is None:
                await self._push_dead_letter(message, DeadLetterReason.MALFORMED)
                return
            await self._route_one(entity_event_id, message.id)
        except _REDIS_UNREACHABLE:
            raise
        except Exception:
            logger.warning(
                "aggregation_message_handling_failed", message_id=message.id, exc_info=True
            )

    async def _push_dead_letter(self, message: StreamMessage, reason: DeadLetterReason) -> None:
        """Move the message to the dead-letter stream."""
        await self._redis_stream.dead_letter(
            message,
            EntityEventCompletedStream.NAME,
            EntityEventCompletedStream.CONSUMER_GROUP,
            AggregationDeadLetterStream.NAME,
            reason,
        )

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
        except asyncio.CancelledError:
            # Not handed to its group, and a restart reuses this consumer, so a leftover entry
            # would make every redelivery skip as in flight.
            self._enqueued.discard(entity_event_id)
            raise
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
