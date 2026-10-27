import asyncio
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.config import DatabaseConfig
from app.db import connection as connection_module
from app.db.connection import DatabaseClient
from app.db.errors import DatabaseError, UniqueConstraintError


def _make_config() -> DatabaseConfig:
    return DatabaseConfig(
        url="postgresql://user:pass@localhost/sapi",
        pool_size=10,
        close_timeout_s=10,
        max_retry=3,
        retry_base_wait_ms=100,
    )


def _make_client() -> tuple[DatabaseClient, MagicMock, AsyncMock]:
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    pool.close = AsyncMock()
    return DatabaseClient(pool, _make_config()), pool, conn


def _make_transaction_client() -> tuple[DatabaseClient, MagicMock, AsyncMock]:
    """A client whose pool hands out conn through `await pool.acquire()`, as transaction() takes
    it."""
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire = AsyncMock(return_value=conn)
    pool.release = AsyncMock()
    return DatabaseClient(pool, _make_config()), pool, conn


def _fail_on(statement: str, error: Exception):
    """A conn.execute side effect that raises error for one statement only."""

    async def execute(query: str, *args):
        if query == statement:
            raise error

    return execute


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    monkeypatch.setattr(connection_module, "_db_client", None)


async def fetch_one_row():
    client, _, conn = _make_client()
    record = MagicMock(spec=asyncpg.Record)
    conn.fetchrow = AsyncMock(return_value=record)

    row = await client.fetch_one("SELECT $1", 1)

    assert row is record
    conn.fetchrow.assert_awaited_once_with("SELECT $1", 1)


async def fetch_one_no_row():
    client, _, conn = _make_client()
    conn.fetchrow = AsyncMock(return_value=None)

    assert await client.fetch_one("SELECT 1 WHERE false") is None


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(asyncpg.PostgresConnectionError("lost"), id="postgres_connection_error"),
        pytest.param(asyncpg.InterfaceError("pool closed"), id="interface_error"),
        pytest.param(asyncpg.TooManyConnectionsError("too many"), id="too_many_connections"),
        pytest.param(OSError("refused"), id="os_error"),
        pytest.param(asyncpg.CannotConnectNowError("starting up"), id="cannot_connect_now"),
    ],
)
async def fetch_one_with_connection_failures(error):
    client, _, conn = _make_client()
    conn.fetchrow = AsyncMock(side_effect=error)

    with pytest.raises(ServiceUnavailableError) as excinfo:
        await client.fetch_one("SELECT 1")

    assert excinfo.value.__cause__ is error


async def fetch_one_with_acquire_connection_failure():
    client, pool, _ = _make_client()
    error = OSError("refused")
    pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=error)

    with pytest.raises(ServiceUnavailableError) as excinfo:
        await client.fetch_one("SELECT 1")

    assert excinfo.value.__cause__ is error


async def fetch_one_with_postgres_error():
    client, _, conn = _make_client()
    error = asyncpg.PostgresError("syntax error")
    conn.fetchrow = AsyncMock(side_effect=error)

    with pytest.raises(DatabaseError) as excinfo:
        await client.fetch_one("SELECT 1")

    assert excinfo.value.category is ErrorCategory.DB
    assert excinfo.value.__cause__ is error


async def fetch_all_success():
    client, _, conn = _make_client()
    records = [MagicMock(spec=asyncpg.Record), MagicMock(spec=asyncpg.Record)]
    conn.fetch = AsyncMock(return_value=records)

    rows = await client.fetch_all("SELECT $1", 1)

    assert rows == records
    conn.fetch.assert_awaited_once_with("SELECT $1", 1)


@pytest.mark.parametrize(
    ("error", "expected_cls"),
    [
        pytest.param(asyncpg.PostgresConnectionError("lost"), ServiceUnavailableError, id="lost"),
        pytest.param(asyncpg.PostgresError("syntax error"), DatabaseError, id="postgres"),
    ],
)
async def fetch_all_failure(error, expected_cls):
    client, _, conn = _make_client()
    conn.fetch = AsyncMock(side_effect=error)

    with pytest.raises(expected_cls) as excinfo:
        await client.fetch_all("SELECT 1")

    assert type(excinfo.value) is expected_cls
    assert excinfo.value.category is ErrorCategory.DB
    assert excinfo.value.__cause__ is error


