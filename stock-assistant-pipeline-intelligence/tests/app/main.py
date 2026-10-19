from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.exceptions
from fastapi import FastAPI

from app import main as main_module
from app.config import AppConfig, DatabaseConfig, HealthConfig, RedisConfig

CONFIG = AppConfig(
    db=DatabaseConfig(url="postgresql://localhost/sapi", pool_size=10, close_timeout_s=10),
    health=HealthConfig(check_timeout_s=2),
    redis=RedisConfig(stream_url="redis://localhost/0", state_url="redis://localhost/1"),
)


@pytest.fixture
def infra():
    """Patch every create_X() main.py calls; a shared parent mock records call order."""
    calls = MagicMock()
    db, redis_stream, redis_state = AsyncMock(), AsyncMock(), AsyncMock()
    calls.attach_mock(db.close, "db_close")
    calls.attach_mock(redis_stream.close, "redis_stream_close")
    calls.attach_mock(redis_state.close, "redis_state_close")
    create_db = AsyncMock(return_value=db)
    create_stream = AsyncMock(return_value=redis_stream)
    create_state = AsyncMock(return_value=redis_state)
    calls.attach_mock(create_db, "create_db_client")
    calls.attach_mock(create_stream, "create_stream_client")
    calls.attach_mock(create_state, "create_state_client")
    with (
        mock.patch.object(main_module, "load_config", return_value=CONFIG),
        mock.patch.object(main_module, "create_db_client", create_db),
        mock.patch.object(main_module, "create_stream_client", create_stream),
        mock.patch.object(main_module, "create_state_client", create_state),
    ):
        yield calls


def _call_names(calls: MagicMock) -> list[str]:
    return [name for name, _args, _kwargs in calls.mock_calls]


async def lifespan(infra):
    async with main_module.lifespan(FastAPI()):
        assert _call_names(infra) == [
            "create_db_client",
            "create_stream_client",
            "create_state_client",
        ]
        infra.create_db_client.assert_awaited_once_with(CONFIG.db)
        infra.create_stream_client.assert_awaited_once_with(CONFIG.redis.stream_url)
        infra.create_state_client.assert_awaited_once_with(CONFIG.redis.state_url)

    assert _call_names(infra)[3:] == ["redis_state_close", "redis_stream_close", "db_close"]
    infra.db_close.assert_awaited_once_with(CONFIG.db.close_timeout_s)


async def lifespan_startup_failure(infra):
    infra.create_state_client.side_effect = redis.exceptions.ConnectionError("down")

    with pytest.raises(redis.exceptions.ConnectionError):
        async with main_module.lifespan(FastAPI()):
            pytest.fail("startup should not complete")

    assert _call_names(infra)[3:] == ["redis_stream_close", "db_close"]
