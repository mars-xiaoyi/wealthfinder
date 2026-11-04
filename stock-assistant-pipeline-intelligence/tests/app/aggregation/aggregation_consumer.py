import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
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
from app.config import AggregationConfig, DatabaseConfig, RedisConfig
from app.db.errors import DatabaseError
from app.redis import stream_client as stream_client_module
from app.redis.stream_client import ReclaimedMessage, RedisStreamClient, StreamMessage

# A concurrency bug shows up as a hang, so fail fast instead.
pytestmark = [pytest.mark.timeout(5)]

CONFIG = AggregationConfig(
    batch_size=5,
    batch_timeout_ms=200,
    max_concurrent=2,
    group_idle_timeout_s=60,
    db_flush_interval_s=300,
    max_retry=3,
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
CLAIM_SLEEP_S = REDIS_CONFIG.stream_claim_timeout_ms / 1000
DB_CONFIG = DatabaseConfig(
    url="postgresql://user:pass@localhost/sapi",
    pool_size=10,
    close_timeout_s=1,
    max_retry=3,
    retry_base_wait_ms=100,
)
ID_A = UUID("00000000-0000-0000-0000-00000000000a")
ID_B = UUID("00000000-0000-0000-0000-00000000000b")
MESSAGE_A = StreamMessage(id="1-0", fields={"entity_event_id": str(ID_A), "v": "1"})
MESSAGE_B = StreamMessage(id="2-0", fields={"entity_event_id": str(ID_B), "v": "1"})
# Later deliveries of A's EntityEvent, as separate stream entries.
MESSAGE_A2 = StreamMessage(id="3-0", fields={"entity_event_id": str(ID_A), "v": "1"})
MESSAGE_A3 = StreamMessage(id="4-0", fields={"entity_event_id": str(ID_A), "v": "1"})
RECLAIMED_A = ReclaimedMessage(id=MESSAGE_A.id, fields=MESSAGE_A.fields, delivery_count=2)
RECLAIMED_B = ReclaimedMessage(id=MESSAGE_B.id, fields=MESSAGE_B.fields, delivery_count=2)
MALFORMED_FIELDS = [
    pytest.param({"v": "1"}, id="missing_entity_event_id"),
    pytest.param({"entity_event_id": "not-a-uuid", "v": "1"}, id="invalid_entity_event_id"),
    pytest.param({"entity_event_id": str(ID_A)}, id="missing_v"),
    pytest.param({"entity_event_id": str(ID_A), "v": "2"}, id="unrecognised_v"),
]


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


def _side_effect(results: list[object]) -> Callable[..., Awaitable[object]]:
    """Return each result in turn (a list of messages, an exception to raise, or an async
    callable returning either), then a cancellation."""
    remaining = iter([*results, asyncio.CancelledError()])

    async def next_result(*_args: object, **_kwargs: object) -> object:
        result = next(remaining)
        if callable(result):
            result = await result()
        if isinstance(result, BaseException):
            raise result
        return result

    return next_result


async def _run(
    redis_stream: AsyncMock,
    batches: list[object],
    claims: list[object] | None = None,
    config: AggregationConfig = CONFIG,
) -> AsyncMock:
    """Run start() over the given read_from() and autoclaim() results (see _side_effect()),
    each loop stopping by cancellation on its next call. Returns the patched asyncio.sleep, so
    its awaits can be checked."""
    redis_stream.read_from.side_effect = _side_effect(batches)
    redis_stream.autoclaim.side_effect = _side_effect(claims or [])
    with mock.patch("asyncio.sleep", new=AsyncMock()) as sleep:
        await AggregationConsumer(config, REDIS_CONFIG, DB_CONFIG).start()
    return sleep


def _read_call() -> object:
    return mock.call(
        "stream:entity_event_completed",
        "sapi-aggregation",
        CONFIG.consumer_name,
        count=CONFIG.batch_size,
        block_ms=CONFIG.batch_timeout_ms,
    )


def _claim_call() -> object:
    return mock.call(
        "stream:entity_event_completed",
        "sapi-aggregation",
        CONFIG.consumer_name,
        min_idle_ms=REDIS_CONFIG.stream_claim_timeout_ms,
        count=CONFIG.batch_size,
    )


def _dead_letter_call(message: StreamMessage, reason: str) -> object:
    return mock.call(
        message,
        "stream:entity_event_completed",
        "sapi-aggregation",
        "stream:aggregation_dead_letter",
        reason,
    )


def _ack_call(message: StreamMessage) -> object:
    return mock.call("stream:entity_event_completed", "sapi-aggregation", message.id)


def _processed_ids(service: AsyncMock) -> list[UUID]:
    return [call.args[0] for call in service.process_entity_event.await_args_list]


async def start_acks_processed(redis_stream, service):
    """Each message's EntityEvent is processed in stream order, and the message ACKed once
    its outcome is final."""
    sleep = await _run(redis_stream, [[MESSAGE_A, MESSAGE_B], []], [[]])

    redis_stream.create_group_if_not_exists.assert_awaited_once_with(
        "stream:entity_event_completed", "sapi-aggregation"
    )
    assert redis_stream.read_from.await_args_list == [_read_call()] * 3
    assert redis_stream.autoclaim.await_args_list == [_claim_call()] * 2
    assert sleep.await_args_list == [mock.call(CLAIM_SLEEP_S)] * 2
    assert _processed_ids(service) == [ID_A, ID_B]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_B)]
    redis_stream.dead_letter.assert_not_awaited()


