import redis.asyncio

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError


class RedisStreamClient:
    """Wrapper around the Redis connection dedicated to stream:* keys."""

    def __init__(self, client: redis.asyncio.Redis) -> None:
        self._client = client

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


async def create_stream_client(redis_url: str) -> RedisStreamClient:
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
    _redis_stream_client = RedisStreamClient(client)
    return _redis_stream_client
