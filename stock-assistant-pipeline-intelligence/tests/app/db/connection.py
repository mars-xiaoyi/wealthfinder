import asyncio
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from app.common.errors import ServiceUnavailableError
from app.config import DatabaseConfig
from app.db import connection as connection_module
from app.db.connection import DatabaseClient


def _make_config() -> DatabaseConfig:
    return DatabaseConfig(
        url="postgresql://user:pass@localhost/sapi", pool_size=10, close_timeout_s=10
    )


def _make_client() -> tuple[DatabaseClient, MagicMock, AsyncMock]:
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    pool.close = AsyncMock()
    return DatabaseClient(pool), pool, conn


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