@pytest.mark.parametrize("fields", MALFORMED_FIELDS)
@pytest.mark.parametrize(
    "reclaimed",
    [pytest.param(False, id="read_path"), pytest.param(True, id="reclaim_path")],
)
async def start_malformed_message_dead_lettered(redis_stream, service, fields, reclaimed):
    """A message this layer can never process is dead-lettered without being processed, on
    either path; the rest of its batch is processed as normal."""
    if reclaimed:
        malformed: StreamMessage = ReclaimedMessage(id="9-0", fields=fields, delivery_count=2)
        await _run(redis_stream, [], [[malformed, RECLAIMED_B]])
    else:
        malformed = StreamMessage(id="9-0", fields=fields)
        await _run(redis_stream, [[malformed, MESSAGE_B]])

    assert redis_stream.dead_letter.await_args_list == [_dead_letter_call(malformed, "malformed")]
    assert _processed_ids(service) == [ID_B]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B)]


async def start_reclaimed_retries_exhausted(redis_stream, service):
    """A reclaimed message delivered max_retry times is dead-lettered without being processed;
    one with deliveries left is processed and ACKed."""
    exhausted = ReclaimedMessage(
        id=MESSAGE_A.id, fields=MESSAGE_A.fields, delivery_count=CONFIG.max_retry
    )

    await _run(redis_stream, [], [[exhausted, RECLAIMED_B]])

    assert redis_stream.dead_letter.await_args_list == [
        _dead_letter_call(exhausted, "retries_exhausted")
    ]
    assert _processed_ids(service) == [ID_B]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B)]


async def start_dead_letter_failure_isolated(redis_stream, service):
    """A message whose dead-lettering fails is left un-ACKed, and the rest of its batch is
    processed as normal."""
    redis_stream.dead_letter.side_effect = redis.exceptions.ResponseError("OOM")
    malformed = StreamMessage(id="9-0", fields={"v": "1"})

    await _run(redis_stream, [[malformed, MESSAGE_B], []])

    assert redis_stream.dead_letter.await_args_list == [_dead_letter_call(malformed, "malformed")]
    assert _processed_ids(service) == [ID_B]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B)]
    assert redis_stream.read_from.await_count == 3


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
    assert sleep.await_args_list == [mock.call(CLAIM_SLEEP_S)]


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

    # The reclaim loop's sleep interleaves with the consumer loop's, so compare without order.
    assert sorted(call.args[0] for call in sleep.await_args_list) == [
        CLAIM_SLEEP_S,
        REDIS_CONFIG.reconnect_interval_s,
        REDIS_CONFIG.reconnect_interval_s,
    ]
    assert redis_stream.read_from.await_count == 4
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]


