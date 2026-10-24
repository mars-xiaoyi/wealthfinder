import asyncio
import functools
import io
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from typing import Any

import openpyxl
import pytest
import redis.asyncio
import redis.exceptions
import structlog.testing

from app import logger
from app.common import error_codes, hk_stock_list
from app.common.error_categories import ErrorCategory
from app.redis.stream_client import CONSUMER_GROUP_ENTITY_ANALYSIS, STREAM_RAW_NEWS_CLEANED
from tests.integration.conftest import (
    HKEX_URL_CONSTANTS,
    REFUSED_URL,
    Infra,
    StubHKEX,
    StubResponse,
    StubSADI,
)

pytestmark = [pytest.mark.live, pytest.mark.timeout(120)]

CONSUMER_ENV = {
    "HTTP_TIMEOUT_S": "1",
    "ENTITY_ANALYSIS_BATCH_SIZE": "5",
    "ENTITY_ANALYSIS_BATCH_TIMEOUT_MS": "200",
    "REDIS_RECONNECT_INTERVAL_S": "1",
}
POLL_INTERVAL_S = 0.05
SETTLE_S = 0.5
GATE_EVENT = "hk_stock_list_not_ready"
REAL_HKEX_URLS = {name: getattr(hk_stock_list, name) for name in HKEX_URL_CONSTANTS}

# A trimmed copy of HKEX's three files: 00700 and 09988 are equities, 02800 is an ETF.
EN_FEED = [
    {"i": 1, "c": "00700", "n": "TENCENT", "s": 1},
    {"i": 2, "c": "02800", "n": "TRACKER FUND", "s": 2},
    {"i": 3, "c": "09988", "n": "BABA-W", "s": 3},
]
ZH_FEED = [
    {"i": 1, "c": "00700", "n": "騰訊控股", "s": 1},
    {"i": 2, "c": "02800", "n": "盈富基金", "s": 2},
    {"i": 3, "c": "09988", "n": "阿里巴巴", "s": 3},
]
SECURITIES_FEED = [
    ("00700", "TENCENT", "Equity", "Equity Securities (Main Board)", "HKD"),
    ("02800", "TRACKER FUND", "Exchange Traded Products", "Exchange Traded Funds", "HKD"),
    ("09988", "BABA-W", "Equity", "Equity Securities (Main Board)", "HKD"),
]
LOADED_FEED_ENTRIES = 2
# CONSUMER_ENV's HTTP_TIMEOUT_S is too short to download the real HKEX files.
REAL_HKEX_ENV = {"HTTP_TIMEOUT_S": "30"}


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


