from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.exceptions
from fastapi import FastAPI

from app import main as main_module
from app.common.error_categories import ErrorCategory
from app.common.errors import ParseError, ServiceUnavailableError
from app.config import AppConfig, DatabaseConfig, HealthConfig, HttpConfig, RedisConfig

CONFIG = AppConfig(
    db=DatabaseConfig(url="postgresql://localhost/sapi", pool_size=10, close_timeout_s=10),
    http=HttpConfig(timeout_s=120),
    health=HealthConfig(check_timeout_s=2),
    redis=RedisConfig(stream_url="redis://localhost/0", state_url="redis://localhost/1"),
)


@pytest.fixture
def infra():
    """Patch every create_X() main.py calls; a shared parent mock records call order."""
    calls = MagicMock()
    db, redis_stream, redis_state = AsyncMock(), AsyncMock(), AsyncMock()
    http_client = MagicMock()
    http_client.aclose = AsyncMock()
    calls.attach_mock(db.close, "db_close")
    calls.attach_mock(redis_stream.close, "redis_stream_close")
    calls.attach_mock(redis_state.close, "redis_state_close")
    calls.attach_mock(http_client.aclose, "http_client_aclose")
    create_db = AsyncMock(return_value=db)
    create_stream = AsyncMock(return_value=redis_stream)
    create_state = AsyncMock(return_value=redis_state)
    create_http = MagicMock(return_value=http_client)
    refresh = AsyncMock(return_value=2800)
    calls.attach_mock(create_db, "create_db_client")
    calls.attach_mock(create_stream, "create_stream_client")
    calls.attach_mock(create_state, "create_state_client")
    calls.attach_mock(create_http, "create_http_client")
    calls.attach_mock(refresh, "refresh_hk_stock_list")
    with (
        mock.patch.object(main_module, "load_config", return_value=CONFIG),
        mock.patch.object(main_module, "create_db_client", create_db),
        mock.patch.object(main_module, "create_stream_client", create_stream),
        mock.patch.object(main_module, "create_state_client", create_state),
        mock.patch.object(main_module, "create_http_client", create_http),
        mock.patch.object(main_module, "refresh_hk_stock_list", refresh),
    ):
        yield calls


def _call_names(calls: MagicMock) -> list[str]:
    return [name for name, _args, _kwargs in calls.mock_calls]


STARTUP_CALLS = [
    "create_db_client",
    "create_stream_client",
    "create_state_client",
    "create_http_client",
    "refresh_hk_stock_list",
]
SHUTDOWN_CALLS = ["http_client_aclose", "redis_state_close", "redis_stream_close", "db_close"]


async def lifespan(infra):
    async with main_module.lifespan(FastAPI()):
        assert _call_names(infra) == STARTUP_CALLS
        infra.create_db_client.assert_awaited_once_with(CONFIG.db)
        infra.create_stream_client.assert_awaited_once_with(CONFIG.redis.stream_url)
        infra.create_state_client.assert_awaited_once_with(CONFIG.redis.state_url)
        infra.create_http_client.assert_called_once_with(CONFIG.http.timeout_s)
        infra.refresh_hk_stock_list.assert_awaited_once_with()

    assert _call_names(infra) == STARTUP_CALLS + SHUTDOWN_CALLS
    infra.db_close.assert_awaited_once_with(CONFIG.db.close_timeout_s)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            ServiceUnavailableError("HKEXnews unreachable", category=ErrorCategory.STOCK_LIST),
            id="unreachable",
        ),
        pytest.param(
            ParseError("shape mismatch", category=ErrorCategory.STOCK_LIST),
            id="unparseable",
        ),
    ],
)
async def lifespan_hk_stock_list_load_failed(infra, error):
    """Unlike the infra connects, a failed HK Stock List load must not abort startup."""
    infra.refresh_hk_stock_list.side_effect = error

    async with main_module.lifespan(FastAPI()):
        assert _call_names(infra) == STARTUP_CALLS

    assert _call_names(infra) == STARTUP_CALLS + SHUTDOWN_CALLS


async def lifespan_startup_failure(infra):
    infra.create_state_client.side_effect = redis.exceptions.ConnectionError("down")

    with pytest.raises(redis.exceptions.ConnectionError):
        async with main_module.lifespan(FastAPI()):
            pytest.fail("startup should not complete")

    assert _call_names(infra) == [
        "create_db_client",
        "create_stream_client",
        "create_state_client",
        "redis_stream_close",
        "db_close",
    ]