async def start_redis_unreachable_on_ack(redis_stream, service):
    """An ACK that fails before the EntityEvent is queued goes through the same retry, leaving
    the rest of its batch pending, and a redelivery is processed again."""
    redis_stream.ack.side_effect = [redis.exceptions.ConnectionError("down"), None]

    sleep = await _run(redis_stream, [[MESSAGE_A, MESSAGE_B], [MESSAGE_A2]])

    assert sorted(call.args[0] for call in sleep.await_args_list) == [
        CLAIM_SLEEP_S,
        REDIS_CONFIG.reconnect_interval_s,
    ]
    assert _processed_ids(service) == [ID_A, ID_A]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_A2)]


async def start_flushes_when_drained(redis_stream, service):
    """The active Events are flushed each time a read finds the stream drained, and once more
    when consuming stops."""
    counts: list[int] = []

    async def drained() -> list[StreamMessage]:
        counts.append(service.flush.await_count)
        return []

    async def after_drained() -> list[StreamMessage]:
        counts.append(service.flush.await_count)
        return [MESSAGE_B]

    await _run(redis_stream, [[MESSAGE_A], drained, after_drained])

    assert counts == [0, 1]
    assert service.flush.await_count == 2
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_B)]


async def start_flushes_on_interval(redis_stream, service):
    """Under steady input the active Events are still flushed once the flush interval has
    passed."""
    config = dataclasses.replace(CONFIG, db_flush_interval_s=0)

    await _run(redis_stream, [[MESSAGE_A], [MESSAGE_B]], config=config)

    # One flush after each batch, plus the one when consuming stops.
    assert service.flush.await_count == 3


async def start_flushes_when_stopped(redis_stream, service):
    """Stopping the consumer flushes the active Events once more."""
    await _run(redis_stream, [[MESSAGE_A]])

    service.flush.assert_awaited_once_with()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            ServiceUnavailableError("DB connection error", category=ErrorCategory.DB),
            id="database_unreachable",
        ),
        pytest.param(DatabaseError("DB error", category=ErrorCategory.DB), id="database"),
        pytest.param(redis.exceptions.ConnectionError("down"), id="state_redis_unreachable"),
    ],
)
async def start_flush_failed(redis_stream, service, error):
    """A failed flush doesn't stop consuming; the next flush is tried as usual."""
    service.flush.side_effect = [error, None, None]

    sleep = await _run(redis_stream, [[], [MESSAGE_A], []])

    assert service.flush.await_count == 3
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]
    assert sleep.await_args_list == [mock.call(CLAIM_SLEEP_S)]


async def start_final_flush_failed(redis_stream, service):
    """A failure of the flush when consuming stops is not raised in place of the stop."""
    service.flush.side_effect = DatabaseError("DB error", category=ErrorCategory.DB)

    await _run(redis_stream, [[MESSAGE_A]])

    service.flush.assert_awaited_once_with()


async def start_final_flush_timed_out(redis_stream, service):
    """A flush that hangs when consuming stops is abandoned after the final flush timeout, so
    stopping completes."""
    abandoned = asyncio.Event()

    async def hang() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            abandoned.set()
            raise

    service.flush.side_effect = hang

    await _run(redis_stream, [[MESSAGE_A]])

    assert abandoned.is_set()
    service.flush.assert_awaited_once_with()