async def transaction_success():
    client, pool, conn = _make_transaction_client()
    record = MagicMock(spec=asyncpg.Record)
    conn.fetchrow.return_value = record

    async with client.transaction() as tx:
        row = await tx.execute_returning("INSERT $1", 1)

    assert row is record
    assert conn.mock_calls == [
        mock.call.execute("BEGIN"),
        mock.call.fetchrow("INSERT $1", 1),
        mock.call.execute("COMMIT"),
    ]
    pool.release.assert_awaited_once_with(conn)


async def transaction_block_raises():
    client, pool, conn = _make_transaction_client()
    error = DatabaseError("insert failed", category=ErrorCategory.DB)

    with pytest.raises(DatabaseError) as excinfo:
        async with client.transaction():
            raise error

    assert excinfo.value is error
    assert conn.mock_calls == [mock.call.execute("BEGIN"), mock.call.execute("ROLLBACK")]
    pool.release.assert_awaited_once_with(conn)


async def transaction_rollback_fails():
    """A connection lost mid-transaction can't roll back; the block's own error is raised."""
    client, pool, conn = _make_transaction_client()
    conn.execute.side_effect = _fail_on("ROLLBACK", asyncpg.InterfaceError("connection is closed"))
    error = DatabaseError("insert failed", category=ErrorCategory.DB)

    with pytest.raises(DatabaseError) as excinfo:
        async with client.transaction():
            raise error

    assert excinfo.value is error
    pool.release.assert_awaited_once_with(conn)


async def transaction_commit_fails():
    client, pool, conn = _make_transaction_client()
    error = asyncpg.ConnectionDoesNotExistError("lost")
    conn.execute.side_effect = _fail_on("COMMIT", error)

    with pytest.raises(ServiceUnavailableError) as excinfo:
        async with client.transaction():
            pass

    assert excinfo.value.category is ErrorCategory.DB
    assert excinfo.value.__cause__ is error
    assert conn.mock_calls == [
        mock.call.execute("BEGIN"),
        mock.call.execute("COMMIT"),
        mock.call.execute("ROLLBACK"),
    ]
    pool.release.assert_awaited_once_with(conn)


@pytest.mark.parametrize(
    ("error", "expected_cls"),
    [
        pytest.param(
            asyncpg.ConnectionDoesNotExistError("lost"), ServiceUnavailableError, id="lost"
        ),
        pytest.param(asyncpg.StringDataRightTruncationError("too long"), DatabaseError, id="data"),
    ],
)
async def execute_returning_failure(error, expected_cls):
    """A failed statement is translated, not retried, and its transaction rolls back."""
    client, pool, conn = _make_transaction_client()
    conn.fetchrow.side_effect = error

    with pytest.raises(expected_cls) as excinfo:
        async with client.transaction() as tx:
            await tx.execute_returning("INSERT $1", 1)

    assert type(excinfo.value) is expected_cls
    assert excinfo.value.category is ErrorCategory.DB
    assert excinfo.value.__cause__ is error
    assert conn.mock_calls == [
        mock.call.execute("BEGIN"),
        mock.call.fetchrow("INSERT $1", 1),
        mock.call.execute("ROLLBACK"),
    ]
    pool.release.assert_awaited_once_with(conn)


