import logging
from dataclasses import dataclass
from uuid import UUID

from app.redis.stream_client import (
    STREAM_RAW_NEWS_CLEANED,
    STREAM_RAW_NEWS_INSERTED,
    StreamClient,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamMessage:
    """A message read from the raw_news_inserted stream."""
    message_id: str
    raw_id: str


class StreamHandler:
    """Wraps StreamClient with cleaning-layer-specific constants and logic."""

    STREAM_IN = STREAM_RAW_NEWS_INSERTED
    STREAM_OUT = STREAM_RAW_NEWS_CLEANED
    GROUP_NAME = "sadi-cleaner"
    CONSUMER_NAME = "sadi-cleaner-1"  # One consumer in MVP; expand for horizontal scaling

    def __init__(self, stream_client: StreamClient):
        self._client = stream_client

    async def ensure_consumer_group(self) -> None:
        """Create the consumer group if it doesn't exist. Call once at startup."""
        await self._client.create_group_if_not_exists(self.STREAM_IN, self.GROUP_NAME)
        logger.info(
            "[stream_handler] consumer group '%s' ensured on '%s'",
            self.GROUP_NAME,
            self.STREAM_IN,
        )

    def _to_stream_messages(self, raw: list[dict]) -> list[StreamMessage]:
        return [
            StreamMessage(message_id=msg["id"], raw_id=msg["fields"]["raw_id"])
            for msg in raw
        ]

    async def read_messages(self, count: int = 10) -> list[StreamMessage]:
        """
        Read up to `count` new messages from stream:raw_news_inserted.
        Blocks up to 5000 ms; returns an empty list if no messages arrive.
        """
        raw = await self._client.read_group(
            stream=self.STREAM_IN,
            group=self.GROUP_NAME,
            consumer=self.CONSUMER_NAME,
            count=count,
            block_ms=5000,
        )
        return self._to_stream_messages(raw)

    async def reclaim_pending(self, min_idle_ms: int, count: int = 10) -> list[StreamMessage]:
        """
        Reclaim messages that have been pending longer than min_idle_ms.
        Called periodically to recover from worker crashes.
        """
        raw = await self._client.autoclaim(
            stream=self.STREAM_IN,
            group=self.GROUP_NAME,
            consumer=self.CONSUMER_NAME,
            min_idle_ms=min_idle_ms,
            count=count,
        )
        if raw:
            logger.info(
                "[stream_handler] reclaimed %d pending messages (min_idle_ms=%d)",
                len(raw),
                min_idle_ms,
            )
        return self._to_stream_messages(raw)

    async def ack(self, message_id: str) -> None:
        """
        Acknowledge a successfully processed message.
        Call ONLY after cleaned_news record is written AND stream:raw_news_cleaned is published.
        """
        await self._client.ack(self.STREAM_IN, self.GROUP_NAME, message_id)

    async def publish_cleaned(self, cleaned_id: UUID) -> None:
        """
        Publish to stream:raw_news_cleaned after a record is successfully cleaned.
        SAPI NLP layer consumes this stream.
        """
        await self._client.publish(self.STREAM_OUT, {"cleaned_id": str(cleaned_id)})
        logger.debug(
            "[stream_handler] published cleaned_id=%s to %s",
            cleaned_id,
            self.STREAM_OUT,
        )
