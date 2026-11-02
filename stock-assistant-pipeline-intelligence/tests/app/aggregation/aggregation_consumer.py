import asyncio
from unittest import mock
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import redis.exceptions

from app.aggregation import aggregation_service as aggregation_service_module
from app.aggregation.aggregation_consumer import AggregationConsumer
from app.aggregation.aggregation_service import AggregationService
from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.config import AggregationConfig, RedisConfig
from app.db.errors import DatabaseError
from app.redis import stream_client as stream_client_module
from app.redis.stream_client import RedisStreamClient, StreamMessage

# A concurrency bug shows up as a hang, so fail fast instead.
pytestmark = [pytest.mark.timeout(5)]

CONFIG = AggregationConfig(
    batch_size=5,
    batch_timeout_ms=200,
    max_concurrent=2,
    group_idle_timeout_s=60,
    db_flush_interval_s=300,
    sliding_window_hours=4,
    event_max_timespan_hours=24,
    consumer_name="pod-1",
)
REDIS_CONFIG = RedisConfig(
    stream_url="redis://localhost/0",
    state_url="redis://localhost/1",
    reconnect_interval_s=3,
    stream_claim_timeout_ms=1500,
)
ID_A = UUID("00000000-0000-0000-0000-00000000000a")
ID_B = UUID("00000000-0000-0000-0000-00000000000b")
MESSAGE_A = StreamMessage(id="1-0", fields={"entity_event_id": str(ID_A), "v": "1"})
MESSAGE_B = StreamMessage(id="2-0", fields={"entity_event_id": str(ID_B), "v": "1"})
# Later deliveries of A's EntityEvent, as separate stream entries.
MESSAGE_A2 = StreamMessage(id="3-0", fields={"entity_event_id": str(ID_A), "v": "1"})
MESSAGE_A3 = StreamMessage(id="4-0", fields={"entity_event_id": str(ID_A), "v": "1"})


@pytest.fixture
def redis_stream(monkeypatch):
    client = AsyncMock(spec=RedisStreamClient)
    monkeypatch.setattr(stream_client_module, "_redis_stream_client", client)
    return client


@pytest.fixture
def service(monkeypatch):
    """An AggregationService whose every EntityEvent's outcome is final at once, unless a test
    says otherwise."""
    aggregation_service = AsyncMock(spec=AggregationService)

    async def process(entity_event_id, on_success, on_failure) -> None:
        await on_success()

    aggregation_service.process_entity_event.side_effect = process
    monkeypatch.setattr(aggregation_service_module, "_aggregation_service", aggregation_service)
    return aggregation_service


async def _run(redis_stream: AsyncMock, batches: list[object]) -> AsyncMock:
    """Run start() over the given read_from() results (a list of messages, an exception to
    raise, or an async callable returning either), stopping by cancellation on the next read.
    Returns the patched asyncio.sleep, so its awaits can be checked."""
    results = iter([*batches, asyncio.CancelledError()])

    async def read_from(*_args, **_kwargs):
        result = next(results)
        if callable(result):
            result = await result()
        if isinstance(result, BaseException):
            raise result
        return result

    redis_stream.read_from.side_effect = read_from
    with (
        mock.patch("asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(asyncio.CancelledError),
    ):
        await AggregationConsumer(CONFIG, REDIS_CONFIG).start()
    return sleep


def _read_call() -> object:
    return mock.call(
        "stream:entity_event_completed",
        "sapi-aggregation",
        CONFIG.consumer_name,
        count=CONFIG.batch_size,
        block_ms=CONFIG.batch_timeout_ms,
    )


def _ack_call(message: StreamMessage) -> object:
    return mock.call("stream:entity_event_completed", "sapi-aggregation", message.id)


def _processed_ids(service: AsyncMock) -> list[UUID]:
    return [call.args[0] for call in service.process_entity_event.await_args_list]


async def start_acks_processed(redis_stream, service):
    """Each message's EntityEvent is processed in stream order, and the message ACKed once
    its outcome is final."""
    await _run(redis_stream, [[MESSAGE_A, MESSAGE_B], []])

    redis_stream.create_group_if_not_exists.assert_awaited_once_with(
        "stream:entity_event_completed", "sapi-aggregation"
    )
    assert redis_stream.read_from.await_args_list == [_read_call()] * 3
    assert _processed_ids(service) == [ID_A, ID_B]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_B)]


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"v": "1"}, id="missing_entity_event_id"),
        pytest.param({"entity_event_id": "not-a-uuid", "v": "1"}, id="invalid_entity_event_id"),
    ],
)
async def start_invalid_message_left_unacked(redis_stream, service, fields):
    """A message without a valid entity_event_id is left un-ACKed, and the rest of its batch
    is processed as normal."""
    await _run(redis_stream, [[StreamMessage(id="9-0", fields=fields), MESSAGE_B]])

    assert _processed_ids(service) == [ID_B]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B)]