async def execute_returning_unique_violation():
    """Like any failed statement, but the error also names the violated constraint."""
    client, pool, conn = _make_transaction_client()
    error = asyncpg.UniqueViolationError("dup")
    error.constraint_name = "uq_entity_events_source_url_stock_code"
    conn.fetchrow.side_effect = error

    with pytest.raises(UniqueConstraintError) as excinfo:
        async with client.transaction() as tx:
            await tx.execute_returning("INSERT $1", 1)

    assert excinfo.value.constraint_name == "uq_entity_events_source_url_stock_code"
    assert excinfo.value.category is ErrorCategory.DB
    assert excinfo.value.__cause__ is error
    assert conn.mock_calls == [
        mock.call.execute("BEGIN"),
        mock.call.fetchrow("INSERT $1", 1),
        mock.call.execute("ROLLBACK"),
    ]
    pool.release.assert_awaited_once_with(conn)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(asyncpg.TooManyConnectionsError("too many"), id="pool_exhausted"),
        pytest.param(asyncpg.ConnectionDoesNotExistError("lost"), id="connection_lost"),
        pytest.param(asyncpg.CannotConnectNowError("starting up"), id="starting_up"),
        pytest.param(ConnectionRefusedError("refused"), id="refused"),
    ],
)
async def transaction_open_retried(error):
    """A connection that can't be opened is retried with exponential backoff."""
    client, pool, conn = _make_transaction_client()
    pool.acquire.side_effect = [error, error, conn]

    with mock.patch("asyncio.sleep", AsyncMock()) as sleep:
        async with client.transaction():
            pass

    assert pool.acquire.await_count == 3
    assert sleep.await_args_list == [mock.call(0.1), mock.call(0.2)]
    assert conn.mock_calls == [mock.call.execute("BEGIN"), mock.call.execute("COMMIT")]


async def transaction_begin_retried():
    """A pooled connection that died while idle fails at BEGIN; it is released and another one
    is acquired."""
    client, pool, _ = _make_transaction_client()
    dead_conn, conn = AsyncMock(), AsyncMock()
    dead_conn.execute.side_effect = asyncpg.ConnectionDoesNotExistError("lost")
    pool.acquire.side_effect = [dead_conn, conn]

    with mock.patch("asyncio.sleep", AsyncMock()):
        async with client.transaction():
            pass

    assert pool.release.await_args_list == [mock.call(dead_conn), mock.call(conn)]
    assert conn.mock_calls == [mock.call.execute("BEGIN"), mock.call.execute("COMMIT")]


async def transaction_open_retries_exhausted():
    client, pool, _ = _make_transaction_client()
    error = ConnectionRefusedError("refused")
    pool.acquire.side_effect = error

    with (
        mock.patch("asyncio.sleep", AsyncMock()) as sleep,
        pytest.raises(ServiceUnavailableError) as excinfo,
    ):
        async with client.transaction():
            pytest.fail("the transaction should not open")

    assert excinfo.value.category is ErrorCategory.DB
    assert excinfo.value.__cause__ is error
    assert pool.acquire.await_count == 3
    assert sleep.await_args_list == [mock.call(0.1), mock.call(0.2)]


@pytest.mark.parametrize(
    ("error", "expected_cls"),
    [
        pytest.param(
            asyncpg.InterfaceError("pool is closed"), ServiceUnavailableError, id="closed"
        ),
        pytest.param(asyncpg.PostgresError("permission denied"), DatabaseError, id="postgres"),
    ],
)
async def transaction_open_not_retried(error, expected_cls):
    """A failure no fresh attempt can clear is raised at once."""
    client, pool, _ = _make_transaction_client()
    pool.acquire.side_effect = error

    with (
        mock.patch("asyncio.sleep", AsyncMock()) as sleep,
        pytest.raises(expected_cls) as excinfo,
    ):
        async with client.transaction():
            pytest.fail("the transaction should not open")

    assert type(excinfo.value) is expected_cls
    assert excinfo.value.category is ErrorCategory.DB
    assert excinfo.value.__cause__ is error
    assert pool.acquire.await_count == 1
    sleep.assert_not_awaited()


async def close():
    client, pool, _ = _make_client()

    await client.close(timeout_s=1)

    pool.close.assert_awaited_once()


async def close_pool_close_hangs():
    """Pool.close() never finishes while a connection is stuck; close() returns at its timeout."""
    client, pool, _ = _make_client()
    pool.close = AsyncMock(side_effect=asyncio.Event().wait)

    await asyncio.wait_for(client.close(timeout_s=0.05), timeout=1)


def get_db_client_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        connection_module.get_db_client()


async def create_db_client():
    config = _make_config()
    pool = MagicMock()

    with mock.patch.object(asyncpg, "create_pool", AsyncMock(return_value=pool)) as create_pool:
        client = await connection_module.create_db_client(config)

    create_pool.assert_awaited_once_with(dsn=config.url, min_size=2, max_size=config.pool_size)
    assert connection_module.get_db_client() is client
