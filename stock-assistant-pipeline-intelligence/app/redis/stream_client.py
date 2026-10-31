import asyncio
from dataclasses import dataclass

import redis.asyncio
import redis.exceptions
from redis.typing import EncodableT, FieldT

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.logger import get_logger

logger = get_logger(__name__)

_REDIS_UNREACHABLE = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


@dataclass
class StreamMessage:
    """One stream entry: its id and fields."""

    id: str
    fields: dict[str, str]


@dataclass
class ReclaimedMessage(StreamMessage):
    """A pending message claimed from an earlier delivery, with how many times it has been
    delivered, this claim included."""

    delivery_count: int


class RedisStreamClient:
    """Wrapper around the Redis connection dedicated to stream:* keys."""

    def __init__(self, client: redis.asyncio.Redis, reconnect_interval_s: int) -> None:
        self._client = client
        self._reconnect_interval_s = reconnect_interval_s

    async def publish(self, stream: str, fields: dict[str, str]) -> str:
        """
        Append a message to a stream and return its id. While the stream Redis is unreachable,
        retries every reconnect_interval_s until the message is written.
        """
        entry: dict[FieldT, EncodableT] = {key: value for key, value in fields.items()}
        while True:
            try:
                message_id: str = await self._client.xadd(stream, entry)
                return message_id
            except _REDIS_UNREACHABLE as exc:
                logger.warning(
                    "redis_stream_publish_retrying",
                    stream=stream,
                    retry_in_s=self._reconnect_interval_s,
                    error=repr(exc),
                )
                await asyncio.sleep(self._reconnect_interval_s)

    async def create_group_if_not_exists(self, stream: str, group: str) -> None:
        """Create the consumer group at the stream's start, creating the stream too if it is
        absent. A group that already exists is left as it is."""
        try:
            await self._client.xgroup_create(stream, group, id="0", mkstream=True)
        except redis.exceptions.ResponseError as exc:
            if not str(exc).startswith("BUSYGROUP"):
                raise
            logger.info("consumer_group_exists", stream=stream, group=group)

    async def read_from(
        self, stream: str, group: str, consumer: str, count: int | None, block_ms: int
    ) -> list[StreamMessage]:
        """
        Read new messages as part of a consumer group. Returns as soon as any are available,
        up to count (None means no limit); block_ms bounds the wait only while the stream is
        empty, and an empty list means it elapsed with nothing to read.
        Raises redis.ConnectionError / redis.TimeoutError if Redis is unreachable.
        """
        response = await self._client.xreadgroup(
            group, consumer, {stream: ">"}, count=count, block=block_ms
        )
        return [
            StreamMessage(id=message_id, fields=fields)
            for _stream, entries in response
            for message_id, fields in entries
        ]

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        """Acknowledge a message. Call only once every write its processing implies has
        committed."""
        await self._client.xack(stream, group, message_id)

    async def autoclaim(
        self, stream: str, group: str, consumer: str, min_idle_ms: int, count: int
    ) -> list[ReclaimedMessage]:
        """
        Claim up to count messages that have been pending longer than min_idle_ms, each with its
        delivery count.
        Raises redis.ConnectionError / redis.TimeoutError if Redis is unreachable.
        """
        _next_start_id, entries, _deleted_ids = await self._client.xautoclaim(
            stream, group, consumer, min_idle_time=min_idle_ms, count=count
        )
        claimed = [StreamMessage(id=message_id, fields=fields) for message_id, fields in entries]
        if not claimed:
            return []
        async with self._client.pipeline(transaction=False) as pipe:
            for message in claimed:
                pipe.xpending_range(stream, group, min=message.id, max=message.id, count=1)
            pending = await pipe.execute()
        # A message ACKed since the claim has no pending entry left, and is done.
        return [
            ReclaimedMessage(
                id=message.id,
                fields=message.fields,
                delivery_count=pending_entries[0]["times_delivered"],
            )
            for message, pending_entries in zip(claimed, pending, strict=True)
            if pending_entries
        ]

    async def ping(self) -> None:
        """
        Re-verify connectivity on demand. Raises redis.ConnectionError / redis.TimeoutError
        untranslated, so the caller can classify the raw signal itself.
        """
        await self._client.ping()

    async def close(self) -> None:
        """Close the Redis connection. Called in the lifespan shutdown handler."""
        await self._client.aclose()


_redis_stream_client: RedisStreamClient | None = None


def get_redis_stream_client() -> RedisStreamClient:
    """
    Return the process's one RedisStreamClient instance.
    Raises ServiceUnavailableError if called before create_stream_client() has run.
    """
    if _redis_stream_client is None:
        raise ServiceUnavailableError(
            "redis stream client is not initialized", category=ErrorCategory.REDIS
        )
    return _redis_stream_client


async def create_stream_client(redis_url: str, reconnect_interval_s: int) -> RedisStreamClient:
    """
    Create a Redis connection pool, verify connectivity with PING, wrap it in a
    RedisStreamClient and register it as this module's singleton.
    Raises redis.ConnectionError if Redis is unreachable.
    """
    global _redis_stream_client
    client = redis.asyncio.Redis.from_url(redis_url, decode_responses=True)
    try:
        await client.ping()
    except BaseException:
        await client.aclose()
        raise
    _redis_stream_client = RedisStreamClient(client, reconnect_interval_s)
    return _redis_stream_client
