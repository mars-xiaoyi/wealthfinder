import asyncio
import functools
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
import redis.asyncio
import redis.exceptions
import structlog.testing

from app import logger
from app.common import error_codes, hk_stock_list
from app.common.error_categories import ErrorCategory
from app.redis.stream_client import CONSUMER_GROUP_ENTITY_ANALYSIS, STREAM_RAW_NEWS_CLEANED
from tests.integration.conftest import REFUSED_URL, Infra, StubSADI

pytestmark = [pytest.mark.live, pytest.mark.timeout(120)]

CONSUMER_ENV = {
    "HTTP_TIMEOUT_S": "1",
    "ENTITY_ANALYSIS_BATCH_SIZE": "5",
    "ENTITY_ANALYSIS_BATCH_TIMEOUT_MS": "200",
    "REDIS_RECONNECT_INTERVAL_S": "1",
}
POLL_INTERVAL_S = 0.05
SETTLE_S = 0.5


class StreamProbe:
    """The test's own view of the stream Redis: XADD in, ACK state out."""

    def __init__(self, client: redis.asyncio.Redis) -> None:
        self._client = client

    async def add(self, cleaned_id: uuid.UUID) -> str:
        """XADD a stream:raw_news_cleaned message for cleaned_id; returns its stream id."""
        message_id: str = await self._client.xadd(
            STREAM_RAW_NEWS_CLEANED, {"cleaned_id": str(cleaned_id), "v": "1"}
        )
        return message_id

    async def group_names(self) -> list[str]:
        """Names of the stream's consumer groups; none while the stream does not exist."""
        try:
            groups = await self._client.xinfo_groups(STREAM_RAW_NEWS_CLEANED)
        except redis.exceptions.ResponseError:
            return []
        return [str(group["name"]) for group in groups]

    async def pending(self) -> dict[str, int]:
        """{stream id: delivery count} of every delivered but un-ACKed message."""
        entries = await self._client.xpending_range(
            STREAM_RAW_NEWS_CLEANED, CONSUMER_GROUP_ENTITY_ANALYSIS, "-", "+", 100
        )
        return {entry["message_id"]: entry["times_delivered"] for entry in entries}


@pytest.fixture
async def streams(infra: Infra) -> AsyncIterator[StreamProbe]:
    client = redis.asyncio.Redis.from_url(
        f"redis://127.0.0.1:{infra.redis_stream.port}/0", decode_responses=True
    )
    await client.flushall()
    yield StreamProbe(client)
    await client.aclose()


@pytest.fixture
def start_consumer(
    start_app: Callable[..., object],
    hkex_urls: Callable[..., None],
    stub_sadi: StubSADI,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., object]:
    """start_app() with SADI pointed at the stub and test timings. The HK Stock List load
    fails at once (refused, no connect retries): the consumer does not read the list."""
    hkex_urls(every=REFUSED_URL)
    monkeypatch.setattr(hk_stock_list, "_CONNECT_RETRY_WAITS_S", (0, 0))
    return functools.partial(start_app, **CONSUMER_ENV, SADI_API_URL=stub_sadi.url)


async def _wait_for(condition: Callable[[], Awaitable[bool]], timeout_s: float) -> float:
    """Poll condition until it holds; fails the test after timeout_s. Returns the seconds
    waited."""
    started = time.monotonic()
    while not await condition():
        if time.monotonic() - started > timeout_s:
            pytest.fail(f"condition not met within {timeout_s}s")
        await asyncio.sleep(POLL_INTERVAL_S)
    return time.monotonic() - started


async def _wait_acked(
    streams: StreamProbe,
    stub: StubSADI,
    entries: dict[uuid.UUID, str],
    timeout_s: float,
) -> float:
    """Wait until SADI was asked for every cleaned_id in entries ({cleaned_id: stream id})
    and none of their messages is pending any more."""

    async def acked() -> bool:
        if not all(stub.requests_for(cleaned_id) for cleaned_id in entries):
            return False
        return not set(entries.values()) & set(await streams.pending())

    return await _wait_for(acked, timeout_s)


