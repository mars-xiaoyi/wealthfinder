from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis.asyncio

from app.redis.stream_client import (
    STREAM_CRAWL_COMPLETED,
    STREAM_RAW_NEWS_CLEANED,
    STREAM_RAW_NEWS_INSERTED,
    StreamClient,
    create_stream_client,
)


def make_client() -> tuple[StreamClient, AsyncMock]:
    redis_mock = AsyncMock(spec=redis.asyncio.Redis)
    return StreamClient(redis_mock), redis_mock


# ---------------------------------------------------------------------------
# Stream name constants
# ---------------------------------------------------------------------------

def test_stream_name_constants():
    assert STREAM_RAW_NEWS_INSERTED == "stream:raw_news_inserted"
    assert STREAM_RAW_NEWS_CLEANED == "stream:raw_news_cleaned"
    assert STREAM_CRAWL_COMPLETED == "stream:crawl_completed"


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_returns_message_id():
    client, r = make_client()
    r.xadd = AsyncMock(return_value="1712345678901-0")
    msg_id = await client.publish(STREAM_RAW_NEWS_INSERTED, {"raw_id": "abc"})
    assert msg_id == "1712345678901-0"
    r.xadd.assert_called_once_with(STREAM_RAW_NEWS_INSERTED, {"raw_id": "abc"})


# ---------------------------------------------------------------------------
# read_group
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_group_returns_messages():
    client, r = make_client()
    r.xreadgroup = AsyncMock(return_value=[
        ("stream:raw_news_inserted", [("1-0", {"raw_id": "abc"}), ("2-0", {"raw_id": "def"})]),
    ])
    messages = await client.read_group(STREAM_RAW_NEWS_INSERTED, "sadi-cleaner", "worker-1", 10, 5000)
    assert messages == [
        {"id": "1-0", "fields": {"raw_id": "abc"}},
        {"id": "2-0", "fields": {"raw_id": "def"}},
    ]
    r.xreadgroup.assert_called_once_with(
        groupname="sadi-cleaner",
        consumername="worker-1",
        streams={STREAM_RAW_NEWS_INSERTED: ">"},
        count=10,
        block=5000,
    )


@pytest.mark.asyncio
async def test_read_group_returns_empty_on_timeout():
    client, r = make_client()
    r.xreadgroup = AsyncMock(return_value=None)
    messages = await client.read_group(STREAM_RAW_NEWS_INSERTED, "sadi-cleaner", "worker-1", 10, 5000)
    assert messages == []


# ---------------------------------------------------------------------------
# ack
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ack_calls_xack():
    client, r = make_client()
    r.xack = AsyncMock()
    await client.ack(STREAM_RAW_NEWS_INSERTED, "sadi-cleaner", "1-0")
    r.xack.assert_called_once_with(STREAM_RAW_NEWS_INSERTED, "sadi-cleaner", "1-0")


# ---------------------------------------------------------------------------
# autoclaim
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_autoclaim_returns_reclaimed_messages():
    client, r = make_client()
    r.xautoclaim = AsyncMock(return_value=(
        "0-0",
        [("3-0", {"raw_id": "xyz"})],
    ))
    messages = await client.autoclaim(STREAM_RAW_NEWS_INSERTED, "sadi-cleaner", "worker-1", 30000, 5)
    assert messages == [{"id": "3-0", "fields": {"raw_id": "xyz"}}]
    r.xautoclaim.assert_called_once_with(
        STREAM_RAW_NEWS_INSERTED,
        "sadi-cleaner",
        "worker-1",
        min_idle_time=30000,
        count=5,
    )


@pytest.mark.asyncio
async def test_autoclaim_returns_empty_when_no_pending():
    client, r = make_client()
    r.xautoclaim = AsyncMock(return_value=("0-0", []))
    messages = await client.autoclaim(STREAM_RAW_NEWS_INSERTED, "sadi-cleaner", "worker-1", 30000, 5)
    assert messages == []


# ---------------------------------------------------------------------------
# create_group_if_not_exists
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_group_creates_new_group():
    client, r = make_client()
    r.xgroup_create = AsyncMock(return_value=True)
    await client.create_group_if_not_exists(STREAM_RAW_NEWS_INSERTED, "sadi-cleaner")
    r.xgroup_create.assert_called_once_with(
        STREAM_RAW_NEWS_INSERTED, "sadi-cleaner", id="0", mkstream=True
    )


@pytest.mark.asyncio
async def test_create_group_ignores_busygroup_error():
    client, r = make_client()
    r.xgroup_create = AsyncMock(
        side_effect=redis.asyncio.ResponseError("BUSYGROUP Consumer Group name already exists")
    )
    # Should not raise
    await client.create_group_if_not_exists(STREAM_RAW_NEWS_INSERTED, "sadi-cleaner")


@pytest.mark.asyncio
async def test_create_group_reraises_other_redis_errors():
    client, r = make_client()
    r.xgroup_create = AsyncMock(
        side_effect=redis.asyncio.ResponseError("ERR some other error")
    )
    with pytest.raises(redis.asyncio.ResponseError):
        await client.create_group_if_not_exists(STREAM_RAW_NEWS_INSERTED, "sadi-cleaner")


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_calls_aclose():
    client, r = make_client()
    r.aclose = AsyncMock()
    await client.close()
    r.aclose.assert_called_once()


# ---------------------------------------------------------------------------
# create_stream_client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_stream_client_pings_and_returns_client():
    fake_redis = AsyncMock(spec=redis.asyncio.Redis)
    fake_redis.ping = AsyncMock()

    with patch("redis.asyncio.Redis.from_url", return_value=fake_redis):
        client = await create_stream_client("redis://localhost:6379")

    assert isinstance(client, StreamClient)
    assert client._client is fake_redis
    fake_redis.ping.assert_called_once()
