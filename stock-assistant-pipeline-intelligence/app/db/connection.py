import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.config import DatabaseConfig
from app.db.errors import DatabaseError, UniqueConstraintError
from app.logger import get_logger

logger = get_logger(__name__)

# Failures a fresh connection attempt can clear: an exhausted pool, a refused or reset TCP
# connection (OSError), a dropped connection, a server still starting up.
_TRANSIENT_ERRORS = (
    asyncpg.TooManyConnectionsError,
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    OSError,
)
# Every failure meaning the database can't be reached; InterfaceError covers a closed pool or
# connection.
_CONNECTION_ERRORS = (*_TRANSIENT_ERRORS, asyncpg.InterfaceError)


@contextmanager
def _translated_errors() -> Iterator[None]:
    """Re-raise asyncpg's errors from inside the block as SAPI errors."""
    try:
        yield
    except asyncpg.UniqueViolationError as exc:
        raise UniqueConstraintError(exc.constraint_name or "", category=ErrorCategory.DB) from exc
    except _CONNECTION_ERRORS as exc:
        raise ServiceUnavailableError(
            f"DB connection error: {exc!r}", category=ErrorCategory.DB
        ) from exc
    except asyncpg.PostgresError as exc:
        raise DatabaseError(f"DB error: {exc!r}", category=ErrorCategory.DB) from exc


async def _db_write_with_retry[T](config: DatabaseConfig, write: Callable[[], Awaitable[T]]) -> T:
    """
    Run write, retrying it with exponential backoff while it fails in a way a fresh attempt
    can clear.
    Raises ServiceUnavailableError once max_retry attempts have failed or the database is
    unreachable, UniqueConstraintError or DatabaseError on any other database failure.
    """
    with _translated_errors():
        attempt = 1
        while True:
            try:
                return await write()
            except _TRANSIENT_ERRORS as exc:
                if attempt >= config.max_retry:
                    raise
                wait_s = config.retry_base_wait_ms * 2 ** (attempt - 1) / 1000
                logger.warning(
                    "db_write_retrying",
                    attempt=attempt,
                    max_retry=config.max_retry,
                    retry_in_s=wait_s,
                    error=repr(exc),
                )
                await asyncio.sleep(wait_s)
                attempt += 1


class TransactionConnection:
    """A connection inside an open transaction: its statements commit or roll back together."""

    def __init__(self, conn: PoolConnectionProxy) -> None:
        self._conn = conn

    async def execute_returning(self, query: str, *args: object) -> asyncpg.Record | None:
        """
        Execute a write statement with a RETURNING clause and return its first row, or None if
        it affected no rows.
        Raises ServiceUnavailableError if the connection is lost, UniqueConstraintError or
        DatabaseError on any other database failure.
        """
        with _translated_errors():
            return await self._conn.fetchrow(query, *args)


class DatabaseClient:
    """
    Wraps the asyncpg connection pool. asyncpg is an implementation detail of this module
    only — every method translates asyncpg's exceptions into the driver-agnostic types in
    app.db.errors / app.common.errors before returning control to the caller.
    """

    def __init__(self, pool: asyncpg.Pool, config: DatabaseConfig) -> None:
        self._pool = pool
        self._config = config

    async def fetch_one(self, query: str, *args: object) -> asyncpg.Record | None:
        """
        Execute a SELECT and return the first matching row, or None. No retry — reads are
        safe to re-issue by the caller.
        Raises ServiceUnavailableError on a connection failure, DatabaseError on
        any other Postgres error.
        """
        with _translated_errors():
            async with self._pool.acquire() as conn:
                return await conn.fetchrow(query, *args)

    async def fetch_all(self, query: str, *args: object) -> list[asyncpg.Record]:
        """
        Execute a SELECT and return every matching row.
        Raises ServiceUnavailableError on a connection failure, DatabaseError on any other
        Postgres error.
        """
        with _translated_errors():
            async with self._pool.acquire() as conn:
                return await conn.fetch(query, *args)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[TransactionConnection]:
        """
        Open a transaction for the block's statements. It commits when the block exits
        normally and rolls back when the block raises.
        Raises ServiceUnavailableError if the database is unreachable, DatabaseError on any
        other database failure.
        """
        conn = await _db_write_with_retry(self._config, self._begin)
        try:
            yield TransactionConnection(conn)
            with _translated_errors():
                await conn.execute("COMMIT")
        except BaseException:
            # A lost connection can't roll back; the server discards its transaction anyway.
            with suppress(Exception):
                await conn.execute("ROLLBACK")
            raise
        finally:
            await self._pool.release(conn)

    async def _begin(self) -> PoolConnectionProxy:
        """Acquire a pool connection and open a transaction on it."""
        conn = await self._pool.acquire()
        try:
            await conn.execute("BEGIN")
        except BaseException:
            await self._pool.release(conn)
            raise
        return conn

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
        raise ServiceUnavailableError(
            "database client is not initialized", category=ErrorCategory.DB
        )
    return _db_client


async def create_db_client(config: DatabaseConfig) -> DatabaseClient:
    """
    Create the asyncpg pool, wrap it in a DatabaseClient and register it as this module's
    singleton. This is the only place asyncpg.create_pool() is called.
    Called once in the FastAPI lifespan startup handler; raises if the DB is unreachable.
    """
    global _db_client
    pool = await asyncpg.create_pool(dsn=config.url, min_size=2, max_size=config.pool_size)
    _db_client = DatabaseClient(pool, config)
    return _db_client