def _securities_xlsx() -> bytes:
    """SECURITIES_FEED as a List of Securities spreadsheet, with HKEX's two title rows."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(("List of Securities",))
    sheet.append(("Updated as at 24/10/2026",))
    sheet.append(
        ("Stock Code", "Name of Securities", "Category", "Sub-Category", "Trading Currency")
    )
    for row in SECURITIES_FEED:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def hkex_feed(
    hkex_urls: Callable[..., None],
    stub_hkex: Callable[[StubResponse], StubHKEX],
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str], None]:
    """Choose where the HK Stock List loads from: hkex_feed("loaded") serves the trimmed feed
    from local stubs, hkex_feed("empty") refuses at once (no connect retries) so the empty
    fallback list is installed, and hkex_feed("real") uses the real HKEX."""

    def point(feed: str) -> None:
        if feed == "empty":
            hkex_urls(every=REFUSED_URL)
            monkeypatch.setattr(hk_stock_list, "_CONNECT_RETRY_WAITS_S", (0, 0))
        elif feed == "loaded":
            bodies = {
                "_ACTIVESTOCK_EN_URL": json.dumps(EN_FEED).encode(),
                "_ACTIVESTOCK_ZH_URL": json.dumps(ZH_FEED).encode(),
                "_LIST_OF_SECURITIES_URL": _securities_xlsx(),
            }
            hkex_urls(
                **{name: stub_hkex(StubResponse(200, body)).url for name, body in bodies.items()}
            )
        else:
            hkex_urls(**REAL_HKEX_URLS)

    return point


@pytest.fixture
def start_consumer(
    start_app: Callable[..., object],
    hkex_feed: Callable[[str], None],
    stub_sadi: StubSADI,
) -> Callable[..., object]:
    """start_app() with SADI pointed at the stub and test timings. The HK Stock List loads
    from the trimmed feed unless the test points it elsewhere first (hkex_feed)."""
    hkex_feed("loaded")
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


def _gated(logs: list[MutableMapping[str, Any]]) -> list[tuple[object, object]]:
    """(log_level, message_id) of each hk_stock_list_not_ready record."""
    return [(log["log_level"], log["message_id"]) for log in logs if log["event"] == GATE_EVENT]


async def _wait_gated(logs: list[MutableMapping[str, Any]], count: int, timeout_s: float) -> None:
    async def gated() -> bool:
        return len(_gated(logs)) >= count

    await _wait_for(gated, timeout_s)


async def start_hk_stock_list_not_ready_holds_message(
    start_consumer, hkex_feed, streams, stub_sadi
):
    """With the HK Stock List empty, the message stays pending and SADI is never asked; the
    app keeps running."""
    hkex_feed("empty")

    with structlog.testing.capture_logs() as logs:
        async with start_consumer() as client:
            message_id = await streams.add(uuid.uuid4())
            await _wait_gated(logs, 1, 3)
            await asyncio.sleep(3)
            await asyncio.wait_for(client.get("/v1/health"), 3)

    assert _gated(logs) == [("warning", message_id)]
    assert stub_sadi.requests == []
    assert await streams.pending() == {message_id: 1}


async def start_hk_stock_list_not_ready_every_message(
    start_consumer, hkex_feed, streams, stub_sadi
):
    hkex_feed("empty")

    with structlog.testing.capture_logs() as logs:
        async with start_consumer():
            message_ids = [await streams.add(uuid.uuid4()) for _ in range(3)]
            await _wait_gated(logs, 3, 3)
            await asyncio.sleep(SETTLE_S)

    assert sorted(_gated(logs)) == sorted(("warning", message_id) for message_id in message_ids)
    assert stub_sadi.requests == []
    assert await streams.pending() == dict.fromkeys(message_ids, 1)


async def start_hk_stock_list_not_ready_before_fetch(start_consumer, hkex_feed, streams, stub_sadi):
    """The gate comes before the fetch: a message SADI would answer 404 for (an ACKed outcome)
    stays pending too."""
    not_found, unavailable = uuid.uuid4(), uuid.uuid4()
    stub_sadi.respond(not_found, 404)
    stub_sadi.respond(unavailable, 503)
    hkex_feed("empty")

    with structlog.testing.capture_logs() as logs:
        async with start_consumer():
            not_found_message_id = await streams.add(not_found)
            unavailable_message_id = await streams.add(unavailable)
            await _wait_gated(logs, 2, 3)
            await asyncio.sleep(SETTLE_S)

    assert stub_sadi.requests == []
    assert await streams.pending() == {not_found_message_id: 1, unavailable_message_id: 1}


@pytest.mark.parametrize("feed", ["loaded", "real"])
async def start_hk_stock_list_loaded(start_consumer, hkex_feed, streams, stub_sadi, feed):
    """With the HK Stock List loaded, from the trimmed feed or the real HKEX, a message is
    fetched and ACKed with no gate record."""
    cleaned_id = uuid.uuid4()
    hkex_feed(feed)
    env = REAL_HKEX_ENV if feed == "real" else {}

    with structlog.testing.capture_logs() as logs:
        async with start_consumer(**env):
            message_id = await streams.add(cleaned_id)
            await _wait_acked(streams, stub_sadi, {cleaned_id: message_id}, 5)
            await asyncio.sleep(SETTLE_S)

    assert [r.cleaned_id for r in stub_sadi.requests] == [str(cleaned_id)]
    assert await streams.pending() == {}
    assert _gated(logs) == []


async def start_hk_stock_list_becomes_ready(start_consumer, hkex_feed, streams, stub_sadi):
    """Once the HK Stock List loads on the running app, new messages are handled; the one
    gated before stays pending."""
    gated, handled = uuid.uuid4(), uuid.uuid4()
    hkex_feed("empty")

    with structlog.testing.capture_logs() as logs:
        async with start_consumer():
            gated_message_id = await streams.add(gated)
            await _wait_gated(logs, 1, 3)

            hkex_feed("loaded")
            assert await hk_stock_list.refresh_hk_stock_list() == LOADED_FEED_ENTRIES
            handled_message_id = await streams.add(handled)
            await _wait_acked(streams, stub_sadi, {handled: handled_message_id}, 5)
            pending = await streams.pending()

    assert _gated(logs) == [("warning", gated_message_id)]
    assert [r.cleaned_id for r in stub_sadi.requests] == [str(handled)]
    assert pending == {gated_message_id: 1}
