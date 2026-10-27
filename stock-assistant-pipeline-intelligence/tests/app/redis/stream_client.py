from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.asyncio
import redis.exceptions

from app.common.errors import ServiceUnavailableError
from app.redis import stream_client as stream_client_module
from app.redis.stream_client import RedisStreamClient, StreamMessage

REDIS_URL = "redis://localhost:6379/0"
STREAM = "stream:test"
GROUP = "test-group"
RECONNECT_INTERVAL_S = 5


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    monkeypatch.setattr(stream_client_module, "_redis_stream_client", None)


@pytest.fixture
def redis_mock():
    client = MagicMock()
    client.ping = AsyncMock(return_value=True)
    client.aclose = AsyncMock()
    client.xgroup_create = AsyncMock(return_value=True)
    client.xreadgroup = AsyncMock(return_value=[])
    client.xack = AsyncMock(return_value=1)
    client.xadd = AsyncMock(return_value="1-0")
    return client


async def create_group_if_not_exists(redis_mock):
    await RedisStreamClient(redis_mock, RECONNECT_INTERVAL_S).create_group_if_not_exists(
        STREAM, GROUP
    )

    redis_mock.xgroup_create.assert_awaited_once_with(STREAM, GROUP, id="0", mkstream=True)


async def create_group_if_not_exists_already_exists(redis_mock):
    redis_mock.xgroup_create.side_effect = redis.exceptions.ResponseError(
        "BUSYGROUP Consumer Group name already exists"
    )

    await RedisStreamClient(redis_mock, RECONNECT_INTERVAL_S).create_group_if_not_exists(
        STREAM, GROUP
    )

    redis_mock.xgroup_create.assert_awaited_once_with(STREAM, GROUP, id="0", mkstream=True)


async def create_group_if_not_exists_other_response_error(redis_mock):
    redis_mock.xgroup_create.side_effect = redis.exceptions.ResponseError("WRONGTYPE")

    with pytest.raises(redis.exceptions.ResponseError):
        await RedisStreamClient(redis_mock, RECONNECT_INTERVAL_S).create_group_if_not_exists(
            STREAM, GROUP
        )


async def publish_success(redis_mock):
    message_id = await RedisStreamClient(redis_mock, RECONNECT_INTERVAL_S).publish(
        STREAM, {"entity_event_id": "a", "v": "1"}
    )

    assert message_id == "1-0"
    redis_mock.xadd.assert_awaited_once_with(STREAM, {"entity_event_id": "a", "v": "1"})


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(redis.exceptions.ConnectionError("down"), id="connection_error"),
        pytest.param(redis.exceptions.TimeoutError("timed out"), id="timeout"),
    ],
)
async def publish_redis_unreachable(redis_mock, error):
    """While Redis is unreachable the same message is retried, one reconnect interval apart."""
    redis_mock.xadd.side_effect = [error, error, "3-0"]

    with mock.patch("asyncio.sleep", AsyncMock()) as sleep:
        message_id = await RedisStreamClient(redis_mock, RECONNECT_INTERVAL_S).publish(
            STREAM, {"entity_event_id": "a", "v": "1"}
        )

    assert message_id == "3-0"
    assert (
        redis_mock.xadd.await_args_list
        == [mock.call(STREAM, {"entity_event_id": "a", "v": "1"})] * 3
    )
    assert sleep.await_args_list == [mock.call(RECONNECT_INTERVAL_S)] * 2


async def read_from(redis_mock):
    redis_mock.xreadgroup.return_value = [
        [STREAM, [("1-0", {"cleaned_id": "a", "v": "1"}), ("2-0", {"cleaned_id": "b", "v": "1"})]]
    ]

    messages = await RedisStreamClient(redis_mock, RECONNECT_INTERVAL_S).read_from(
        STREAM, GROUP, "consumer-1", count=5, block_ms=200
    )

    assert messages == [
        StreamMessage(id="1-0", fields={"cleaned_id": "a", "v": "1"}),
        StreamMessage(id="2-0", fields={"cleaned_id": "b", "v": "1"}),
    ]
    redis_mock.xreadgroup.assert_awaited_once_with(
        GROUP, "consumer-1", {STREAM: ">"}, count=5, block=200
    )


async def read_from_empty(redis_mock):
    messages = await RedisStreamClient(redis_mock, RECONNECT_INTERVAL_S).read_from(
        STREAM, GROUP, "consumer-1", count=None, block_ms=200
    )

    assert messages == []
    redis_mock.xreadgroup.assert_awaited_once_with(
        GROUP, "consumer-1", {STREAM: ">"}, count=None, block=200
    )


async def ack(redis_mock):
    await RedisStreamClient(redis_mock, RECONNECT_INTERVAL_S).ack(STREAM, GROUP, "1-0")

    redis_mock.xack.assert_awaited_once_with(STREAM, GROUP, "1-0")


async def ping(redis_mock):
    await RedisStreamClient(redis_mock, RECONNECT_INTERVAL_S).ping()

    redis_mock.ping.assert_awaited_once()


async def close(redis_mock):
    await RedisStreamClient(redis_mock, RECONNECT_INTERVAL_S).close()

    redis_mock.aclose.assert_awaited_once()


def get_redis_stream_client_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        stream_client_module.get_redis_stream_client()


async def create_stream_client(redis_mock):
    with mock.patch.object(redis.asyncio.Redis, "from_url", return_value=redis_mock) as from_url:
        client = await stream_client_module.create_stream_client(REDIS_URL, RECONNECT_INTERVAL_S)

    from_url.assert_called_once_with(REDIS_URL, decode_responses=True)
    redis_mock.ping.assert_awaited_once()
    assert stream_client_module.get_redis_stream_client() is client


async def create_stream_client_unreachable(redis_mock):
    redis_mock.ping.side_effect = redis.exceptions.ConnectionError("down")

    with (
        mock.patch.object(redis.asyncio.Redis, "from_url", return_value=redis_mock),
        pytest.raises(redis.exceptions.ConnectionError),
    ):
        await stream_client_module.create_stream_client(REDIS_URL, RECONNECT_INTERVAL_S)

    redis_mock.aclose.assert_awaited_once()
    with pytest.raises(ServiceUnavailableError):
        stream_client_module.get_redis_stream_client()
