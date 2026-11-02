from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.asyncio
import redis.exceptions

from app.common.errors import ServiceUnavailableError
from app.redis import state_client as state_client_module
from app.redis.state_client import RedisStateClient

REDIS_URL = "redis://localhost:6379/1"


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    monkeypatch.setattr(state_client_module, "_redis_state_client", None)


@pytest.fixture
def redis_mock():
    client = MagicMock()
    client.ping = AsyncMock(return_value=True)
    client.aclose = AsyncMock()
    return client


async def hgetall_success(redis_mock):
    redis_mock.hgetall = AsyncMock(return_value={"event_id": "e1"})

    fields = await RedisStateClient(redis_mock).hgetall("agg:event:HKEX:00700:EARNINGS")

    assert fields == {"event_id": "e1"}
    redis_mock.hgetall.assert_awaited_once_with("agg:event:HKEX:00700:EARNINGS")


async def hgetall_missing_key(redis_mock):
    redis_mock.hgetall = AsyncMock(return_value={})

    fields = await RedisStateClient(redis_mock).hgetall("agg:event:HKEX:00700:EARNINGS")

    assert fields is None


async def multi_exec_success(redis_mock):
    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[1, True])
    pipeline = MagicMock()
    pipeline.__aenter__ = AsyncMock(return_value=pipe)
    pipeline.__aexit__ = AsyncMock(return_value=None)
    redis_mock.pipeline = MagicMock(return_value=pipeline)

    def build(queued) -> None:
        queued.hset("agg:event:HKEX:00700:EARNINGS", mapping={"event_id": "e1"})
        queued.expire("agg:event:HKEX:00700:EARNINGS", 15000)

    await RedisStateClient(redis_mock).multi_exec(build)

    redis_mock.pipeline.assert_called_once_with(transaction=True)
    assert pipe.mock_calls == [
        mock.call.hset("agg:event:HKEX:00700:EARNINGS", mapping={"event_id": "e1"}),
        mock.call.expire("agg:event:HKEX:00700:EARNINGS", 15000),
        mock.call.execute(),
    ]


async def ping(redis_mock):
    await RedisStateClient(redis_mock).ping()

    redis_mock.ping.assert_awaited_once()


async def close(redis_mock):
    await RedisStateClient(redis_mock).close()

    redis_mock.aclose.assert_awaited_once()


def get_redis_state_client_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        state_client_module.get_redis_state_client()


async def create_state_client(redis_mock):
    with mock.patch.object(redis.asyncio.Redis, "from_url", return_value=redis_mock) as from_url:
        client = await state_client_module.create_state_client(REDIS_URL)

    from_url.assert_called_once_with(REDIS_URL, decode_responses=True)
    redis_mock.ping.assert_awaited_once()
    assert state_client_module.get_redis_state_client() is client


async def create_state_client_unreachable(redis_mock):
    redis_mock.ping.side_effect = redis.exceptions.ConnectionError("down")

    with (
        mock.patch.object(redis.asyncio.Redis, "from_url", return_value=redis_mock),
        pytest.raises(redis.exceptions.ConnectionError),
    ):
        await state_client_module.create_state_client(REDIS_URL)

    redis_mock.aclose.assert_awaited_once()
    with pytest.raises(ServiceUnavailableError):
        state_client_module.get_redis_state_client()
