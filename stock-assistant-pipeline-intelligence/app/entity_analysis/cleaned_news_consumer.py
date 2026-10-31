import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

import redis.exceptions

from app.common.errors import SAPIError
from app.common.hk_stock_list import get_hk_stock_list_cache
from app.config import EntityAnalysisConfig, RedisConfig
from app.entity_analysis.entity_analysis_service import get_entity_analysis_service
from app.logger import get_logger
from app.redis.stream_client import ReclaimedMessage, StreamMessage, get_redis_stream_client
from app.redis.stream_enum import EntityAnalysisDeadLetterStream, RawNewsCleanedStream

logger = get_logger(__name__)

_REDIS_UNREACHABLE = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


def _get_valid_cleaned_id(message: StreamMessage) -> UUID | None:
    """The message's cleaned_id, or None if the message is not one this layer can process."""
    if message.fields.get("v") != RawNewsCleanedStream.VERSION:
        return None
    try:
        return UUID(message.fields["cleaned_id"])
    except (KeyError, ValueError):
        return None


def _get_delivery_count(message: StreamMessage) -> int:
    return message.delivery_count if isinstance(message, ReclaimedMessage) else 1


class CleanedNewsConsumer:
    """Consumes stream:raw_news_cleaned and analyses each article."""

    def __init__(self, config: EntityAnalysisConfig, redis_config: RedisConfig) -> None:
        self._service = get_entity_analysis_service()
        self._redis_stream = get_redis_stream_client()
        self._config = config
        self._reconnect_interval_s = redis_config.reconnect_interval_s
        self._claim_timeout_ms = redis_config.stream_claim_timeout_ms
        self._processing_cleaned_ids: set[UUID] = set()

    async def start(self) -> None:
        """Consume stream:raw_news_cleaned until cancelled. Raises ExceptionGroup if consumption
        stops on an unexpected error."""
        await self._redis_stream.create_group_if_not_exists(
            RawNewsCleanedStream.NAME, RawNewsCleanedStream.CONSUMER_GROUP
        )
        async with asyncio.TaskGroup() as group:
            group.create_task(self._consumer_loop())
            group.create_task(self._reclaim_loop())

    async def _consumer_loop(self) -> None:
        """Read batches and route each message until cancelled, riding out periods when the
        stream Redis is unreachable."""
        while True:
            try:
                messages = await self._redis_stream.read_from(
                    RawNewsCleanedStream.NAME,
                    RawNewsCleanedStream.CONSUMER_GROUP,
                    self._config.consumer_name,
                    count=self._config.batch_size,
                    block_ms=self._config.batch_timeout_ms,
                )
                for message in messages:
                    await self._route(message)
            except _REDIS_UNREACHABLE:
                # Any unprocessed rest of the batch stays pending, un-ACKed.
                logger.warning(
                    "entity_analysis_redis_stream_unreachable",
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
                    RawNewsCleanedStream.NAME,
                    RawNewsCleanedStream.CONSUMER_GROUP,
                    self._config.consumer_name,
                    min_idle_ms=self._claim_timeout_ms,
                    count=self._config.batch_size,
                )
                for message in messages:
                    await self._route(message)
            except _REDIS_UNREACHABLE:
                logger.warning(
                    "entity_analysis_redis_stream_unreachable",
                    retry_in_s=self._claim_timeout_ms / 1000,
                    exc_info=True,
                )
            except Exception:
                logger.warning("entity_analysis_reclaim_sweep_failed", exc_info=True)

    async def _route(self, message: StreamMessage) -> None:
        """Dead-letter a message that is out of retries or malformed, and process any other."""
        try:
            if (
                isinstance(message, ReclaimedMessage)
                and message.delivery_count >= self._config.max_retry
            ):
                await self._push_dead_letter(message, reason="retries_exhausted")
                return
            cleaned_id = _get_valid_cleaned_id(message)
            if cleaned_id is None:
                await self._push_dead_letter(message, reason="malformed")
                return
            await self._process_one(cleaned_id, message.id)
        except _REDIS_UNREACHABLE:
            raise
        except Exception:
            logger.warning(
                "entity_analysis_message_handling_failed", message_id=message.id, exc_info=True
            )

    async def _push_dead_letter(self, message: StreamMessage, reason: str) -> None:
        """Move the message to the dead-letter stream."""
        delivery_count = _get_delivery_count(message)
        await self._redis_stream.publish(
            EntityAnalysisDeadLetterStream.NAME,
            {
                "message_id": message.id,
                "fields": json.dumps(message.fields),
                "delivery_count": str(delivery_count),
                "failed_at": datetime.now(UTC).isoformat(),
                "v": EntityAnalysisDeadLetterStream.VERSION,
            },
        )
        await self._redis_stream.ack(
            RawNewsCleanedStream.NAME, RawNewsCleanedStream.CONSUMER_GROUP, message.id
        )
        logger.warning(
            "entity_analysis_message_dead_lettered",
            message_id=message.id,
            delivery_count=delivery_count,
            reason=reason,
        )

    async def _process_one(self, cleaned_id: UUID, message_id: str) -> None:
        """Analyse one message's article and ACK the message once its outcome is final:
        analysed, or not found in SADI. Any other outcome, including an HK Stock List not loaded
        yet or the same article already in flight, leaves it un-ACKed."""
        if len(get_hk_stock_list_cache()) == 0:
            logger.warning("hk_stock_list_not_ready", message_id=message_id)
            return

        # No await between this check and the insert, so two loops can't both pass it.
        if cleaned_id in self._processing_cleaned_ids:
            return
        self._processing_cleaned_ids.add(cleaned_id)

        try:
            await self._service.find_entity_events(cleaned_id)
            await self._redis_stream.ack(
                RawNewsCleanedStream.NAME, RawNewsCleanedStream.CONSUMER_GROUP, message_id
            )
        except _REDIS_UNREACHABLE:
            raise
        except SAPIError as exc:
            logger.log(
                exc.log_level,
                exc.dev_message,
                cleaned_id=str(cleaned_id),
                message_id=message_id,
                exc_info=exc,
            )
        except Exception as exc:
            logger.error(
                f"Unhandled exception: {exc!r}",
                cleaned_id=str(cleaned_id),
                message_id=message_id,
                exc_info=exc,
            )
        finally:
            self._processing_cleaned_ids.discard(cleaned_id)