async def start_in_flight_duplicate_skipped(redis_stream, service):
    """A redelivery of an EntityEvent still in flight is skipped and left un-ACKed; once the
    first delivery's outcome is final, a later redelivery is processed again."""
    pending: list[object] = []

    async def hold_first(entity_event_id, on_success, on_failure) -> None:
        if not pending:
            pending.append(on_success)
            return
        await on_success()

    service.process_entity_event.side_effect = hold_first

    async def finish_first() -> list[StreamMessage]:
        await pending[0]()
        return [MESSAGE_A3]

    await _run(redis_stream, [[MESSAGE_A], [MESSAGE_A2], finish_first])

    assert _processed_ids(service) == [ID_A, ID_A]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_A3)]


async def start_failed_merge_redelivered(redis_stream, service):
    """A failed merge leaves its message un-ACKed, and a redelivery is processed again."""

    async def fail_first(entity_event_id, on_success, on_failure) -> None:
        if service.process_entity_event.await_count == 1:
            on_failure()
            return
        await on_success()

    service.process_entity_event.side_effect = fail_first

    await _run(redis_stream, [[MESSAGE_A], [MESSAGE_A2]])

    assert _processed_ids(service) == [ID_A, ID_A]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A2)]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            ServiceUnavailableError("DB connection error", category=ErrorCategory.DB),
            id="database_unreachable",
        ),
        pytest.param(DatabaseError("DB error", category=ErrorCategory.DB), id="database"),
        pytest.param(RuntimeError("bug"), id="unexpected"),
    ],
)
async def start_route_failed(redis_stream, service, error):
    """An EntityEvent that fails before it is queued leaves its message un-ACKed while the
    rest of the batch carries on, and a redelivery is processed again."""
    calls = 0

    async def fail_first(entity_event_id, on_success, on_failure) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        await on_success()

    service.process_entity_event.side_effect = fail_first

    sleep = await _run(redis_stream, [[MESSAGE_A, MESSAGE_B], [MESSAGE_A2]])

    assert _processed_ids(service) == [ID_A, ID_B, ID_A]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B), _ack_call(MESSAGE_A2)]
    sleep.assert_not_awaited()


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

    assert sleep.await_args_list == [mock.call(REDIS_CONFIG.reconnect_interval_s)] * 2
    assert redis_stream.read_from.await_count == 4
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]


async def start_redis_unreachable_on_ack(redis_stream, service):
    """An ACK that fails before the EntityEvent is queued goes through the same retry, leaving
    the rest of its batch pending, and a redelivery is processed again."""
    redis_stream.ack.side_effect = [redis.exceptions.ConnectionError("down"), None]

    sleep = await _run(redis_stream, [[MESSAGE_A, MESSAGE_B], [MESSAGE_A2]])

    assert sleep.await_args_list == [mock.call(REDIS_CONFIG.reconnect_interval_s)]
    assert _processed_ids(service) == [ID_A, ID_A]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_A2)]
