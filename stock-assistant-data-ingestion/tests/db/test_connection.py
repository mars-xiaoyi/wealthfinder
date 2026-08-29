import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import asyncpg
import pytest

from app.common.exceptions import ServiceUnavailableException
from app.config import DatabaseConfig
from app.db.connection import DatabaseClient, create_db_client
from app.db.exceptions import DatabaseError, UniqueConstraintError


def make_config(**overrides) -> DatabaseConfig:
    defaults = dict(url="postgresql://user:pass@localhost/db", pool_size=10, max_retry=3, retry_base_wait_ms=100)
    return DatabaseConfig(**{**defaults, **overrides})


def make_client(config: DatabaseConfig | None = None) -> tuple[DatabaseClient, MagicMock]:
    config = config or make_config()
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return DatabaseClient(pool, config), conn


# ---------------------------------------------------------------------------
# execute — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_success():
    client, conn = make_client()
    conn.execute = AsyncMock()
    await client.execute("INSERT INTO foo VALUES ($1)", "bar")
    conn.execute.assert_called_once_with("INSERT INTO foo VALUES ($1)", "bar")


# ---------------------------------------------------------------------------
# execute — asyncpg errors are translated to driver-agnostic exceptions,
# never leaked to the caller. Permanent ones raise immediately without retry.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_unique_violation_translated_with_constraint_name():
    client, conn = make_client()
    exc = asyncpg.UniqueViolationError("msg")
    exc.constraint_name = "uq_raw_news_raw_hash"
    conn.execute = AsyncMock(side_effect=exc)
    with pytest.raises(UniqueConstraintError) as excinfo:
        await client.execute("INSERT INTO foo VALUES ($1)", 1)
    assert excinfo.value.constraint_name == "uq_raw_news_raw_hash"
    conn.execute.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_class", [asyncpg.DataError, asyncpg.NotNullViolationError])
async def test_execute_data_integrity_error_translated_no_retry(exc_class):
    client, conn = make_client()
    conn.execute = AsyncMock(side_effect=exc_class("msg"))
    with pytest.raises(DatabaseError):
        await client.execute("INSERT INTO foo VALUES ($1)", 1)
    conn.execute.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_class", [asyncpg.PostgresConnectionError, asyncpg.InterfaceError])
async def test_execute_connection_error_translated_to_service_unavailable(exc_class):
    client, conn = make_client()
    conn.execute = AsyncMock(side_effect=exc_class("msg"))
    with pytest.raises(ServiceUnavailableException):
        await client.execute("INSERT INTO foo VALUES ($1)", 1)
    conn.execute.assert_called_once()


@pytest.mark.asyncio
async def test_execute_unclassified_postgres_error_translated_to_database_error():
    """Safety net: any Postgres-side error not explicitly classified above
    must still never leak a raw asyncpg type out of DatabaseClient."""
    client, conn = make_client()
    conn.execute = AsyncMock(side_effect=asyncpg.InternalServerError("weird"))
    with pytest.raises(DatabaseError):
        await client.execute("INSERT INTO foo VALUES ($1)", 1)


# ---------------------------------------------------------------------------
# execute — transient error retries then raises ServiceUnavailableException
# after exhausting attempts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_transient_error_retries_then_raises():
    config = make_config(max_retry=3, retry_base_wait_ms=10)
    client, conn = make_client(config)
    conn.execute = AsyncMock(side_effect=asyncpg.TooManyConnectionsError("busy"))

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(ServiceUnavailableException):
            await client.execute("INSERT INTO foo VALUES ($1)", 1)

    assert conn.execute.call_count == 3
    # Backoff: 10ms, 20ms (attempt 3 raises without sleeping)
    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args_list == [call(0.01), call(0.02)]


# ---------------------------------------------------------------------------
# execute — transient error succeeds on retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_transient_error_succeeds_on_retry():
    config = make_config(max_retry=3, retry_base_wait_ms=10)
    client, conn = make_client(config)
    conn.execute = AsyncMock(
        side_effect=[asyncpg.TooManyConnectionsError("busy"), None]
    )

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await client.execute("INSERT INTO foo VALUES ($1)", 1)

    assert conn.execute.call_count == 2
    assert mock_sleep.call_count == 1


# ---------------------------------------------------------------------------
# execute_returning — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_returning_returns_row():
    client, conn = make_client()
    fake_row = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fake_row)
    result = await client.execute_returning(
        "INSERT INTO foo VALUES ($1) RETURNING id", "bar"
    )
    assert result is fake_row
    conn.fetchrow.assert_called_once_with(
        "INSERT INTO foo VALUES ($1) RETURNING id", "bar"
    )


@pytest.mark.asyncio
async def test_execute_returning_returns_none_on_conflict_no_op():
    client, conn = make_client()
    conn.fetchrow = AsyncMock(return_value=None)
    result = await client.execute_returning(
        "INSERT INTO foo VALUES ($1) ON CONFLICT DO NOTHING RETURNING id", "bar"
    )
    assert result is None


