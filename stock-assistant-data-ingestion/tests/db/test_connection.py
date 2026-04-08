import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import asyncpg
import pytest

from app.config import DatabaseConfig
from app.db.connection import DatabaseClient, create_db_client


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
# execute — permanent errors raise immediately without retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("exc_class", [
    asyncpg.UniqueViolationError,
    asyncpg.DataError,
    asyncpg.NotNullViolationError,
])
async def test_execute_permanent_error_no_retry(exc_class):
    client, conn = make_client()
    conn.execute = AsyncMock(side_effect=exc_class("msg"))
    with pytest.raises(exc_class):
        await client.execute("INSERT INTO foo VALUES ($1)", 1)
    conn.execute.assert_called_once()


# ---------------------------------------------------------------------------
# execute — transient error retries then raises after exhausting attempts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_transient_error_retries_then_raises():
    config = make_config(max_retry=3, retry_base_wait_ms=10)
    client, conn = make_client(config)
    conn.execute = AsyncMock(side_effect=asyncpg.TooManyConnectionsError("busy"))

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(asyncpg.TooManyConnectionsError):
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
