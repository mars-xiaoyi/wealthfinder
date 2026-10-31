import asyncio
import json
from datetime import UTC, datetime
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
from app.redis.stream_client import ReclaimedMessage, RedisStreamClient, StreamMessage

CONFIG = EntityAnalysisConfig(
    batch_size=5,
    batch_timeout_ms=200,
    max_retry=3,
    max_input_chars=30000,
    sadi_api_url="http://sadi:8000",
    consumer_name="pod-1",
)
REDIS_CONFIG = RedisConfig(
    stream_url="redis://localhost/0",
    state_url="redis://localhost/1",
    reconnect_interval_s=3,
    stream_claim_timeout_ms=1500,
)
CLAIM_SLEEP_S = REDIS_CONFIG.stream_claim_timeout_ms / 1000
ID_A = UUID("00000000-0000-0000-0000-00000000000a")
ID_B = UUID("00000000-0000-0000-0000-00000000000b")
MESSAGE_A = StreamMessage(id="1-0", fields={"cleaned_id": str(ID_A), "v": "1"})
MESSAGE_B = StreamMessage(id="2-0", fields={"cleaned_id": str(ID_B), "v": "1"})
RECLAIMED_A = ReclaimedMessage(id=MESSAGE_A.id, fields=MESSAGE_A.fields, delivery_count=2)
RECLAIMED_B = ReclaimedMessage(id=MESSAGE_B.id, fields=MESSAGE_B.fields, delivery_count=2)
MALFORMED_FIELDS = [
    pytest.param({"v": "1"}, id="missing_cleaned_id"),
    pytest.param({"cleaned_id": "not-a-uuid", "v": "1"}, id="invalid_cleaned_id"),
    pytest.param({"cleaned_id": str(ID_A)}, id="missing_v"),
    pytest.param({"cleaned_id": str(ID_A), "v": "2"}, id="unrecognised_v"),
]
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


async def _run(
    redis_stream: AsyncMock, batches: list[object], claims: list[object] | None = None
) -> AsyncMock:
    """Run start() over the given read_from() and autoclaim() results (a list of messages, or
    an exception to raise), each loop stopping by cancellation on its next call. Returns the
    patched asyncio.sleep, so its awaits can be checked."""
    redis_stream.read_from.side_effect = [*batches, asyncio.CancelledError()]
    redis_stream.autoclaim.side_effect = [*(claims or []), asyncio.CancelledError()]
    with mock.patch("asyncio.sleep", new=AsyncMock()) as sleep:
        await CleanedNewsConsumer(CONFIG, REDIS_CONFIG).start()
    return sleep


def _read_call() -> object:
    return mock.call(
        "stream:raw_news_cleaned",
        "sapi-entity-analysis",
        CONFIG.consumer_name,
        count=CONFIG.batch_size,
        block_ms=CONFIG.batch_timeout_ms,
    )


def _claim_call() -> object:
    return mock.call(
        "stream:raw_news_cleaned",
        "sapi-entity-analysis",
        CONFIG.consumer_name,
        min_idle_ms=REDIS_CONFIG.stream_claim_timeout_ms,
        count=CONFIG.batch_size,
    )


def _ack_call(message: StreamMessage) -> object:
    return mock.call("stream:raw_news_cleaned", "sapi-entity-analysis", message.id)


def _dead_letter_call(message: StreamMessage, delivery_count: int) -> object:
    return mock.call(
        "stream:entity_analysis_dead_letter",
        {
            "message_id": message.id,
            "fields": json.dumps(message.fields),
            "delivery_count": str(delivery_count),
            "failed_at": mock.ANY,
            "v": "1",
        },
    )


def _assert_failed_at_between(redis_stream: AsyncMock, start: datetime) -> None:
    """Every dead-letter entry's failed_at is a UTC ISO-8601 time from this test's run."""
    for call in redis_stream.publish.await_args_list:
        failed_at = datetime.fromisoformat(call.args[1]["failed_at"])
        assert failed_at.tzinfo == UTC
        assert start <= failed_at <= datetime.now(UTC)


