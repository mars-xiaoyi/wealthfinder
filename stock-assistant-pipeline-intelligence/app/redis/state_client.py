import inspect
from collections.abc import Callable

import redis.asyncio
import redis.asyncio.client

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError


class RedisStateClient:
    """Thin generic wrapper around the Redis connection for every non-stream key."""

    def __init__(self, client: redis.asyncio.Redis) -> None:
        self._client = client

    async def hgetall(self, key: str) -> dict[str, str] | None:
        """Return every field of the hash at key, or None if the key does not exist."""
        response = self._client.hgetall(key)
        # redis-py types its hash commands for its sync and async clients alike.
        assert inspect.isawaitable(response)
        fields: dict[str, str] = await response
        return fields or None

    async def scan(self, pattern: str) -> list[str]:
        """Return every key matching the glob-style pattern, each once."""
        # SCAN may return a key more than once.
        return list(dict.fromkeys([key async for key in self._client.scan_iter(match=pattern)]))

    async def multi_exec(self, build: Callable[[redis.asyncio.client.Pipeline], None]) -> None:
        """Apply the commands build queues on the pipeline as one transaction."""
        async with self._client.pipeline(transaction=True) as pipe:
            build(pipe)
            await pipe.execute()

    async def ping(self) -> None:
        """
        Re-verify connectivity on demand. Raises redis.ConnectionError / redis.TimeoutError
        untranslated, so the caller can classify the raw signal itself.
        """
        await self._client.ping()

    async def close(self) -> None:
        """Close the Redis connection. Called in the lifespan shutdown handler."""
        await self._client.aclose()


_redis_state_client: RedisStateClient | None = None


def get_redis_state_client() -> RedisStateClient:
    """
    Return the process's one RedisStateClient instance.
    Raises ServiceUnavailableError if called before create_state_client() has run.
    """
    if _redis_state_client is None:
        raise ServiceUnavailableError(
            "redis state client is not initialized", category=ErrorCategory.REDIS
        )
    return _redis_state_client


async def create_state_client(redis_url: str) -> RedisStateClient:
    """
    Create a Redis connection pool, verify connectivity with PING, wrap it in a
    RedisStateClient and register it as this module's singleton.
    Raises redis.ConnectionError if Redis is unreachable.
    """
    global _redis_state_client
    client = redis.asyncio.Redis.from_url(redis_url, decode_responses=True)
    try:
        await client.ping()
    except BaseException:
        await client.aclose()
        raise
    _redis_state_client = RedisStateClient(client)
    return _redis_state_client
