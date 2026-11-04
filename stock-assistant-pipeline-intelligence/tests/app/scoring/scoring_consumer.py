import asyncio
from collections.abc import Awaitable, Callable
from unittest import mock
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import redis.exceptions

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.config import RedisConfig, ScoringConfig
from app.db.errors import DatabaseError
from app.redis import stream_client as stream_client_module
from app.redis.stream_client import ReclaimedMessage, RedisStreamClient, StreamMessage
from app.scoring import scoring_service as scoring_service_module
from app.scoring.scoring_consumer import ScoringConsumer
from app.scoring.scoring_service import ScoringService

# A concurrency bug shows up as a hang, so fail fast instead.
pytestmark = [pytest.mark.timeout(5)]

CONFIG = ScoringConfig(
    batch_size=5,
    batch_timeout_ms=200,
    max_retry=3,
    consumer_name="pod-1",
    total_active_sources_default=4,
    event_type_score=[],
)
REDIS_CONFIG = RedisConfig(
    stream_url="redis://localhost/0",
    state_url="redis://localhost/1",
    reconnect_interval_s=3,
    stream_claim_timeout_ms=1500,
)
CLAIM_SLEEP_S = REDIS_CONFIG.stream_claim_timeout_ms / 1000
ID_A = UUID("00000000-0000-0000-0000-0000000000e1")
ID_B = UUID("00000000-0000-0000-0000-0000000000e2")
MESSAGE_A = StreamMessage(id="1-0", fields={"event_id": str(ID_A), "v": "1"})
MESSAGE_B = StreamMessage(id="2-0", fields={"event_id": str(ID_B), "v": "1"})
# A later delivery of A's Event, as a separate stream entry.
MESSAGE_A2 = StreamMessage(id="3-0", fields={"event_id": str(ID_A), "v": "1"})
RECLAIMED_A = ReclaimedMessage(id=MESSAGE_A.id, fields=MESSAGE_A.fields, delivery_count=2)
RECLAIMED_B = ReclaimedMessage(id=MESSAGE_B.id, fields=MESSAGE_B.fields, delivery_count=2)
MALFORMED_FIELDS = [
    pytest.param({"v": "1"}, id="missing_event_id"),
    pytest.param({"event_id": "not-a-uuid", "v": "1"}, id="invalid_event_id"),
    pytest.param({"event_id": str(ID_A)}, id="missing_v"),
    pytest.param({"event_id": str(ID_A), "v": "2"}, id="unrecognised_v"),
]


@pytest.fixture
def redis_stream(monkeypatch):
    client = AsyncMock(spec=RedisStreamClient)
    monkeypatch.setattr(stream_client_module, "_redis_stream_client", client)
    return client


@pytest.fixture
def service(monkeypatch):
    scoring_service = AsyncMock(spec=ScoringService)
    monkeypatch.setattr(scoring_service_module, "_scoring_service", scoring_service)
    return scoring_service


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
    redis_stream: AsyncMock, batches: list[object], claims: list[object] | None = None
) -> AsyncMock:
    """Run start() over the given read_from() and autoclaim() results (see _side_effect()),
    each loop stopping by cancellation on its next call. Returns the patched asyncio.sleep, so
    its awaits can be checked."""
    redis_stream.read_from.side_effect = _side_effect(batches)
    redis_stream.autoclaim.side_effect = _side_effect(claims or [])
    with mock.patch("asyncio.sleep", new=AsyncMock()) as sleep:
        await ScoringConsumer(CONFIG, REDIS_CONFIG).start()
    return sleep


def _read_call() -> object:
    return mock.call(
        "stream:event_aggregated",
        "sapi-scoring",
        CONFIG.consumer_name,
        count=CONFIG.batch_size,
        block_ms=CONFIG.batch_timeout_ms,
    )


def _claim_call() -> object:
    return mock.call(
        "stream:event_aggregated",
        "sapi-scoring",
        CONFIG.consumer_name,
        min_idle_ms=REDIS_CONFIG.stream_claim_timeout_ms,
        count=CONFIG.batch_size,
    )