async def start_acks_analysed(redis_stream, service):
    """An analysed article is a final outcome, whether it yields entity events or none (no
    listed company, or not found in SADI): ACKed."""
    service.find_entity_events.side_effect = [[ENTITY_EVENT_A], []]

    sleep = await _run(redis_stream, [[MESSAGE_A, MESSAGE_B], []])

    redis_stream.create_group_if_not_exists.assert_awaited_once_with(
        "stream:raw_news_cleaned", "sapi-entity-analysis"
    )
    assert redis_stream.read_from.await_args_list == [_read_call()] * 3
    assert redis_stream.autoclaim.await_args_list == [_claim_call()]
    assert sleep.await_args_list == [mock.call(CLAIM_SLEEP_S)]
    assert service.find_entity_events.await_args_list == [mock.call(ID_A), mock.call(ID_B)]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_B)]
    redis_stream.publish.assert_not_awaited()


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
async def start_failed_analysis_reclaimed(redis_stream, service, error):
    """A failed analysis leaves its message un-ACKed while the rest of the batch carries on;
    once reclaimed, the message is analysed again and ACKed."""
    service.find_entity_events.side_effect = [error, [], []]

    await _run(redis_stream, [[MESSAGE_A, MESSAGE_B], []], [[RECLAIMED_A]])

    assert service.find_entity_events.await_args_list == [
        mock.call(ID_A),
        mock.call(ID_B),
        mock.call(ID_A),
    ]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B), _ack_call(MESSAGE_A)]
    assert redis_stream.read_from.await_count == 3
    assert redis_stream.autoclaim.await_count == 2
    redis_stream.publish.assert_not_awaited()


@pytest.mark.parametrize("fields", MALFORMED_FIELDS)
@pytest.mark.parametrize(
    "reclaimed",
    [pytest.param(False, id="read_path"), pytest.param(True, id="reclaim_path")],
)
async def start_malformed_message_dead_lettered(redis_stream, service, fields, reclaimed):
    """A message this layer can never process is dead-lettered and ACKed without being
    analysed, on either path; the rest of its batch is processed as normal."""
    start = datetime.now(UTC)
    if reclaimed:
        malformed: StreamMessage = ReclaimedMessage(id="9-0", fields=fields, delivery_count=2)
        await _run(redis_stream, [], [[malformed, RECLAIMED_B]])
    else:
        malformed = StreamMessage(id="9-0", fields=fields)
        await _run(redis_stream, [[malformed, MESSAGE_B]])

    assert redis_stream.publish.await_args_list == [
        _dead_letter_call(malformed, delivery_count=2 if reclaimed else 1)
    ]
    _assert_failed_at_between(redis_stream, start)
    assert service.find_entity_events.await_args_list == [mock.call(ID_B)]
    assert redis_stream.ack.await_args_list == [_ack_call(malformed), _ack_call(MESSAGE_B)]


async def start_reclaimed_retries_exhausted(redis_stream, service):
    """A reclaimed message delivered max_retry times is dead-lettered and ACKed without being
    analysed; one with deliveries left is analysed."""
    start = datetime.now(UTC)
    exhausted = ReclaimedMessage(
        id=MESSAGE_A.id, fields=MESSAGE_A.fields, delivery_count=CONFIG.max_retry
    )

    await _run(redis_stream, [], [[exhausted, RECLAIMED_B]])

    assert redis_stream.publish.await_args_list == [
        _dead_letter_call(exhausted, delivery_count=CONFIG.max_retry)
    ]
    _assert_failed_at_between(redis_stream, start)
    assert service.find_entity_events.await_args_list == [mock.call(ID_B)]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_B)]


async def start_in_flight_duplicate_skipped(redis_stream, service):
    """A message whose article is still being analysed is skipped and left un-ACKed; the
    analysis in flight ACKs once it finishes."""
    release = asyncio.Event()

    async def _analyse(cleaned_id: UUID) -> list[EntityEvent]:
        await release.wait()
        return []

    async def _claim(*args: object, **kwargs: object) -> list[ReclaimedMessage]:
        if redis_stream.autoclaim.await_count == 1:
            return [RECLAIMED_A]
        release.set()
        raise asyncio.CancelledError

    service.find_entity_events.side_effect = _analyse
    redis_stream.read_from.side_effect = [[MESSAGE_A], asyncio.CancelledError()]
    redis_stream.autoclaim.side_effect = _claim
    with mock.patch("asyncio.sleep", new=AsyncMock()):
        await CleanedNewsConsumer(CONFIG, REDIS_CONFIG).start()

    assert service.find_entity_events.await_args_list == [mock.call(ID_A)]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]
    assert redis_stream.autoclaim.await_count == 2


async def start_loop_failure_stops_layer(redis_stream, service):
    """An unexpected error in one loop stops the other one, and start() raises it."""
    error = redis.exceptions.ResponseError("WRONGTYPE")
    claiming = asyncio.Event()
    stopped: list[str] = []

    async def _claim(*args: object, **kwargs: object) -> list[ReclaimedMessage]:
        claiming.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append("reclaim_loop")
        return []

    async def _read(*args: object, **kwargs: object) -> list[StreamMessage]:
        await claiming.wait()
        raise error

    redis_stream.read_from.side_effect = _read
    redis_stream.autoclaim.side_effect = _claim
    with mock.patch("asyncio.sleep", new=AsyncMock()), pytest.raises(ExceptionGroup) as raised:
        await CleanedNewsConsumer(CONFIG, REDIS_CONFIG).start()

    assert raised.value.exceptions == (error,)
    assert stopped == ["reclaim_loop"]


