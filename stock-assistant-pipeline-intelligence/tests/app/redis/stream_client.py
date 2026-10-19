from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.asyncio
import redis.exceptions

from app.common.errors import ServiceUnavailableError
from app.redis import stream_client as stream_client_module
from app.redis.stream_client import RedisStreamClient

REDIS_URL = "redis://localhost:6379/0"


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    monkeypatch.setattr(stream_client_module, "_redis_stream_client", None)


@pytest.fixture
def redis_mock():
    client = MagicMock()
    client.ping = AsyncMock(return_value=True)
    client.aclose = AsyncMock()
    return client


async def ping(redis_mock):
    await RedisStreamClient(redis_mock).ping()

    redis_mock.ping.assert_awaited_once()


async def close(redis_mock):
    await RedisStreamClient(redis_mock).close()

    redis_mock.aclose.assert_awaited_once()


def get_redis_stream_client_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        stream_client_module.get_redis_stream_client()


async def create_stream_client(redis_mock):
    with mock.patch.object(redis.asyncio.Redis, "from_url", return_value=redis_mock) as from_url:
        client = await stream_client_module.create_stream_client(REDIS_URL)

    from_url.assert_called_once_with(REDIS_URL, decode_responses=True)
    redis_mock.ping.assert_awaited_once()
    assert stream_client_module.get_redis_stream_client() is client


async def create_stream_client_unreachable(redis_mock):
    redis_mock.ping.side_effect = redis.exceptions.ConnectionError("down")

    with (
        mock.patch.object(redis.asyncio.Redis, "from_url", return_value=redis_mock),
        pytest.raises(redis.exceptions.ConnectionError),
    ):
        await stream_client_module.create_stream_client(REDIS_URL)

    redis_mock.aclose.assert_awaited_once()
    with pytest.raises(ServiceUnavailableError):
        stream_client_module.get_redis_stream_client()