async def _wait_pending(streams: StreamProbe, message_id: str, timeout_s: float) -> None:
    async def is_pending() -> bool:
        return message_id in await streams.pending()

    await _wait_for(is_pending, timeout_s)


async def _wait_group_created(streams: StreamProbe) -> None:
    """Wait until the consumer has created its group, i.e. is up and reading."""

    async def group_created() -> bool:
        return await streams.group_names() == [CONSUMER_GROUP_ENTITY_ANALYSIS]

    await _wait_for(group_created, 3)


async def start_creates_group(start_consumer, streams):
    async with start_consumer():
        await _wait_group_created(streams)


async def start_existing_group(start_consumer, streams):
    """A group left by an earlier run is not an error, and is not created twice."""
    async with start_consumer():
        await _wait_group_created(streams)

    with structlog.testing.capture_logs() as logs:
        async with start_consumer():

            async def group_noticed() -> bool:
                return any(log["event"] == "consumer_group_exists" for log in logs)

            await _wait_for(group_noticed, 3)

    assert await streams.group_names() == [CONSUMER_GROUP_ENTITY_ANALYSIS]


async def start_fetch_and_ack(start_consumer, streams, stub_sadi):
    """More messages than one batch: each cleaned_id reaches SADI as a canonical UUID, once,
    and every message is ACKed."""
    cleaned_ids = [uuid.uuid4() for _ in range(12)]

    async with start_consumer():
        entries = {cleaned_id: await streams.add(cleaned_id) for cleaned_id in cleaned_ids}
        await _wait_acked(streams, stub_sadi, entries, 10)
        await asyncio.sleep(SETTLE_S)

    assert sorted(r.cleaned_id for r in stub_sadi.requests) == sorted(map(str, cleaned_ids))
    assert await streams.pending() == {}


async def start_not_found_acked(start_consumer, streams, stub_sadi):
    cleaned_id = uuid.uuid4()
    stub_sadi.respond(cleaned_id, 404)

    with structlog.testing.capture_logs() as logs:
        async with start_consumer():
            message_id = await streams.add(cleaned_id)
            await _wait_acked(streams, stub_sadi, {cleaned_id: message_id}, 5)
            await asyncio.sleep(SETTLE_S)

    assert len(stub_sadi.requests_for(cleaned_id)) == 1
    assert await streams.pending() == {}
    not_found = [log for log in logs if log["event"] == "cleaned_news_not_found"]
    assert [log["log_level"] for log in not_found] == ["warning"]


async def start_sadi_error_left_pending(start_consumer, streams, stub_sadi):
    """A SADI failure leaves its message pending, and the next message is still handled."""
    failing, healthy = uuid.uuid4(), uuid.uuid4()
    stub_sadi.respond(failing, 503)

    async with start_consumer():
        with structlog.testing.capture_logs(processors=[logger.add_sapi_error_fields]) as logs:
            failing_message_id = await streams.add(failing)
            healthy_message_id = await streams.add(healthy)
            await _wait_acked(streams, stub_sadi, {healthy: healthy_message_id}, 5)
            await asyncio.sleep(SETTLE_S)

    assert await streams.pending() == {failing_message_id: 1}
    assert len(stub_sadi.requests_for(failing)) == 1
    failures = [log for log in logs if log.get("error_code") == error_codes.UPSTREAM_UNAVAILABLE]
    assert [(log["log_level"], log["category"]) for log in failures] == [
        ("warning", ErrorCategory.NEWS_CLIENT)
    ]
    assert not [log for log in logs if log["log_level"] in ("error", "critical")]


