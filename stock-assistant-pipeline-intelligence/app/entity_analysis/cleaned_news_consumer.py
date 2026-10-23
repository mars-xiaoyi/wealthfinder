import asyncio
from uuid import UUID

import redis.exceptions

from app.clients.news_client import get_news_client
from app.common.errors import SAPIError
from app.config import EntityAnalysisConfig, RedisConfig
from app.logger import get_logger
from app.redis.stream_client import (
    CONSUMER_GROUP_ENTITY_ANALYSIS,
    STREAM_RAW_NEWS_CLEANED,
    StreamMessage,
    get_redis_stream_client,
)

logger = get_logger(__name__)

_REDIS_UNREACHABLE = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


class CleanedNewsConsumer:
    """Consumes stream:raw_news_cleaned and fetches each article from SADI."""

    def __init__(self, config: EntityAnalysisConfig, redis_config: RedisConfig) -> None:
        self._news_client = get_news_client()
        self._redis_stream = get_redis_stream_client()
        self._config = config
        self._reconnect_interval_s = redis_config.reconnect_interval_s

    async def start(self) -> None:
        """Create the consumer group, then consume the stream until cancelled."""
        await self._redis_stream.create_group_if_not_exists(
            STREAM_RAW_NEWS_CLEANED, CONSUMER_GROUP_ENTITY_ANALYSIS
        )
        await self._consumer_loop()

    async def _consumer_loop(self) -> None:
        """Read batches and process each message until cancelled, riding out periods when the
        stream Redis is unreachable."""
        while True:
            try:
                messages = await self._redis_stream.read_from(
                    STREAM_RAW_NEWS_CLEANED,
                    CONSUMER_GROUP_ENTITY_ANALYSIS,
                    self._config.consumer_name,
                    count=self._config.batch_size,
                    block_ms=self._config.batch_timeout_ms,
                )
                for message in messages:
                    await self._process_one(message)
            except _REDIS_UNREACHABLE:
                # Any unprocessed rest of the batch stays pending, un-ACKed.
                logger.warning(
                    "entity_analysis_redis_stream_unreachable",
                    retry_in_s=self._reconnect_interval_s,
                    exc_info=True,
                )
                await asyncio.sleep(self._reconnect_interval_s)

    async def _process_one(self, message: StreamMessage) -> None:
        """Fetch one message's article and ACK the message once its outcome is final: fetched,
        or not found in SADI. Any other outcome leaves it un-ACKed."""
        try:
            cleaned_id = UUID(message.fields["cleaned_id"])
        except (KeyError, ValueError):
            logger.warning(
                "entity_analysis_message_invalid", message_id=message.id, fields=message.fields
            )
            return

        try:
            news = await self._news_client.fetch_cleaned_news(cleaned_id)
            if news is None:
                logger.warning("cleaned_news_not_found", cleaned_id=str(cleaned_id))
            else:
                logger.info("cleaned_news_fetched", cleaned_id=str(cleaned_id))
            await self._redis_stream.ack(
                STREAM_RAW_NEWS_CLEANED, CONSUMER_GROUP_ENTITY_ANALYSIS, message.id
            )
        except _REDIS_UNREACHABLE:
            raise
        except SAPIError as exc:
            logger.log(
                exc.log_level,
                exc.dev_message,
                cleaned_id=str(cleaned_id),
                message_id=message.id,
                exc_info=exc,
            )
        except Exception as exc:
            logger.error(
                f"Unhandled exception: {exc!r}",
                cleaned_id=str(cleaned_id),
                message_id=message.id,
                exc_info=exc,
            )
