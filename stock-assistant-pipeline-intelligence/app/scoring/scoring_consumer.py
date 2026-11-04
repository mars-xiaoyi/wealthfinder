import asyncio
from uuid import UUID

import redis.exceptions

from app.common.errors import SAPIError
from app.config import RedisConfig, ScoringConfig
from app.logger import get_logger
from app.redis.stream_client import (
    DeadLetterReason,
    ReclaimedMessage,
    StreamMessage,
    get_redis_stream_client,
)
from app.redis.stream_enum import EventAggregatedStream, ScoringDeadLetterStream
from app.scoring.scoring_service import get_scoring_service

logger = get_logger(__name__)

_REDIS_UNREACHABLE = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


def _get_valid_event_id(message: StreamMessage) -> UUID | None:
    """The message's event_id, or None if the message is not one this layer can process."""
    if message.fields.get("v") != EventAggregatedStream.VERSION:
        return None
    try:
        return UUID(message.fields["event_id"])
    except (KeyError, ValueError):
        return None


class ScoringConsumer:
    """Consumes stream:event_aggregated and scores each Event."""

    def __init__(self, config: ScoringConfig, redis_config: RedisConfig) -> None:
        self._service = get_scoring_service()
        self._redis_stream = get_redis_stream_client()
        self._config = config
        self._reconnect_interval_s = redis_config.reconnect_interval_s
        self._claim_timeout_ms = redis_config.stream_claim_timeout_ms
        self._in_process: set[UUID] = set()

    async def start(self) -> None:
        """Consume stream:event_aggregated until cancelled. Raises ExceptionGroup."""
        await self._redis_stream.create_group_if_not_exists(
            EventAggregatedStream.NAME, EventAggregatedStream.CONSUMER_GROUP
        )
        async with asyncio.TaskGroup() as group:
            group.create_task(self._consumer_loop())
            group.create_task(self._reclaim_loop())

    async def _consumer_loop(self) -> None:
        """Route every message of the stream until cancelled, riding out periods when the
        stream Redis is unreachable."""
        while True:
            try:
                messages = await self._redis_stream.read_from(
                    EventAggregatedStream.NAME,
                    EventAggregatedStream.CONSUMER_GROUP,
                    self._config.consumer_name,
                    count=self._config.batch_size,
                    block_ms=self._config.batch_timeout_ms,
                )
                for message in messages:
                    await self._route(message)
            except _REDIS_UNREACHABLE:
                # Any unrouted rest of the batch stays pending, un-ACKed.
                logger.warning(
                    "scoring_redis_stream_unreachable",
                    retry_in_s=self._reconnect_interval_s,
                    exc_info=True,
                )
                await asyncio.sleep(self._reconnect_interval_s)

    async def _reclaim_loop(self) -> None:
        """Reclaim the messages left pending longer than the claim timeout, until cancelled."""
        while True:
            await asyncio.sleep(self._claim_timeout_ms / 1000)
            try:
                messages = await self._redis_stream.autoclaim(
                    EventAggregatedStream.NAME,
                    EventAggregatedStream.CONSUMER_GROUP,
                    self._config.consumer_name,
                    min_idle_ms=self._claim_timeout_ms,
                    count=self._config.batch_size,
                )
                for message in messages:
                    await self._route(message)
            except _REDIS_UNREACHABLE:
                logger.warning(
                    "scoring_redis_stream_unreachable",
                    retry_in_s=self._claim_timeout_ms / 1000,
                    exc_info=True,
                )
            except Exception:
                logger.warning("scoring_reclaim_sweep_failed", exc_info=True)

    async def _route(self, message: StreamMessage) -> None:
        """Skip a message whose Event is already being scored, dead-letter one that is malformed
        or out of retries, and score any other's Event."""
        try:
            event_id = _get_valid_event_id(message)
            if event_id is None:
                await self._push_dead_letter(message, DeadLetterReason.MALFORMED)
                return
            # Ahead of the retry check, so a message whose Event is still being scored is
            # never dead-lettered. No await between this check and the insert below.
            if event_id in self._in_process:
                return
            if (
                isinstance(message, ReclaimedMessage)
                and message.delivery_count >= self._config.max_retry
            ):
                await self._push_dead_letter(message, DeadLetterReason.RETRIES_EXHAUSTED)
                return
            self._in_process.add(event_id)
            try:
                await self._process_one(event_id, message.id)
            finally:
                self._in_process.discard(event_id)
        except _REDIS_UNREACHABLE:
            raise
        except Exception:
            logger.warning("scoring_message_handling_failed", message_id=message.id, exc_info=True)

    async def _push_dead_letter(self, message: StreamMessage, reason: DeadLetterReason) -> None:
        """Move the message to the dead-letter stream."""
        await self._redis_stream.dead_letter(
            message,
            EventAggregatedStream.NAME,
            EventAggregatedStream.CONSUMER_GROUP,
            ScoringDeadLetterStream.NAME,
            reason,
        )

    async def _process_one(self, event_id: UUID, message_id: str) -> None:
        """Score the Event."""
        try:
            await self._service.score_event(event_id)
            await self._redis_stream.ack(
                EventAggregatedStream.NAME, EventAggregatedStream.CONSUMER_GROUP, message_id
            )
        except _REDIS_UNREACHABLE:
            raise
        except SAPIError as exc:
            logger.log(
                exc.log_level,
                exc.dev_message,
                event_id=str(event_id),
                message_id=message_id,
                exc_info=exc,
            )
        except Exception as exc:
            logger.error(
                f"Unhandled exception: {exc!r}",
                event_id=str(event_id),
                message_id=message_id,
                exc_info=exc,
            )
