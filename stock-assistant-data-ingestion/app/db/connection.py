import asyncio
import logging
from typing import Optional

import asyncpg

from app.config import DatabaseConfig

logger = logging.getLogger(__name__)


class DatabaseClient:
    def __init__(self, pool: asyncpg.Pool, config: DatabaseConfig):
        self._pool = pool
        self._config = config

    async def execute(self, query: str, *args) -> None:
        """
        Execute a write query (INSERT/UPDATE) with retry on transient failures.
        Raises asyncpg.PostgresError on permanent failure or after retries exhausted.
        """
        _TRANSIENT = (
            asyncpg.TooManyConnectionsError,
            asyncpg.ConnectionDoesNotExistError,
        )
        _PERMANENT = (
            asyncpg.UniqueViolationError,
            asyncpg.DataError,
            asyncpg.NotNullViolationError,
        )

        for attempt in range(1, self._config.max_retry + 1):
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(query, *args)
                return
            except _PERMANENT:
                raise
            except _TRANSIENT as exc:
                if attempt == self._config.max_retry:
                    raise
                wait_s = (self._config.retry_base_wait_ms * (2 ** (attempt - 1))) / 1000
                logger.warning(
                    "Transient DB error on attempt %d/%d, retrying in %.3fs: %s",
                    attempt,
                    self._config.max_retry,
                    wait_s,
                    exc,
                )
                await asyncio.sleep(wait_s)

    async def fetch_one(self, query: str, *args) -> Optional[asyncpg.Record]:
        """
        Execute a SELECT and return the first matching row, or None.
        No retry — reads are safe to re-issue by the caller.
        """
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetch_all(self, query: str, *args) -> list[asyncpg.Record]:
        """
        Execute a SELECT and return all matching rows.
        No retry.
        """
        async with self._pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def close(self) -> None:
        """Gracefully close the connection pool. Called in the lifespan shutdown handler."""
        await self._pool.close()


async def create_db_client(config: DatabaseConfig) -> DatabaseClient:
    """
    Create the asyncpg pool and wrap it in a DatabaseClient.
    This is the only place asyncpg.create_pool() is called.
    Called once in the FastAPI lifespan startup handler.
    """
    pool = await asyncpg.create_pool(
        dsn=config.url,
        min_size=2,
        max_size=config.pool_size,
    )
    return DatabaseClient(pool, config)
