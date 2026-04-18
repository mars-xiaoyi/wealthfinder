from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.cleaner.stream_handler import StreamHandler, StreamMessage
from app.redis.stream_client import (
    STREAM_RAW_NEWS_CLEANED,
    STREAM_RAW_NEWS_INSERTED,
    StreamClient,
)


@pytest.fixture
def mock_stream_client():
    client = MagicMock(spec=StreamClient)
    client.create_group_if_not_exists = AsyncMock()
    client.read_group = AsyncMock(return_value=[])
    client.autoclaim = AsyncMock(return_value=[])
    client.ack = AsyncMock()
    client.publish = AsyncMock(return_value="1-0")
    return client


@pytest.fixture
def handler(mock_stream_client):
    return StreamHandler(mock_stream_client)


class TestStreamHandlerConstants:
    def test_stream_names(self):
        assert StreamHandler.STREAM_IN == STREAM_RAW_NEWS_INSERTED
        assert StreamHandler.STREAM_OUT == STREAM_RAW_NEWS_CLEANED

    def test_group_and_consumer(self):
        assert StreamHandler.GROUP_NAME == "sadi-cleaner"
        assert StreamHandler.CONSUMER_NAME == "sadi-cleaner-1"


class TestEnsureConsumerGroup:
    @pytest.mark.asyncio
    async def test_delegates_to_stream_client(self, handler, mock_stream_client):
        await handler.ensure_consumer_group()
        mock_stream_client.create_group_if_not_exists.assert_called_once_with(
            STREAM_RAW_NEWS_INSERTED, "sadi-cleaner"
        )


class TestReadMessages:
    @pytest.mark.asyncio
    async def test_delegates_with_defaults(self, handler, mock_stream_client):
        await handler.read_messages()
        mock_stream_client.read_group.assert_called_once_with(
            stream=STREAM_RAW_NEWS_INSERTED,
            group="sadi-cleaner",
            consumer="sadi-cleaner-1",
            count=10,
            block_ms=5000,
        )

    @pytest.mark.asyncio
    async def test_custom_count(self, handler, mock_stream_client):
        await handler.read_messages(count=5)
        mock_stream_client.read_group.assert_called_once_with(
            stream=STREAM_RAW_NEWS_INSERTED,
            group="sadi-cleaner",
            consumer="sadi-cleaner-1",
            count=5,
            block_ms=5000,
        )

    @pytest.mark.asyncio
    async def test_returns_stream_messages(self, handler, mock_stream_client):
        mock_stream_client.read_group.return_value = [
            {"id": "1-0", "fields": {"raw_id": "abc"}}
        ]
        result = await handler.read_messages()
        assert len(result) == 1
        assert isinstance(result[0], StreamMessage)
        assert result[0].message_id == "1-0"
        assert result[0].raw_id == "abc"


class TestReclaimPending:
    @pytest.mark.asyncio
    async def test_delegates_with_idle_ms(self, handler, mock_stream_client):
        await handler.reclaim_pending(min_idle_ms=30000)
        mock_stream_client.autoclaim.assert_called_once_with(
            stream=STREAM_RAW_NEWS_INSERTED,
            group="sadi-cleaner",
            consumer="sadi-cleaner-1",
            min_idle_ms=30000,
            count=10,
        )

    @pytest.mark.asyncio
    async def test_returns_stream_messages(self, handler, mock_stream_client):
        mock_stream_client.autoclaim.return_value = [
            {"id": "2-0", "fields": {"raw_id": "xyz"}}
        ]
        result = await handler.reclaim_pending(min_idle_ms=5000)
        assert len(result) == 1
        assert isinstance(result[0], StreamMessage)
        assert result[0].message_id == "2-0"
        assert result[0].raw_id == "xyz"


class TestAck:
    @pytest.mark.asyncio
    async def test_delegates_to_stream_client(self, handler, mock_stream_client):
        await handler.ack("1-0")
        mock_stream_client.ack.assert_called_once_with(
            STREAM_RAW_NEWS_INSERTED, "sadi-cleaner", "1-0"
        )


class TestPublishCleaned:
    @pytest.mark.asyncio
    async def test_publishes_cleaned_id_as_string(self, handler, mock_stream_client):
        cid = uuid4()
        await handler.publish_cleaned(cid)
        mock_stream_client.publish.assert_called_once_with(
            STREAM_RAW_NEWS_CLEANED, {"cleaned_id": str(cid)}
        )