async def start_reclaimed_in_flight_skipped(redis_stream, service):
    """A reclaimed message whose EntityEvent is still in flight from its first delivery is
    skipped; the first delivery ACKs it once its outcome is final."""
    queued = asyncio.Event()
    pending: list[Callable[[], Awaitable[None]]] = []

    async def hold(entity_event_id, on_success, on_failure) -> None:
        pending.append(on_success)
        queued.set()

    service.process_entity_event.side_effect = hold

    async def claim_once_queued() -> list[StreamMessage]:
        await queued.wait()
        return [RECLAIMED_A]

    async def finish_first() -> list[StreamMessage]:
        await pending[0]()
        return []

    await _run(redis_stream, [[MESSAGE_A]], [claim_once_queued, finish_first])

    assert _processed_ids(service) == [ID_A]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]
    redis_stream.dead_letter.assert_not_awaited()


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
    assert _processed_ids(service) == [ID_A]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]


async def start_loop_failure_stops_layer(redis_stream, service):
    """An unexpected error in one loop stops the other one, and start() raises it once the
    active Events are flushed once more."""
    error = redis.exceptions.ResponseError("WRONGTYPE")
    claiming = asyncio.Event()
    stopped: list[str] = []

    async def claim(*_args: object, **_kwargs: object) -> list[ReclaimedMessage]:
        claiming.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append("reclaim_loop")
        return []

    async def read(*_args: object, **_kwargs: object) -> list[StreamMessage]:
        await claiming.wait()
        raise error

    redis_stream.read_from.side_effect = read
    redis_stream.autoclaim.side_effect = claim
    with mock.patch("asyncio.sleep", new=AsyncMock()), pytest.raises(ExceptionGroup) as raised:
        await AggregationConsumer(CONFIG, REDIS_CONFIG, DB_CONFIG).start()

    assert raised.value.exceptions == (error,)
    assert stopped == ["reclaim_loop"]
    service.flush.assert_awaited_once_with()


async def start_restart_reprocesses_cancelled_entity_event(redis_stream, service):
    """An EntityEvent whose routing was cancelled by a loop failure is processed again by a
    restart of the same consumer."""
    processing = asyncio.Event()

    async def block_first(entity_event_id, on_success, on_failure) -> None:
        if service.process_entity_event.await_count == 1:
            processing.set()
            await asyncio.Event().wait()
        await on_success()

    error = redis.exceptions.ResponseError("NOGROUP")

    async def fail_once_processing(*_args: object, **_kwargs: object) -> list[StreamMessage]:
        await processing.wait()
        raise error

    service.process_entity_event.side_effect = block_first
    consumer = AggregationConsumer(CONFIG, REDIS_CONFIG, DB_CONFIG)
    with mock.patch("asyncio.sleep", new=AsyncMock()):
        redis_stream.read_from.side_effect = fail_once_processing
        redis_stream.autoclaim.side_effect = _side_effect([[RECLAIMED_A]])
        with pytest.raises(ExceptionGroup) as raised:
            await consumer.start()

        redis_stream.read_from.side_effect = _side_effect([[MESSAGE_A2]])
        redis_stream.autoclaim.side_effect = _side_effect([])
        await consumer.start()

    assert raised.value.exceptions == (error,)
    assert _processed_ids(service) == [ID_A, ID_A]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A2)]
    redis_stream.dead_letter.assert_not_awaited()


async def start_cancelled(redis_stream, service):
    """Cancelling start() stops both loops and flushes the active Events once more before the
    cancellation reaches the caller."""
    reading = asyncio.Event()
    claiming = asyncio.Event()
    stopped: list[str] = []

    async def block(started: asyncio.Event, name: str) -> list[StreamMessage]:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(name)
        return []

    async def read(*_args: object, **_kwargs: object) -> list[StreamMessage]:
        return await block(reading, "consumer_loop")

    async def claim(*_args: object, **_kwargs: object) -> list[StreamMessage]:
        return await block(claiming, "reclaim_loop")

    redis_stream.read_from.side_effect = read
    redis_stream.autoclaim.side_effect = claim
    with mock.patch("asyncio.sleep", new=AsyncMock()):
        task = asyncio.create_task(AggregationConsumer(CONFIG, REDIS_CONFIG, DB_CONFIG).start())
        await reading.wait()
        await claiming.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert sorted(stopped) == ["consumer_loop", "reclaim_loop"]
    service.flush.assert_awaited_once_with()