# ---------------------------------------------------------------------------
# execute_returning — same translation contract as execute()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_returning_unique_violation_translated():
    client, conn = make_client()
    exc = asyncpg.UniqueViolationError("msg")
    exc.constraint_name = "uq_raw_news_raw_hash"
    conn.fetchrow = AsyncMock(side_effect=exc)
    with pytest.raises(UniqueConstraintError) as excinfo:
        await client.execute_returning("INSERT INTO foo VALUES ($1) RETURNING id", 1)
    assert excinfo.value.constraint_name == "uq_raw_news_raw_hash"
    conn.fetchrow.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_class", [asyncpg.DataError, asyncpg.NotNullViolationError])
async def test_execute_returning_data_integrity_error_translated(exc_class):
    client, conn = make_client()
    conn.fetchrow = AsyncMock(side_effect=exc_class("msg"))
    with pytest.raises(DatabaseError):
        await client.execute_returning("INSERT INTO foo VALUES ($1) RETURNING id", 1)
    conn.fetchrow.assert_called_once()


# ---------------------------------------------------------------------------
# execute_returning — transient error retries then raises ServiceUnavailableException
# after exhausting attempts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_returning_transient_error_retries_then_raises():
    config = make_config(max_retry=3, retry_base_wait_ms=10)
    client, conn = make_client(config)
    conn.fetchrow = AsyncMock(side_effect=asyncpg.TooManyConnectionsError("busy"))

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(ServiceUnavailableException):
            await client.execute_returning("INSERT INTO foo VALUES ($1) RETURNING id", 1)

    assert conn.fetchrow.call_count == 3
    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args_list == [call(0.01), call(0.02)]


# ---------------------------------------------------------------------------
# execute_returning — transient error succeeds on retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_returning_transient_error_succeeds_on_retry():
    config = make_config(max_retry=3, retry_base_wait_ms=10)
    client, conn = make_client(config)
    fake_row = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[asyncpg.TooManyConnectionsError("busy"), fake_row]
    )

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await client.execute_returning(
            "INSERT INTO foo VALUES ($1) RETURNING id", 1
        )

    assert result is fake_row
    assert conn.fetchrow.call_count == 2
    assert mock_sleep.call_count == 1


# ---------------------------------------------------------------------------
# fetch_one
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_one_returns_row():
    client, conn = make_client()
    fake_row = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fake_row)
    result = await client.fetch_one("SELECT * FROM foo WHERE id = $1", 1)
    assert result is fake_row
    conn.fetchrow.assert_called_once_with("SELECT * FROM foo WHERE id = $1", 1)


@pytest.mark.asyncio
async def test_fetch_one_returns_none_when_not_found():
    client, conn = make_client()
    conn.fetchrow = AsyncMock(return_value=None)
    result = await client.fetch_one("SELECT * FROM foo WHERE id = $1", 999)
    assert result is None


@pytest.mark.asyncio
async def test_fetch_one_connection_error_translated_no_retry():
    client, conn = make_client()
    conn.fetchrow = AsyncMock(side_effect=asyncpg.InterfaceError("conn closed"))
    with pytest.raises(ServiceUnavailableException):
        await client.fetch_one("SELECT * FROM foo WHERE id = $1", 1)
    # No retry on reads — must fail on the first attempt.
    conn.fetchrow.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_one_unclassified_postgres_error_translated():
    client, conn = make_client()
    conn.fetchrow = AsyncMock(side_effect=asyncpg.InternalServerError("weird"))
    with pytest.raises(DatabaseError):
        await client.fetch_one("SELECT * FROM foo WHERE id = $1", 1)


# ---------------------------------------------------------------------------
# fetch_all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_all_returns_rows():
    client, conn = make_client()
    fake_rows = [MagicMock(), MagicMock()]
    conn.fetch = AsyncMock(return_value=fake_rows)
    result = await client.fetch_all("SELECT * FROM foo")
    assert result == fake_rows


@pytest.mark.asyncio
async def test_fetch_all_returns_empty_list():
    client, conn = make_client()
    conn.fetch = AsyncMock(return_value=[])
    result = await client.fetch_all("SELECT * FROM foo WHERE 1=0")
    assert result == []


@pytest.mark.asyncio
async def test_fetch_all_connection_error_translated_no_retry():
    client, conn = make_client()
    conn.fetch = AsyncMock(side_effect=asyncpg.TooManyConnectionsError("busy"))
    with pytest.raises(ServiceUnavailableException):
        await client.fetch_all("SELECT * FROM foo")
    conn.fetch.assert_called_once()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_calls_pool_close():
    config = make_config()
    pool = MagicMock()
    pool.close = AsyncMock()
    client = DatabaseClient(pool, config)
    await client.close()
    pool.close.assert_called_once()


# ---------------------------------------------------------------------------
# create_db_client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_db_client_creates_pool_and_returns_client():
    config = make_config()
    fake_pool = MagicMock()

    with patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=fake_pool):
        client = await create_db_client(config)

    assert isinstance(client, DatabaseClient)
    assert client._pool is fake_pool
    assert client._config is config