def _dead_letter_call(message: StreamMessage, reason: str) -> object:
    return mock.call(
        message, "stream:event_aggregated", "sapi-scoring", "stream:scoring_dead_letter", reason
    )


def _ack_call(message: StreamMessage) -> object:
    return mock.call("stream:event_aggregated", "sapi-scoring", message.id)


def _scored_ids(service: AsyncMock) -> list[UUID]:
    return [call.args[0] for call in service.score_event.await_args_list]


async def start_acks_scored(redis_stream, service):
    """Each message's Event is scored in stream order, and the message ACKed once it is."""
    sleep = await _run(redis_stream, [[MESSAGE_A, MESSAGE_B], []], [[]])

    redis_stream.create_group_if_not_exists.assert_awaited_once_with(
        "stream:event_aggregated", "sapi-scoring"
    )
    assert redis_stream.read_from.await_args_list == [_read_call()] * 3
    assert redis_stream.autoclaim.await_args_list == [_claim_call()] * 2
    assert sleep.await_args_list == [mock.call(CLAIM_SLEEP_S)] * 2
    assert _scored_ids(service) == [ID_A, ID_B]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_B)]
    redis_stream.dead_letter.assert_not_awaited()


@pytest.mark.parametrize("fields", MALFORMED_FIELDS)
@pytest.mark.parametrize(
    "reclaimed",
    [pytest.param(False, id="read_path"), pytest.param(True, id="reclaim_path")],
)
async def start_malformed_message_dead_lettered(redis_stream, service, fields, reclaimed):
    """A message this layer can never process is dead-lettered without being scored, on
    either path; the rest of its batch is scored as normal."""
    if reclaimed:
        malformed: StreamMessage = ReclaimedMessage(id="9-0", fields=fields, delivery_count=2)
        await _run(redis_stream, [], [[malformed, RECLAIMED_B]])
    else:
        malformed = StreamMessage(id="9-0", fields=fields)
        await _run(redis_stream, [[malformed, MESSAGE_B]])

    assert redis_stream.dead_letter.await_args_list == [_dead_letter_call(malformed, "malformed")]
    assert _scored_ids(service) == [ID_B]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B)]


async def start_reclaimed_retries_exhausted(redis_stream, service):
    """A reclaimed message delivered max_retry times is dead-lettered without being scored;
    one with deliveries left is scored and ACKed."""
    exhausted = ReclaimedMessage(
        id=MESSAGE_A.id, fields=MESSAGE_A.fields, delivery_count=CONFIG.max_retry
    )

    await _run(redis_stream, [], [[exhausted, RECLAIMED_B]])

    assert redis_stream.dead_letter.await_args_list == [
        _dead_letter_call(exhausted, "retries_exhausted")
    ]
    assert _scored_ids(service) == [ID_B]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B)]


async def start_dead_letter_failure_isolated(redis_stream, service):
    """A message whose dead-lettering fails is left un-ACKed, and the rest of its batch is
    scored as normal."""
    redis_stream.dead_letter.side_effect = redis.exceptions.ResponseError("OOM")
    malformed = StreamMessage(id="9-0", fields={"v": "1"})

    await _run(redis_stream, [[malformed, MESSAGE_B], []])

    assert redis_stream.dead_letter.await_args_list == [_dead_letter_call(malformed, "malformed")]
    assert _scored_ids(service) == [ID_B]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B)]
    assert redis_stream.read_from.await_count == 3