async def start_sadi_timeout_left_pending(start_consumer, streams, stub_sadi):
    """A SADI answer slower than HTTP_TIMEOUT_S holds the loop for the timeout, not for the
    full delay."""
    slow, healthy = uuid.uuid4(), uuid.uuid4()
    stub_sadi.respond(slow, 200, delay_s=3)

    async with start_consumer():
        slow_message_id = await streams.add(slow)
        await _wait_for(lambda: _requested(stub_sadi, slow), 5)
        healthy_message_id = await streams.add(healthy)
        await _wait_acked(streams, stub_sadi, {healthy: healthy_message_id}, 6)

    assert (await streams.pending())[slow_message_id] == 1


async def _requested(stub: StubSADI, cleaned_id: uuid.UUID) -> bool:
    return bool(stub.requests_for(cleaned_id))


async def start_sadi_unreachable_then_back(start_consumer, streams, stub_sadi):
    """With SADI down, the message stays pending and the app keeps serving; once SADI is
    back, the same app instance handles new messages."""
    unreachable, recovered = uuid.uuid4(), uuid.uuid4()

    async with start_consumer() as client:
        stub_sadi.stop()
        unreachable_message_id = await streams.add(unreachable)
        await _wait_pending(streams, unreachable_message_id, 5)
        await asyncio.wait_for(client.get("/v1/health"), 3)

        stub_sadi.start()
        recovered_message_id = await streams.add(recovered)
        await _wait_acked(streams, stub_sadi, {recovered: recovered_message_id}, 5)

    assert (await streams.pending())[unreachable_message_id] == 1


@pytest.mark.parametrize("outage", ["stop", "pause"])
async def start_stream_redis_outage(start_consumer, streams, stub_sadi, infra, outage):
    """While the stream Redis is down the consumer warns and retries without raising, and
    resumes from the group's position once it is back, on the same app instance."""
    resumed = uuid.uuid4()
    redis_stream = infra.redis_stream

    async with start_consumer() as client:
        await _wait_group_created(streams)
        with structlog.testing.capture_logs() as logs:
            if outage == "stop":
                redis_stream.stop()
            else:
                redis_stream.pause()
            await asyncio.sleep(4)
            if outage == "stop":
                health = (await asyncio.wait_for(client.get("/v1/health"), 10)).json()

        redis_stream.restore()
        message_id = await streams.add(resumed)
        await _wait_acked(streams, stub_sadi, {resumed: message_id}, 10)

    if outage == "stop":
        warnings = [
            log
            for log in logs
            if log["event"] == "entity_analysis_redis_stream_unreachable"
            and log["log_level"] == "warning"
        ]
        assert len(warnings) >= 2
        assert not [log for log in logs if log["log_level"] in ("error", "critical")]
        assert (health["redis_stream"], health["database"], health["redis_state"]) == (
            "error",
            "ok",
            "ok",
        )


async def start_postgres_and_state_redis_down(start_consumer, streams, stub_sadi, infra):
    """The consumer needs only the stream Redis and SADI."""
    cleaned_id = uuid.uuid4()

    async with start_consumer():
        infra.postgres.stop()
        infra.redis_state.stop()
        message_id = await streams.add(cleaned_id)
        await _wait_acked(streams, stub_sadi, {cleaned_id: message_id}, 5)


async def start_resumes_after_restart(start_consumer, streams, stub_sadi):
    """The group's position is kept across an app restart: a message added while the app is
    down is handled, and nothing already ACKed is requested again."""
    before = [uuid.uuid4(), uuid.uuid4()]
    after = uuid.uuid4()

    async with start_consumer():
        entries = {cleaned_id: await streams.add(cleaned_id) for cleaned_id in before}
        await _wait_acked(streams, stub_sadi, entries, 5)

    message_id = await streams.add(after)

    async with start_consumer():
        await _wait_acked(streams, stub_sadi, {after: message_id}, 5)
        await asyncio.sleep(SETTLE_S)

    assert [len(stub_sadi.requests_for(cleaned_id)) for cleaned_id in [*before, after]] == [1, 1, 1]
