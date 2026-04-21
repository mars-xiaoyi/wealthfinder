import logging

import redis.asyncio

logger = logging.getLogger(__name__)

# Stream name constants — import from here everywhere. Never use magic strings.
STREAM_RAW_NEWS_INSERTED = "stream:raw_news_inserted"  # CrawlService → StreamHandler
STREAM_RAW_NEWS_CLEANED = "stream:raw_news_cleaned"    # CleaningService → SAPI
STREAM_CRAWL_COMPLETED = "stream:crawl_completed"      # CrawlService → Admin


class StreamClient:
    def __init__(self, client: redis.asyncio.Redis):
        self._client = client

    async def publish(self, stream: str, fields: dict) -> str:
        """
        Write a message to a Redis Stream (XADD).
        All values in fields must be strings.
        Returns the Redis message ID.
        """
        message_id = await self._client.xadd(stream, fields)
        logger.debug("Published to %s: id=%s fields=%s", stream, message_id, fields)
        return message_id

    async def read_group(
        self,
        stream: str,
        group: str,
        consumer: str,
        count: int,
        block_ms: int,
    ) -> list[dict]:
        """
        Read messages from a stream as part of a consumer group (XREADGROUP).

        count    — maximum number of messages to return per call. Use a small
                   number (e.g. 10) to bound per-batch processing time.
        block_ms — how long to wait for new messages before returning, in
                   milliseconds. 0 blocks indefinitely; use a finite value
                   (e.g. 5000) so the caller can perform periodic housekeeping
                   such as reclaim_pending between polls.

        Returns a list of {"id": ..., "fields": {...}} dicts.
        Returns empty list if block_ms elapses with no messages.
        """
        results = await self._client.xreadgroup(
            groupname=group,
            consumername=consumer,
            streams={stream: ">"},
            count=count,
            block=block_ms,
        )
        if not results:
            return []
        messages = []
        for _stream_name, entries in results:
            for message_id, fields in entries:
                messages.append({"id": message_id, "fields": fields})
        return messages

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        """
        Acknowledge a processed message (XACK).
        Call ONLY after the record is written to cleaned_news AND the stream signal sent.
        """
        await self._client.xack(stream, group, message_id)

    async def autoclaim(
        self,
        stream: str,
        group: str,
        consumer: str,
        min_idle_ms: int,
        count: int,
    ) -> list[dict]:
        """
        Reclaim messages pending longer than min_idle_ms (XAUTOCLAIM).
        Returns messages in the same format as read_group.
        """
        result = await self._client.xautoclaim(
            stream,
            group,
            consumer,
            min_idle_time=min_idle_ms,
            count=count,
        )
        # xautoclaim returns (next_start_id, [(id, fields), ...], ...)
        entries = result[1]
        return [{"id": message_id, "fields": fields} for message_id, fields in entries]

    async def create_group_if_not_exists(self, stream: str, group: str) -> None:
        """
        Create a consumer group starting from the beginning of the stream.
        Safe to call on startup — ignores BUSYGROUP if the group already exists.
        Uses MKSTREAM so the stream is created if it doesn't exist yet.
        """
        try:
            await self._client.xgroup_create(stream, group, id="0", mkstream=True)
        except redis.asyncio.ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                logger.debug("Consumer group '%s' on '%s' already exists.", group, stream)
            else:
                raise

    async def ping(self) -> bool:
        return await self._client.ping()

    async def close(self) -> None:
        """Close the Redis connection."""
        await self._client.aclose()


async def create_stream_client(redis_url: str) -> StreamClient:
    """
    Create a Redis connection pool, verify connectivity with PING, and return
    a StreamClient wrapping it.
    This is the only place redis.asyncio.Redis is instantiated.
    Raises ConnectionError if Redis is unreachable.
    """
    client = redis.asyncio.Redis.from_url(redis_url, decode_responses=True)
    await client.ping()
    return StreamClient(client)