async def start_cancelled(redis_stream, service):
    """Cancelling start() stops both loops before the cancellation reaches the caller."""
    reading = asyncio.Event()
    claiming = asyncio.Event()
    stopped: list[str] = []

    async def _block(started: asyncio.Event, name: str) -> list[StreamMessage]:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(name)
        return []

    async def _read(*args: object, **kwargs: object) -> list[StreamMessage]:
        return await _block(reading, "consumer_loop")

    async def _claim(*args: object, **kwargs: object) -> list[StreamMessage]:
        return await _block(claiming, "reclaim_loop")

    redis_stream.read_from.side_effect = _read
    redis_stream.autoclaim.side_effect = _claim
    with mock.patch("asyncio.sleep", new=AsyncMock()):
        task = asyncio.create_task(CleanedNewsConsumer(CONFIG, REDIS_CONFIG).start())
        await reading.wait()
        await claiming.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert sorted(stopped) == ["consumer_loop", "reclaim_loop"]


async def start_dead_letter_failure_isolated(redis_stream, service):
    """A message whose dead-lettering fails is left un-ACKed, and the rest of its batch is
    processed as normal."""
    redis_stream.publish.side_effect = redis.exceptions.ResponseError("OOM")
    malformed = StreamMessage(id="9-0", fields={"v": "1"})

    await _run(redis_stream, [[malformed, MESSAGE_B], []])

    assert redis_stream.publish.await_args_list == [_dead_letter_call(malformed, 1)]
    assert service.find_entity_events.await_args_list == [mock.call(ID_B)]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B)]
    assert redis_stream.read_from.await_count == 3


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(redis.exceptions.ResponseError("NOPERM"), id="sweep_failed"),
        pytest.param(redis.exceptions.ConnectionError("down"), id="connection_error"),
        pytest.param(redis.exceptions.TimeoutError("hung"), id="timeout"),
    ],
)
async def start_reclaim_sweep_failed(redis_stream, service, error):
    """A failed sweep is retried on the next claim timeout, not raised."""
    sleep = await _run(redis_stream, [], [error, [RECLAIMED_A]])

    assert sleep.await_args_list == [mock.call(CLAIM_SLEEP_S)] * 3
    assert redis_stream.autoclaim.await_args_list == [_claim_call()] * 3
    assert service.find_entity_events.await_args_list == [mock.call(ID_A)]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(redis.exceptions.ConnectionError("down"), id="connection_error"),
        pytest.param(redis.exceptions.TimeoutError("hung"), id="timeout"),
    ],
)
async def start_redis_unreachable_on_read(redis_stream, service, error):
    """An unreachable stream Redis is retried after the reconnect interval, not raised."""
    sleep = await _run(redis_stream, [error, error, [MESSAGE_A]])

    assert sleep.await_args_list == [mock.call(REDIS_CONFIG.reconnect_interval_s)] * 2 + [
        mock.call(CLAIM_SLEEP_S)
    ]
    assert redis_stream.read_from.await_count == 4
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]


async def start_redis_unreachable_on_ack(redis_stream, service):
    """A failed ACK goes through the same retry, leaving the rest of its batch pending."""
    redis_stream.ack.side_effect = redis.exceptions.ConnectionError("down")

    sleep = await _run(redis_stream, [[MESSAGE_A, MESSAGE_B]])

    assert sleep.await_args_list == [
        mock.call(REDIS_CONFIG.reconnect_interval_s),
        mock.call(CLAIM_SLEEP_S),
    ]
    assert service.find_entity_events.await_args_list == [mock.call(ID_A)]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]
    assert redis_stream.read_from.await_count == 2


async def start_hk_stock_list_not_ready(monkeypatch, redis_stream, service):
    """With the HK Stock List empty, every message is left un-ACKed without analysing its
    article, and the loop carries on."""
    monkeypatch.setattr(hk_stock_list_module, "_hk_stock_list_cache", HKStockListCache({}))

    await _run(redis_stream, [[MESSAGE_A, MESSAGE_B], []])

    service.find_entity_events.assert_not_awaited()
    redis_stream.ack.assert_not_awaited()
    assert redis_stream.read_from.await_args_list == [_read_call()] * 3
