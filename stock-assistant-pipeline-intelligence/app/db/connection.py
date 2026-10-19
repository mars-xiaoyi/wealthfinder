import asyncio

import asyncpg

from app.common.errors import ServiceUnavailableError
from app.config import DatabaseConfig
from app.db.errors import DatabaseError
from app.logger import get_logger

logger = get_logger(__name__)

# OSError covers a refused/reset TCP connection while the pool reconnects.
_CONNECTION_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.TooManyConnectionsError,
    OSError,
)


class DatabaseClient:
    """
    Wraps the asyncpg connection pool. asyncpg is an implementation detail of this module
    only — every method translates asyncpg's exceptions into the driver-agnostic types in
    app.db.errors / app.common.errors before returning control to the caller.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def fetch_one(self, query: str, *args: object) -> asyncpg.Record | None:
        """
        Execute a SELECT and return the first matching row, or None. No retry — reads are
        safe to re-issue by the caller.
        Raises ServiceUnavailableError on a connection failure, DatabaseError on
        any other Postgres error.
        """
        try:
            async with self._pool.acquire() as conn:
                return await conn.fetchrow(query, *args)
        except _CONNECTION_ERRORS as exc:
            raise ServiceUnavailableError("DB connection error during read") from exc
        except asyncpg.PostgresError as exc:
            raise DatabaseError(str(exc)) from exc

    async def close(self, timeout_s: float) -> None:
        """
        Gracefully close the connection pool, called in the lifespan shutdown handler.
        Pool.close() waits for every acquired connection to be released, which never happens
        while a query is stuck on an unresponsive server; past timeout_s, asyncpg terminates
        the pool instead.
        """
        try:
            await asyncio.wait_for(self._pool.close(), timeout=timeout_s)
        except TimeoutError:
            logger.warning("db_pool_close_timed_out", timeout_s=timeout_s)


_db_client: DatabaseClient | None = None


def get_db_client() -> DatabaseClient:
    """
    Return the process's one DatabaseClient instance.
    Raises ServiceUnavailableError if called before create_db_client() has run.
    """
    if _db_client is None:
        raise ServiceUnavailableError("database client is not initialized")
    return _db_client


async def create_db_client(config: DatabaseConfig) -> DatabaseClient:
    """
    Create the asyncpg pool, wrap it in a DatabaseClient and register it as this module's
    singleton. This is the only place asyncpg.create_pool() is called.
    Called once in the FastAPI lifespan startup handler; raises if the DB is unreachable.
    """
    global _db_client
    pool = await asyncpg.create_pool(dsn=config.url, min_size=2, max_size=config.pool_size)
    _db_client = DatabaseClient(pool)
    return _db_client
