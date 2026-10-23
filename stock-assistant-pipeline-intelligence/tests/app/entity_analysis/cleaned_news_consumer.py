import asyncio
from unittest import mock
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import redis.exceptions

from app.clients import news_client as news_client_module
from app.clients.news_client import CleanedNews, NewsClient
from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.config import EntityAnalysisConfig, RedisConfig
from app.entity_analysis.cleaned_news_consumer import CleanedNewsConsumer
from app.redis import stream_client as stream_client_module
from app.redis.stream_client import (
    CONSUMER_GROUP_ENTITY_ANALYSIS,
    STREAM_RAW_NEWS_CLEANED,
    RedisStreamClient,
    StreamMessage,
)

CONFIG = EntityAnalysisConfig(
    batch_size=5, batch_timeout_ms=200, sadi_api_url="http://sadi:8000", consumer_name="pod-1"
)
REDIS_CONFIG = RedisConfig(
    stream_url="redis://localhost/0", state_url="redis://localhost/1", reconnect_interval_s=3
)
ID_A = UUID("00000000-0000-0000-0000-00000000000a")
ID_B = UUID("00000000-0000-0000-0000-00000000000b")
MESSAGE_A = StreamMessage(id="1-0", fields={"cleaned_id": str(ID_A), "v": "1"})
MESSAGE_B = StreamMessage(id="2-0", fields={"cleaned_id": str(ID_B), "v": "1"})
NEWS_A = CleanedNews(
    cleaned_id=ID_A,
    title="title",
    body="body",
    source_url="https://example.com/a",
    source_name="MINGPAO",
    published_at=None,
)


@pytest.fixture
def redis_stream(monkeypatch):
    client = AsyncMock(spec=RedisStreamClient)
    monkeypatch.setattr(stream_client_module, "_redis_stream_client", client)
    return client


@pytest.fixture
def news_client(monkeypatch):
    client = AsyncMock(spec=NewsClient)
    monkeypatch.setattr(news_client_module, "_news_client", client)
    return client


async def _run(redis_stream: AsyncMock, *batches: object) -> None:
    """Run start() over the given read_from() results (a list of messages, or an exception
    to raise), then stop it by cancelling on the next read."""
    redis_stream.read_from.side_effect = [*batches, asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await CleanedNewsConsumer(CONFIG, REDIS_CONFIG).start()


def _read_call() -> object:
    return mock.call(
        STREAM_RAW_NEWS_CLEANED,
        CONSUMER_GROUP_ENTITY_ANALYSIS,
        CONFIG.consumer_name,
        count=CONFIG.batch_size,
        block_ms=CONFIG.batch_timeout_ms,
    )


def _ack_call(message: StreamMessage) -> object:
    return mock.call(STREAM_RAW_NEWS_CLEANED, CONSUMER_GROUP_ENTITY_ANALYSIS, message.id)


async def start_acks_fetched_and_not_found(redis_stream, news_client):
    """A fetched article and one SADI doesn't have are both final outcomes: both ACKed."""
    news_client.fetch_cleaned_news.side_effect = [NEWS_A, None]

    await _run(redis_stream, [MESSAGE_A, MESSAGE_B], [])

    redis_stream.create_group_if_not_exists.assert_awaited_once_with(
        STREAM_RAW_NEWS_CLEANED, CONSUMER_GROUP_ENTITY_ANALYSIS
    )
    assert redis_stream.read_from.await_args_list == [_read_call()] * 3
    assert news_client.fetch_cleaned_news.await_args_list == [mock.call(ID_A), mock.call(ID_B)]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_B)]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            ServiceUnavailableError.upstream_unavailable(
                "SADI down", category=ErrorCategory.NEWS_CLIENT
            ),
            id="sadi_failure",
        ),
        pytest.param(RuntimeError("bug"), id="unexpected"),
    ],
)
async def start_failed_fetch_left_unacked(redis_stream, news_client, error):
    """A failed fetch leaves its message un-ACKed, and the rest of the batch and the loop
    carry on."""
    news_client.fetch_cleaned_news.side_effect = [error, NEWS_A]

    await _run(redis_stream, [MESSAGE_A, MESSAGE_B], [])

    assert news_client.fetch_cleaned_news.await_args_list == [mock.call(ID_A), mock.call(ID_B)]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B)]
    assert redis_stream.read_from.await_count == 3


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"v": "1"}, id="missing_cleaned_id"),
        pytest.param({"cleaned_id": "not-a-uuid", "v": "1"}, id="invalid_cleaned_id"),
    ],
)
async def start_invalid_message_left_unacked(redis_stream, news_client, fields):
    news_client.fetch_cleaned_news.return_value = NEWS_A

    await _run(redis_stream, [StreamMessage(id="1-0", fields=fields), MESSAGE_B])

    assert news_client.fetch_cleaned_news.await_args_list == [mock.call(ID_B)]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B)]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(redis.exceptions.ConnectionError("down"), id="connection_error"),
        pytest.param(redis.exceptions.TimeoutError("hung"), id="timeout"),
    ],
)
async def start_redis_unreachable_on_read(redis_stream, news_client, error):
    """An unreachable stream Redis is retried after the reconnect interval, not raised."""
    news_client.fetch_cleaned_news.return_value = NEWS_A

    with mock.patch("asyncio.sleep", new=AsyncMock()) as sleep:
        await _run(redis_stream, error, error, [MESSAGE_A])

    assert sleep.await_args_list == [mock.call(REDIS_CONFIG.reconnect_interval_s)] * 2
    assert redis_stream.read_from.await_count == 4
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]


async def start_redis_unreachable_on_ack(redis_stream, news_client):
    """A failed ACK goes through the same retry, leaving the rest of its batch pending."""
    news_client.fetch_cleaned_news.return_value = NEWS_A
    redis_stream.ack.side_effect = redis.exceptions.ConnectionError("down")

    with mock.patch("asyncio.sleep", new=AsyncMock()) as sleep:
        await _run(redis_stream, [MESSAGE_A, MESSAGE_B])

    assert sleep.await_args_list == [mock.call(REDIS_CONFIG.reconnect_interval_s)]
    assert news_client.fetch_cleaned_news.await_args_list == [mock.call(ID_A)]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]
    assert redis_stream.read_from.await_count == 2
