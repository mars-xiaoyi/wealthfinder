import asyncio
from uuid import UUID

import redis.exceptions

from app.common.errors import SAPIError
from app.config import RedisConfig, ScoringConfig
from app.logger import get_logger
from app.redis.stream_client import StreamMessage, get_redis_stream_client
from app.redis.stream_enum import EventAggregatedStream
from app.scoring.scoring_service import get_scoring_service

logger = get_logger(__name__)

_REDIS_UNREACHABLE = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


class ScoringConsumer:
    """Consumes stream:event_aggregated and scores each Event."""

    def __init__(self, config: ScoringConfig, redis_config: RedisConfig) -> None:
        self._service = get_scoring_service()
        self._redis_stream = get_redis_stream_client()
        self._config = config
        self._reconnect_interval_s = redis_config.reconnect_interval_s

    async def start(self) -> None:
        """Consume stream:event_aggregated until cancelled."""
        await self._redis_stream.create_group_if_not_exists(
            EventAggregatedStream.NAME, EventAggregatedStream.CONSUMER_GROUP
        )
        await self._consumer_loop()

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

    async def _route(self, message: StreamMessage) -> None:
        """Score the message's Event, skipping a message without a valid event_id."""
        try:
            event_id = UUID(message.fields["event_id"])
        except (KeyError, ValueError):
            logger.warning("scoring_message_invalid", message_id=message.id, fields=message.fields)
            return
        await self._process_one(event_id, message.id)

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