@pytest.mark.parametrize(
    "delivery_count",
    [pytest.param(2, id="retries_left"), pytest.param(3, id="retries_exhausted")],
)
async def start_reclaimed_in_flight_skipped(redis_stream, service, delivery_count):
    """A reclaimed message whose Event is still being scored is skipped and not dead-lettered,
    even once out of retries; its first delivery ACKs it once scored."""
    scoring = asyncio.Event()
    finish = asyncio.Event()

    async def hold(event_id: UUID) -> None:
        scoring.set()
        await finish.wait()

    service.score_event.side_effect = hold
    reclaimed = ReclaimedMessage(
        id=MESSAGE_A.id, fields=MESSAGE_A.fields, delivery_count=delivery_count
    )

    async def claim_while_scoring() -> list[StreamMessage]:
        await scoring.wait()
        return [reclaimed]

    async def finish_first() -> list[StreamMessage]:
        finish.set()
        return []

    await _run(redis_stream, [[MESSAGE_A]], [claim_while_scoring, finish_first])

    assert _scored_ids(service) == [ID_A]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]
    redis_stream.dead_letter.assert_not_awaited()


async def start_reclaimed_scored(redis_stream, service):
    """A reclaimed message with deliveries left is scored and ACKed, and once its Event is
    done, a later delivery of it is scored again."""
    await _run(redis_stream, [], [[RECLAIMED_A], [MESSAGE_A2]])

    assert _scored_ids(service) == [ID_A, ID_A]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_A2)]
    redis_stream.dead_letter.assert_not_awaited()


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
async def start_score_failed(redis_stream, service, error):
    """A failed scoring leaves its message un-ACKed while the rest of the batch carries on,
    and a redelivery is scored again."""
    service.score_event.side_effect = [error, None, None]

    sleep = await _run(redis_stream, [[MESSAGE_A, MESSAGE_B], [MESSAGE_A2]])

    assert _scored_ids(service) == [ID_A, ID_B, ID_A]
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
    assert sorted(call.args[0] for call in sleep.await_args_list) == sorted(
        [REDIS_CONFIG.reconnect_interval_s] * 2 + [CLAIM_SLEEP_S]
    )
    assert redis_stream.read_from.await_count == 4
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]


async def start_redis_unreachable_on_ack(redis_stream, service):
    """A failed ACK goes through the same retry, leaving the rest of its batch pending, and a
    redelivery is scored again."""
    redis_stream.ack.side_effect = [redis.exceptions.ConnectionError("down"), None]

    sleep = await _run(redis_stream, [[MESSAGE_A, MESSAGE_B], [MESSAGE_A2]])

    assert sorted(call.args[0] for call in sleep.await_args_list) == sorted(
        [REDIS_CONFIG.reconnect_interval_s, CLAIM_SLEEP_S]
    )
    assert _scored_ids(service) == [ID_A, ID_A]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_A2)]


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
    assert _scored_ids(service) == [ID_A]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A)]


async def start_loop_failure_stops_layer(redis_stream, service):
    """An unexpected error in one loop stops the other one, and start() raises it."""
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
        await ScoringConsumer(CONFIG, REDIS_CONFIG).start()

    assert raised.value.exceptions == (error,)
    assert stopped == ["reclaim_loop"]
    service.score_event.assert_not_awaited()


async def start_cancelled(redis_stream, service):
    """Cancelling start() stops both loops, and a restart of the same consumer scores the
    Event whose scoring was cancelled."""
    scoring = asyncio.Event()
    claiming = asyncio.Event()
    stopped: list[str] = []

    async def hold(event_id: UUID) -> None:
        scoring.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append("consumer_loop")

    async def claim(*_args: object, **_kwargs: object) -> list[StreamMessage]:
        claiming.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append("reclaim_loop")
        return []

    service.score_event.side_effect = hold
    redis_stream.read_from.side_effect = _side_effect([[MESSAGE_A]])
    redis_stream.autoclaim.side_effect = claim
    consumer = ScoringConsumer(CONFIG, REDIS_CONFIG)
    with mock.patch("asyncio.sleep", new=AsyncMock()):
        task = asyncio.create_task(consumer.start())
        await scoring.wait()
        await claiming.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        service.score_event.side_effect = None
        redis_stream.read_from.side_effect = _side_effect([[MESSAGE_A2]])
        redis_stream.autoclaim.side_effect = _side_effect([])
        await consumer.start()

    assert sorted(stopped) == ["consumer_loop", "reclaim_loop"]
    assert _scored_ids(service) == [ID_A, ID_A]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A2)]
