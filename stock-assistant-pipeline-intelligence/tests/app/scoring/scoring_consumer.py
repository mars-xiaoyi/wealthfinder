import asyncio
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
from app.redis.stream_client import RedisStreamClient, StreamMessage
from app.scoring import scoring_service as scoring_service_module
from app.scoring.scoring_consumer import ScoringConsumer
from app.scoring.scoring_service import ScoringService

# A loop bug shows up as a hang, so fail fast instead.
pytestmark = [pytest.mark.timeout(5)]

CONFIG = ScoringConfig(
    batch_size=5,
    batch_timeout_ms=200,
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
ID_A = UUID("00000000-0000-0000-0000-0000000000e1")
ID_B = UUID("00000000-0000-0000-0000-0000000000e2")
MESSAGE_A = StreamMessage(id="1-0", fields={"event_id": str(ID_A), "v": "1"})
MESSAGE_B = StreamMessage(id="2-0", fields={"event_id": str(ID_B), "v": "1"})
# A later delivery of A's Event, as a separate stream entry.
MESSAGE_A2 = StreamMessage(id="3-0", fields={"event_id": str(ID_A), "v": "1"})


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


async def _run(redis_stream: AsyncMock, batches: list[object]) -> AsyncMock:
    """Run start() over the given read_from() results (a list of messages or an exception to
    raise), stopping by cancellation on the next read. Returns the patched asyncio.sleep, so
    its awaits can be checked."""
    results = iter([*batches, asyncio.CancelledError()])

    async def read_from(*_args, **_kwargs):
        result = next(results)
        if isinstance(result, BaseException):
            raise result
        return result

    redis_stream.read_from.side_effect = read_from
    with (
        mock.patch("asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(asyncio.CancelledError),
    ):
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


def _ack_call(message: StreamMessage) -> object:
    return mock.call("stream:event_aggregated", "sapi-scoring", message.id)


def _scored_ids(service: AsyncMock) -> list[UUID]:
    return [call.args[0] for call in service.score_event.await_args_list]


async def start_acks_scored(redis_stream, service):
    """Each message's Event is scored in stream order, and the message ACKed once it is."""
    await _run(redis_stream, [[MESSAGE_A, MESSAGE_B], []])

    redis_stream.create_group_if_not_exists.assert_awaited_once_with(
        "stream:event_aggregated", "sapi-scoring"
    )
    assert redis_stream.read_from.await_args_list == [_read_call()] * 3
    assert _scored_ids(service) == [ID_A, ID_B]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_B)]


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"v": "1"}, id="missing_event_id"),
        pytest.param({"event_id": "not-a-uuid", "v": "1"}, id="invalid_event_id"),
    ],
)
async def start_invalid_message_left_unacked(redis_stream, service, fields):
    """A message without a valid event_id is left un-ACKed, and the rest of its batch is
    scored as normal."""
    await _run(redis_stream, [[StreamMessage(id="9-0", fields=fields), MESSAGE_B]])

    assert _scored_ids(service) == [ID_B]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_B)]


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
    """A failed ACK goes through the same retry, leaving the rest of its batch pending, and a
    redelivery is scored again."""
    redis_stream.ack.side_effect = [redis.exceptions.ConnectionError("down"), None]

    sleep = await _run(redis_stream, [[MESSAGE_A, MESSAGE_B], [MESSAGE_A2]])

    assert sleep.await_args_list == [mock.call(REDIS_CONFIG.reconnect_interval_s)]
    assert _scored_ids(service) == [ID_A, ID_A]
    assert redis_stream.ack.await_args_list == [_ack_call(MESSAGE_A), _ack_call(MESSAGE_A2)]
