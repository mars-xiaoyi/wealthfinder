import asyncio
import logging
from typing import Optional

import asyncpg

from app.common.exceptions import ServiceUnavailableException
from app.config import DatabaseConfig
from app.db.exceptions import DatabaseError, UniqueConstraintError

logger = logging.getLogger(__name__)


class DatabaseClient:
    """
    Wraps the asyncpg connection pool. asyncpg is an implementation detail of
    this module only — every method here translates asyncpg's exceptions into
    the driver-agnostic types in app.db.exceptions / app.common.exceptions
    before returning control to the caller. No other module should import
    asyncpg to catch or inspect an error raised through this client.
    """

    def __init__(self, pool: asyncpg.Pool, config: DatabaseConfig):
        self._pool = pool
        self._config = config

    # Connection-level failures worth retrying with backoff before giving up.
    _TRANSIENT = (
        asyncpg.TooManyConnectionsError,
        asyncpg.ConnectionDoesNotExistError,
    )

    async def _run_with_retry(self, op):
        """
        Shared retry loop for write queries. ``op`` is an async callable that
        takes the acquired connection and returns whatever the caller wants
        back (None for execute(), the RETURNING row for execute_returning()).

        Raises UniqueConstraintError / DatabaseError / ServiceUnavailableException
        — never a raw asyncpg exception.
        """
        for attempt in range(1, self._config.max_retry + 1):
            try:
                async with self._pool.acquire() as conn:
                    return await op(conn)
            except asyncpg.UniqueViolationError as exc:
                raise UniqueConstraintError(getattr(exc, "constraint_name", None)) from exc
            except (asyncpg.DataError, asyncpg.NotNullViolationError) as exc:
                raise DatabaseError(str(exc)) from exc
            except self._TRANSIENT as exc:
                if attempt == self._config.max_retry:
                    logger.error(
                        "DB connection retries exhausted: %s", exc, exc_info=True
                    )
                    raise ServiceUnavailableException("Database is unavailable") from exc
                wait_s = (self._config.retry_base_wait_ms * (2 ** (attempt - 1))) / 1000
                logger.warning(
                    "Transient DB error on attempt %d/%d, retrying in %.3fs: %s",
                    attempt,
                    self._config.max_retry,
                    wait_s,
                    exc,
                )
                await asyncio.sleep(wait_s)
            except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError) as exc:
                logger.error("DB connection error: %s", exc, exc_info=True)
                raise ServiceUnavailableException("Database is unavailable") from exc
            except asyncpg.PostgresError as exc:
                # Safety net for any Postgres-side error not classified above.
                raise DatabaseError(str(exc)) from exc

    async def execute(self, query: str, *args) -> None:
        """
        Execute a write query (INSERT/UPDATE) with retry on transient failures.
        """
        await self._run_with_retry(lambda conn: conn.execute(query, *args))

    async def execute_returning(self, query: str, *args) -> Optional[asyncpg.Record]:
        """
        Execute a write query with a RETURNING clause (e.g. an
        INSERT ... ON CONFLICT DO NOTHING RETURNING <col>), with the same
        retry-on-transient-failure semantics as execute().

        Returns the first returned row, or None if the statement affected no
        rows (e.g. an ON CONFLICT DO NOTHING no-op swallowed the write).
        """
        return await self._run_with_retry(lambda conn: conn.fetchrow(query, *args))

    async def _run_read(self, op):
        """Shared error translation for reads. No retry — reads are safe to re-issue by the caller."""
        try:
            async with self._pool.acquire() as conn:
                return await op(conn)
        except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, *self._TRANSIENT) as exc:
            logger.error("DB connection error during read: %s", exc, exc_info=True)
            raise ServiceUnavailableException("Database is unavailable") from exc
        except asyncpg.PostgresError as exc:
            raise DatabaseError(str(exc)) from exc

    async def fetch_one(self, query: str, *args) -> Optional[asyncpg.Record]:
        """Execute a SELECT and return the first matching row, or None."""
        return await self._run_read(lambda conn: conn.fetchrow(query, *args))

    async def fetch_all(self, query: str, *args) -> list[asyncpg.Record]:
        """Execute a SELECT and return all matching rows."""
        return await self._run_read(lambda conn: conn.fetch(query, *args))

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
