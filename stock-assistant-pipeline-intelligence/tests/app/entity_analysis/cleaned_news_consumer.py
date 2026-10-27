import asyncio
from unittest import mock
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import redis.exceptions

from app.common import hk_stock_list as hk_stock_list_module
from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.common.hk_stock_list import HKStockListCache
from app.common.stock import Stock
from app.config import EntityAnalysisConfig, RedisConfig
from app.entity_analysis import entity_analysis_service as entity_analysis_service_module
from app.entity_analysis.cleaned_news_consumer import CleanedNewsConsumer
from app.entity_analysis.entity_analysis_service import EntityAnalysisService
from app.llm.errors import LLMError
from app.models.entity_event import AnalysisOutcome, EntityEvent, EventTypeEnum, SentimentEnum
from app.models.versioned_metadata import VersionedMetadata
from app.redis import stream_client as stream_client_module
from app.redis.stream_client import (
    CONSUMER_GROUP_ENTITY_ANALYSIS,
    STREAM_RAW_NEWS_CLEANED,
    RedisStreamClient,
    StreamMessage,
)

CONFIG = EntityAnalysisConfig(
    batch_size=5,
    batch_timeout_ms=200,
    max_input_chars=30000,
    sadi_api_url="http://sadi:8000",
    consumer_name="pod-1",
)
REDIS_CONFIG = RedisConfig(
    stream_url="redis://localhost/0", state_url="redis://localhost/1", reconnect_interval_s=3
)
ID_A = UUID("00000000-0000-0000-0000-00000000000a")
ID_B = UUID("00000000-0000-0000-0000-00000000000b")
MESSAGE_A = StreamMessage(id="1-0", fields={"cleaned_id": str(ID_A), "v": "1"})
MESSAGE_B = StreamMessage(id="2-0", fields={"cleaned_id": str(ID_B), "v": "1"})
TENCENT = Stock(
    stock_code="00700", exchange="HKEX", company_name_zh="騰訊控股", company_name_en="TENCENT"
)
ENTITY_EVENT_A = EntityEvent(
    source_id=ID_A,
    source_url="https://example.com/a",
    source_name="MINGPAO",
    published_at=None,
    analysis_outcome=VersionedMetadata(
        "v1.0.0",
        AnalysisOutcome(
            stock_code="00700",
            event_type_primary=EventTypeEnum.EARNINGS,
            event_type_secondary=None,
            sentiment_label=SentimentEnum.POSITIVE,
            sentiment_score=0.8,
            headline="業績超預期",
            entity_summary="季度收入按年增長。",
        ),
    ),
    stock=TENCENT,
)


LOADED_CACHE = HKStockListCache({"00700": TENCENT})


@pytest.fixture(autouse=True)
def hk_stock_list_cache(monkeypatch):
    """A loaded HK Stock List, so every test but the gate's processes its messages."""
    monkeypatch.setattr(hk_stock_list_module, "_hk_stock_list_cache", LOADED_CACHE)


@pytest.fixture
def redis_stream(monkeypatch):
    client = AsyncMock(spec=RedisStreamClient)
    monkeypatch.setattr(stream_client_module, "_redis_stream_client", client)
    return client


@pytest.fixture
def service(monkeypatch):
    """An EntityAnalysisService that returns normally unless a test says otherwise."""
    entity_analysis_service = AsyncMock(spec=EntityAnalysisService)
    entity_analysis_service.find_entity_events.return_value = []
    monkeypatch.setattr(
        entity_analysis_service_module, "_entity_analysis_service", entity_analysis_service
    )
    return entity_analysis_service


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


async def start_acks_analysed(redis_stream, service):
    """An analysed article is a final outcome, whether it yields entity events or none (no
    listed company, or not found in SADI): ACKed."""
    service.find_entity_events.side_effect = [[ENTITY_EVENT_A], []]

    await _run(redis_stream, [MESSAGE_A, MESSAGE_B], [])

    redis_stream.create_group_if_not_exists.assert_awaited_once_with(
        STREAM_RAW_NEWS_CLEANED, CONSUMER_GROUP_ENTITY_ANALYSIS
    )
    assert redis_stream.read_from.await_args_list == [_read_call()] * 3
    assert service.find_entity_events.await_args_list == [mock.call(ID_A), mock.call(ID_B)]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_B)]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(LLMError("analysis failed", category=ErrorCategory.LLM), id="llm_failure"),
        pytest.param(
            ServiceUnavailableError.upstream_unavailable(
                "SADI down", category=ErrorCategory.NEWS_CLIENT
            ),
            id="sadi_failure",
        ),
        pytest.param(RuntimeError("bug"), id="unexpected"),
    ],
)
async def start_failed_analysis_left_unacked(redis_stream, service, error):
    """A failed analysis leaves its message un-ACKed, and the rest of the batch and the loop
    carry on."""
    service.find_entity_events.side_effect = [error, []]

    await _run(redis_stream, [MESSAGE_A, MESSAGE_B], [])

    assert service.find_entity_events.await_args_list == [mock.call(ID_A), mock.call(ID_B)]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B)]
    assert redis_stream.read_from.await_count == 3


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"v": "1"}, id="missing_cleaned_id"),
        pytest.param({"cleaned_id": "not-a-uuid", "v": "1"}, id="invalid_cleaned_id"),
    ],
)
async def start_invalid_message_left_unacked(redis_stream, service, fields):
    await _run(redis_stream, [StreamMessage(id="1-0", fields=fields), MESSAGE_B])

    assert service.find_entity_events.await_args_list == [mock.call(ID_B)]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B)]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(redis.exceptions.ConnectionError("down"), id="connection_error"),
        pytest.param(redis.exceptions.TimeoutError("hung"), id="timeout"),
    ],
)
async def start_redis_unreachable_on_read(redis_stream, service, error):
    """An unreachable stream Redis is retried after the reconnect interval, not raised."""
    with mock.patch("asyncio.sleep", new=AsyncMock()) as sleep:
        await _run(redis_stream, error, error, [MESSAGE_A])

    assert sleep.await_args_list == [mock.call(REDIS_CONFIG.reconnect_interval_s)] * 2
    assert redis_stream.read_from.await_count == 4
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]


async def start_redis_unreachable_on_ack(redis_stream, service):
    """A failed ACK goes through the same retry, leaving the rest of its batch pending."""
    redis_stream.ack.side_effect = redis.exceptions.ConnectionError("down")

    with mock.patch("asyncio.sleep", new=AsyncMock()) as sleep:
        await _run(redis_stream, [MESSAGE_A, MESSAGE_B])

    assert sleep.await_args_list == [mock.call(REDIS_CONFIG.reconnect_interval_s)]
    assert service.find_entity_events.await_args_list == [mock.call(ID_A)]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]
    assert redis_stream.read_from.await_count == 2


async def start_hk_stock_list_not_ready(monkeypatch, redis_stream, service):
    """With the HK Stock List empty, every message is left un-ACKed without analysing its
    article, and the loop carries on."""
    monkeypatch.setattr(hk_stock_list_module, "_hk_stock_list_cache", HKStockListCache({}))

    await _run(redis_stream, [MESSAGE_A, MESSAGE_B], [])

    service.find_entity_events.assert_not_awaited()
    redis_stream.ack.assert_not_awaited()
    assert redis_stream.read_from.await_args_list == [_read_call()] * 3
