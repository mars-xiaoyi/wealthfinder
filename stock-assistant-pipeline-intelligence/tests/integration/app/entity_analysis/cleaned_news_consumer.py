import asyncio
import dataclasses
import functools
import io
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from datetime import UTC, datetime
from typing import Any

import asyncpg
import openpyxl
import pytest
import redis.asyncio
import redis.exceptions
import structlog.testing

from app import logger, main
from app.common import error_codes, hk_stock_list
from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.llm import vertex_adapter
from app.llm.adapter import LLMAdapter, StructuredLLMResponse
from app.llm.errors import (
    LLMAuthenticationError,
    LLMError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequestError,
    LLMSchemaViolationError,
)
from app.models.entity_event import EventTypeEnum, SentimentEnum
from app.skills.entity_analysis_skill import ExtractedEntity, LLMOutput
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
    "DB_WRITE_MAX_RETRY": "3",
    "DB_RETRY_BASE_WAIT_MS": "100",
}
POLL_INTERVAL_S = 0.05
SETTLE_S = 0.5
# Bound on Vertex AI-backed work: one analysis took up to 18 s in the LLM skill spike.
VERTEX_WAIT_S = 60
GATE_EVENT = "hk_stock_list_not_ready"
COMPLETED_EVENT = "entity_analysis_completed"
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
            "stream:raw_news_cleaned",
            {"cleaned_id": str(cleaned_id), "v": "1"},
        )
        return message_id

    async def group_names(self) -> list[str]:
        """Names of the stream's consumer groups; none while the stream does not exist."""
        try:
            groups = await self._client.xinfo_groups("stream:raw_news_cleaned")
        except redis.exceptions.ResponseError:
            return []
        return [str(group["name"]) for group in groups]

    async def pending(self) -> dict[str, int]:
        """{stream id: delivery count} of every delivered but un-ACKed message."""
        entries = await self._client.xpending_range(
            "stream:raw_news_cleaned", "sapi-entity-analysis", "-", "+", 100
        )
        return {entry["message_id"]: entry["times_delivered"] for entry in entries}

    async def published(self) -> list[dict[str, str]]:
        """The fields of every stream:entity_event_completed entry, oldest first."""
        entries = await self._client.xrange("stream:entity_event_completed")
        return [fields for _message_id, fields in entries]

    async def reconnect(self) -> None:
        """Drop the probe's connections, e.g. after the stream Redis restarted."""
        await self._client.connection_pool.disconnect()


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


def _completed(
    logs: list[MutableMapping[str, Any]], cleaned_id: uuid.UUID
) -> list[MutableMapping[str, Any]]:
    """The entity_analysis_completed records for cleaned_id."""
    return [
        log
        for log in logs
        if log["event"] == COMPLETED_EVENT and log["cleaned_id"] == str(cleaned_id)
    ]


async def _wait_group_created(streams: StreamProbe) -> None:
    """Wait until the consumer has created its group, i.e. is up and reading."""

    async def group_created() -> bool:
        return await streams.group_names() == ["sapi-entity-analysis"]

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

    assert await streams.group_names() == ["sapi-entity-analysis"]


@pytest.mark.vertex
# Six sequential Vertex AI analyses: room for the 3 * VERTEX_WAIT_S wait below, plus startup.
@pytest.mark.timeout(4 * VERTEX_WAIT_S)
async def start_fetch_and_ack(start_consumer, streams, stub_sadi):
    """More messages than one batch: each cleaned_id reaches SADI as a canonical UUID, once,
    is analysed once, and every message is ACKed."""
    cleaned_ids = [uuid.uuid4() for _ in range(6)]

    with structlog.testing.capture_logs() as logs:
        async with start_consumer():
            entries = {cleaned_id: await streams.add(cleaned_id) for cleaned_id in cleaned_ids}
            await _wait_acked(streams, stub_sadi, entries, 3 * VERTEX_WAIT_S)
            await asyncio.sleep(SETTLE_S)

    assert sorted(r.cleaned_id for r in stub_sadi.requests) == sorted(map(str, cleaned_ids))
    assert await streams.pending() == {}
    assert [len(_completed(logs, cleaned_id)) for cleaned_id in cleaned_ids] == [1] * 6


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
    assert _completed(logs, cleaned_id) == []


async def start_sadi_error_left_pending(start_consumer, streams, stub_sadi):
    """A SADI failure leaves its message pending without analysing it, and the next message
    is still handled."""
    failing, healthy = uuid.uuid4(), uuid.uuid4()
    stub_sadi.respond(failing, 503)
    stub_sadi.respond(healthy, 404)  # ACKed without reaching Vertex AI

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
    assert _completed(logs, failing) == []


async def start_sadi_timeout_left_pending(start_consumer, streams, stub_sadi):
    """A SADI answer slower than HTTP_TIMEOUT_S holds the loop for the timeout, not for the
    full delay."""
    slow, healthy = uuid.uuid4(), uuid.uuid4()
    stub_sadi.respond(slow, 200, delay_s=3)
    stub_sadi.respond(healthy, 404)  # ACKed without reaching Vertex AI

    with structlog.testing.capture_logs() as logs:
        async with start_consumer():
            slow_message_id = await streams.add(slow)
            await _wait_for(lambda: _requested(stub_sadi, slow), 5)
            healthy_message_id = await streams.add(healthy)
            await _wait_acked(streams, stub_sadi, {healthy: healthy_message_id}, 6)

    assert (await streams.pending())[slow_message_id] == 1
    assert _completed(logs, slow) == []


async def _requested(stub: StubSADI, cleaned_id: uuid.UUID) -> bool:
    return bool(stub.requests_for(cleaned_id))


async def start_sadi_unreachable_then_back(start_consumer, streams, stub_sadi):
    """With SADI down, the message stays pending and the app keeps serving; once SADI is
    back, the same app instance handles new messages."""
    unreachable, recovered = uuid.uuid4(), uuid.uuid4()
    stub_sadi.respond(recovered, 404)  # ACKed without reaching Vertex AI

    with structlog.testing.capture_logs() as logs:
        async with start_consumer() as client:
            stub_sadi.stop()
            unreachable_message_id = await streams.add(unreachable)
            await _wait_pending(streams, unreachable_message_id, 5)
            await asyncio.wait_for(client.get("/v1/health"), 3)

            stub_sadi.start()
            recovered_message_id = await streams.add(recovered)
            await _wait_acked(streams, stub_sadi, {recovered: recovered_message_id}, 5)

    assert (await streams.pending())[unreachable_message_id] == 1
    assert _completed(logs, unreachable) == []


@pytest.mark.parametrize("outage", ["stop", "pause"])
async def start_stream_redis_outage(start_consumer, streams, stub_sadi, infra, outage):
    """While the stream Redis is down the consumer warns and retries without raising, and
    resumes from the group's position once it is back, on the same app instance."""
    resumed = uuid.uuid4()
    stub_sadi.respond(resumed, 404)  # ACKed without reaching Vertex AI
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


async def start_resumes_after_restart(start_consumer, streams, stub_sadi):
    """The group's position is kept across an app restart: a message added while the app is
    down is handled, and nothing already ACKed is requested again."""
    before = [uuid.uuid4(), uuid.uuid4()]
    after = uuid.uuid4()
    for cleaned_id in [*before, after]:
        stub_sadi.respond(cleaned_id, 404)  # ACKed without reaching Vertex AI

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
    stub_sadi.respond(cleaned_id, 404)  # ACKed without reaching Vertex AI
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
    stub_sadi.respond(handled, 404)  # ACKed without reaching Vertex AI
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


# Hand-written, not one of the system prompt's few-shot examples. Both codes are printed, and
# both companies are stable HKEX listings; swap a reference that stops being valid rather than
# weaken an assertion.
LISTED_TITLE = "匯豐控股及友邦保險公佈季度業績"
LISTED_BODY = (
    "匯豐控股（00005）公佈第三季除稅前利潤按年上升10%，超出市場預期。"  # noqa: RUF001
    "友邦保險（01299）新業務價值按年增長15%，並宣佈增加股份回購。"  # noqa: RUF001
)
UNLISTED_TITLE = "輝達公佈季度業績"
UNLISTED_BODY = "美國晶片公司輝達公佈季度業績，數據中心收入按年大增，股價盤後上升。"  # noqa: RUF001
# A project id the developer's credentials cannot access: Vertex AI answers 403.
INACCESSIBLE_VERTEX_PROJECT = "sapi-it-no-access-7f3a9c"
LLM_ERROR_TYPES = {
    "LLMRateLimitError",
    "LLMProviderError",
    "LLMRequestError",
    "LLMAuthenticationError",
    "LLMSchemaViolationError",
}


async def _wait_completed(
    logs: list[MutableMapping[str, Any]], cleaned_id: uuid.UUID, timeout_s: float
) -> None:
    async def completed() -> bool:
        return bool(_completed(logs, cleaned_id))

    await _wait_for(completed, timeout_s)


@pytest.mark.vertex
async def start_listed_companies_verified(
    start_consumer, hkex_feed, streams, stub_sadi, entity_events
):
    """An article naming two listed companies is analysed in one call and both are verified
    against the real HK Stock List, stored and announced; the message is ACKed."""
    cleaned_id = uuid.uuid4()
    stub_sadi.serve(cleaned_id, LISTED_TITLE, LISTED_BODY)
    hkex_feed("real")

    with structlog.testing.capture_logs() as logs:
        async with start_consumer(**REAL_HKEX_ENV):
            started = time.monotonic()
            message_id = await streams.add(cleaned_id)
            await _wait_acked(streams, stub_sadi, {cleaned_id: message_id}, VERTEX_WAIT_S)
            elapsed_ms = (time.monotonic() - started) * 1000
            await asyncio.sleep(SETTLE_S)

    [record] = _completed(logs, cleaned_id)
    assert record["log_level"] == "info"
    assert record["source_url"] == f"https://finance.mingpao.com/{cleaned_id}"
    assert record["skill_version"] == "v1.0.0"
    assert set(record["verified_entities"]) == {"00005", "01299"}
    assert record["unverified_entities"] == []
    assert record["input_truncated"] is False
    # The Skill records the constraint fixes it applies as BusinessValidationError entries.
    assert [e for e in record["llm_errors"] if e["error_type"] in LLM_ERROR_TYPES] == []
    rows = await entity_events.rows(f"https://finance.mingpao.com/{cleaned_id}")
    assert [row["stock_code"] for row in rows] == ["00005", "01299"]
    assert sorted(record["entity_event_ids"]) == sorted(str(row["entity_event_id"]) for row in rows)
    [call] = record["llm_calls"]
    assert (call["call_index"], call["purpose"]) == (1, "structured_output")
    assert call["input_tokens"] > 1000  # the system prompt alone is about 1,800 tokens
    assert call["output_tokens"] > 0
    assert 0 < call["latency_ms"] <= elapsed_ms
    assert (
        record["total_input_tokens"],
        record["total_output_tokens"],
        record["total_latency_ms"],
    ) == (call["input_tokens"], call["output_tokens"], call["latency_ms"])
    assert await streams.pending() == {}


@pytest.mark.vertex
async def start_no_listed_company(start_consumer, hkex_feed, streams, stub_sadi):
    """An article about a company with no HK listing verifies nothing and is still ACKed."""
    cleaned_id = uuid.uuid4()
    stub_sadi.serve(cleaned_id, UNLISTED_TITLE, UNLISTED_BODY)
    hkex_feed("real")

    with structlog.testing.capture_logs() as logs:
        async with start_consumer(**REAL_HKEX_ENV):
            message_id = await streams.add(cleaned_id)
            await _wait_acked(streams, stub_sadi, {cleaned_id: message_id}, VERTEX_WAIT_S)
            await asyncio.sleep(SETTLE_S)

    [record] = _completed(logs, cleaned_id)
    assert record["verified_entities"] == []
    assert [e for e in record["llm_errors"] if e["error_type"] in LLM_ERROR_TYPES] == []
    assert await streams.pending() == {}


@pytest.mark.vertex
@pytest.mark.parametrize(
    ("env", "body", "error_type", "log_level", "error_code"),
    [
        pytest.param(
            {"VERTEX_AI_PROJECT": INACCESSIBLE_VERTEX_PROJECT},
            "騰訊控股第四季廣告收入錄得強勁增長",
            "LLMAuthenticationError",
            "critical",
            error_codes.LLM_AUTHENTICATION_FAILED,
            id="authentication_failed",
        ),
        pytest.param(
            {"LLM_API_TIMEOUT_S": "1"},
            "騰訊控股公佈業績。" * 3400,  # cut to 30,000 characters, too slow for 1 s
            "LLMProviderError",
            "warning",
            error_codes.LLM_PROVIDER_ERROR,
            id="timeout",
        ),
        pytest.param(
            {"VERTEX_AI_LOCATION": "nowhere-1"},
            "騰訊控股第四季廣告收入錄得強勁增長",
            "LLMRequestError",
            "error",
            error_codes.LLM_REQUEST_REJECTED,
            id="unknown_location",
        ),
    ],
)
async def start_llm_failure_left_pending(
    start_consumer, streams, stub_sadi, env, body, error_type, log_level, error_code
):
    """A failed Vertex AI call is logged once, by the consumer, at its error's own level with
    the cleaned_id; the message stays pending and the next one is still handled."""
    failing, next_message = uuid.uuid4(), uuid.uuid4()
    stub_sadi.serve(failing, "騰訊控股公佈第四季業績", body)
    stub_sadi.respond(next_message, 404)  # ACKed without reaching Vertex AI

    with structlog.testing.capture_logs(processors=[logger.add_sapi_error_fields]) as logs:
        async with start_consumer(**env):
            failing_message_id = await streams.add(failing)
            await _wait_completed(logs, failing, VERTEX_WAIT_S)
            next_message_id = await streams.add(next_message)
            await _wait_acked(streams, stub_sadi, {next_message: next_message_id}, 5)

    llm_records = [log for log in logs if str(log.get("error_code", "")).startswith("COMMON-7")]
    assert [
        (log["log_level"], log["error_code"], log["category"], log["cleaned_id"])
        for log in llm_records
    ] == [(log_level, error_code, ErrorCategory.LLM, str(failing))]
    [record] = _completed(logs, failing)
    assert [e["error_type"] for e in record["llm_errors"]] == [error_type]
    assert record["verified_entities"] == []
    assert await streams.pending() == {failing_message_id: 1}


# Persisting and publishing EntityEvents. These cases replace Vertex AI with FakeLLMAdapter, so
# the outcome stored for each article is known in advance.

ARTICLE_TITLE = "騰訊及阿里巴巴公佈季度業績"
ARTICLE_BODY = "騰訊控股（00700）及阿里巴巴（09988）公佈季度業績。"  # noqa: RUF001
ARTICLE_PUBLISHED_AT = datetime(2025, 1, 15, tzinfo=UTC)  # what the SADI stub serves
TENCENT_ITEM = ExtractedEntity(
    stock_code="00700",
    company_name="騰訊控股",
    event_type_primary=EventTypeEnum.EARNINGS,
    event_type_secondary=EventTypeEnum.DIVIDEND,
    sentiment_label=SentimentEnum.POSITIVE,
    sentiment_score=0.8,
    headline="騰訊業績超預期",
    entity_summary="騰訊季度收入按年增長，並增加派息。",  # noqa: RUF001
)
ALIBABA_ITEM = ExtractedEntity(
    stock_code="09988",
    company_name="阿里巴巴",
    event_type_primary=EventTypeEnum.EARNINGS,
    event_type_secondary=None,
    sentiment_label=SentimentEnum.NEGATIVE,
    sentiment_score=0.3,
    headline="阿里巴巴盈利下跌",
    entity_summary="阿里巴巴季度盈利按年下跌。",
)
# (stock_code, exchange, company_name_zh, company_name_en) of each item's stock in the trimmed
# HK Stock List feed.
ITEM_STOCKS = {
    "00700": ("00700", "HKEX", "騰訊控股", "TENCENT"),
    "09988": ("09988", "HKEX", "阿里巴巴", "BABA-W"),
}
REPUBLISHED_EVENT = "entity_analysis_republished"


class FakeLLMAdapter(LLMAdapter):
    """Stands in for Vertex AI: answers every call with items, or raises error. While hold is
    set, a call waits for it before answering; entered is set once a call has started."""

    def __init__(self) -> None:
        self.items: list[ExtractedEntity] = []
        self.error: LLMError | None = None
        self.hold: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.calls = 0

    async def generate_structured(self, messages, output_schema, generation_config):
        self.calls += 1
        self.entered.set()
        if self.hold is not None:
            await self.hold.wait()
        if self.error is not None:
            raise self.error
        return StructuredLLMResponse(
            output=LLMOutput(output_items=self.items),
            input_tokens=1000,
            output_tokens=100,
            latency_ms=10,
        )

    async def close(self) -> None:
        pass


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLMAdapter:
    """Make the app's lifespan register a FakeLLMAdapter instead of the Vertex AI adapter."""
    fake = FakeLLMAdapter()

    def create_llm_adapter(config: object) -> FakeLLMAdapter:
        monkeypatch.setattr(vertex_adapter, "_llm_adapter", fake)
        return fake

    monkeypatch.setattr(main, "create_llm_adapter", create_llm_adapter)
    return fake


class EntityEventsProbe:
    """The test's own view of the entity_events table, on a fresh connection per query so it
    survives a Postgres restart."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    async def rows(self, source_url: str) -> list[asyncpg.Record]:
        """The rows stored for source_url, ordered by stock_code."""
        conn = await asyncpg.connect(self._database_url)
        try:
            return await conn.fetch(
                "SELECT * FROM entity_events WHERE source_url = $1 ORDER BY stock_code",
                source_url,
            )
        finally:
            await conn.close()

    async def truncate(self) -> None:
        conn = await asyncpg.connect(self._database_url)
        try:
            await conn.execute("TRUNCATE entity_events")
        finally:
            await conn.close()


@pytest.fixture
async def entity_events(infra: Infra) -> EntityEventsProbe:
    probe = EntityEventsProbe(infra.env()["DATABASE_URL"])
    await probe.truncate()
    return probe


def _source_url(cleaned_id: uuid.UUID) -> str:
    """The source_url the SADI stub serves for cleaned_id."""
    return f"https://finance.mingpao.com/{cleaned_id}"


def _assert_row(row: asyncpg.Record, cleaned_id: uuid.UUID, item: ExtractedEntity) -> None:
    """row is item's EntityEvent for cleaned_id's article, exactly as stored."""
    stock_code, exchange, company_name_zh, company_name_en = ITEM_STOCKS[item.stock_code or ""]
    assert isinstance(row["entity_event_id"], uuid.UUID)
    assert (row["exchange"], row["stock_code"]) == (exchange, stock_code)
    assert (row["source_url"], row["source_name"]) == (_source_url(cleaned_id), "MINGPAO")
    assert row["published_at"] == ARTICLE_PUBLISHED_AT
    assert (
        row["event_type_primary"],
        row["sentiment_label"],
        row["sentiment_score"],
        row["headline"],
        row["entity_summary"],
    ) == (
        item.event_type_primary,
        item.sentiment_label,
        item.sentiment_score,
        item.headline,
        item.entity_summary,
    )
    assert json.loads(row["additional_outcome"]) == {
        "version": "v1.0.0",
        "output": {
            "company_name_zh": company_name_zh,
            "company_name_en": company_name_en,
            "event_type_secondary": item.event_type_secondary,
            "headline": item.headline,
            "entity_summary": item.entity_summary,
        },
    }
    assert json.loads(row["metadata"]) == {"cleaned_id": str(cleaned_id)}
    assert row["created_at"] == row["updated_at"]


def _announcements(rows: list[asyncpg.Record]) -> list[dict[str, str]]:
    """The stream:entity_event_completed fields announcing each row, in row order."""
    return [{"entity_event_id": str(row["entity_event_id"]), "v": "1"} for row in rows]


def _by_id(fields: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(fields, key=lambda entry: entry["entity_event_id"])


def _sapi_errors(logs: list[MutableMapping[str, Any]], error_code: str) -> list[tuple]:
    """(log_level, category, cleaned_id) of each record logged for a SAPIError with
    error_code."""
    return [
        (log["log_level"], log["category"], log["cleaned_id"])
        for log in logs
        if log.get("error_code") == error_code
    ]


async def start_persist_and_publish(start_consumer, streams, stub_sadi, fake_llm, entity_events):
    """An article naming two listed companies is stored as one row per company, and each row
    is announced once, before the message is ACKed."""
    cleaned_id = uuid.uuid4()
    stub_sadi.serve(cleaned_id, ARTICLE_TITLE, ARTICLE_BODY)
    fake_llm.items = [TENCENT_ITEM, ALIBABA_ITEM]

    with structlog.testing.capture_logs() as logs:
        async with start_consumer():
            message_id = await streams.add(cleaned_id)
            await _wait_acked(streams, stub_sadi, {cleaned_id: message_id}, 10)
            published_when_acked = await streams.published()
            await asyncio.sleep(SETTLE_S)

    rows = await entity_events.rows(_source_url(cleaned_id))
    assert len(rows) == 2
    _assert_row(rows[0], cleaned_id, TENCENT_ITEM)
    _assert_row(rows[1], cleaned_id, ALIBABA_ITEM)
    assert _by_id(published_when_acked) == _by_id(_announcements(rows))
    assert await streams.published() == published_when_acked
    [record] = _completed(logs, cleaned_id)
    assert sorted(record["entity_event_ids"]) == sorted(str(row["entity_event_id"]) for row in rows)
    assert await streams.pending() == {}


async def start_failed_write_stores_nothing(
    start_consumer, streams, stub_sadi, fake_llm, entity_events
):
    """When one company's row can't be written, none of the article's rows is kept or
    announced and its message stays pending; the next article is handled normally."""
    failing, next_article = uuid.uuid4(), uuid.uuid4()
    stub_sadi.serve(failing, ARTICLE_TITLE, ARTICLE_BODY)
    stub_sadi.serve(next_article, ARTICLE_TITLE, ARTICLE_BODY)
    fake_llm.items = [TENCENT_ITEM, ALIBABA_ITEM]

    async with start_consumer():
        cache = hk_stock_list.get_hk_stock_list_cache()
        alibaba = cache.entries["09988"]
        # Longer than the exchange column's VARCHAR(10): the second insert fails.
        cache.entries["09988"] = dataclasses.replace(alibaba, exchange="HKEX-TOO-LONG")
        with structlog.testing.capture_logs(processors=[logger.add_sapi_error_fields]) as logs:
            failing_message_id = await streams.add(failing)

            async def failed() -> bool:
                return bool(_sapi_errors(logs, error_codes.DATABASE_ERROR))

            await _wait_for(failed, 10)
            cache.entries["09988"] = alibaba
            next_message_id = await streams.add(next_article)
            await _wait_acked(streams, stub_sadi, {next_article: next_message_id}, 10)

    assert await entity_events.rows(_source_url(failing)) == []
    next_rows = await entity_events.rows(_source_url(next_article))
    assert len(next_rows) == 2
    assert _by_id(await streams.published()) == _by_id(_announcements(next_rows))
    assert await streams.pending() == {failing_message_id: 1}
    assert _sapi_errors(logs, error_codes.DATABASE_ERROR) == [
        ("error", ErrorCategory.DB, str(failing))
    ]
    assert _completed(logs, failing) == []


async def start_redelivery_republished(start_consumer, streams, stub_sadi, fake_llm, entity_events):
    """A message for an article already stored announces the stored rows again without
    analysing the article again, and is ACKed."""
    cleaned_id = uuid.uuid4()
    stub_sadi.serve(cleaned_id, ARTICLE_TITLE, ARTICLE_BODY)
    fake_llm.items = [TENCENT_ITEM, ALIBABA_ITEM]

    async with start_consumer():
        first_message_id = await streams.add(cleaned_id)
        await _wait_acked(streams, stub_sadi, {cleaned_id: first_message_id}, 10)
        rows = await entity_events.rows(_source_url(cleaned_id))

        with structlog.testing.capture_logs() as logs:
            second_message_id = await streams.add(cleaned_id)

            async def republished() -> bool:
                return any(log["event"] == REPUBLISHED_EVENT for log in logs)

            await _wait_for(republished, 10)
            await _wait_acked(streams, stub_sadi, {cleaned_id: second_message_id}, 5)
            await asyncio.sleep(SETTLE_S)

    assert len(rows) == 2
    assert fake_llm.calls == 1
    assert await entity_events.rows(_source_url(cleaned_id)) == rows
    published = await streams.published()
    assert len(published) == 4
    assert _by_id(published[:2]) == _by_id(_announcements(rows))
    assert _by_id(published[2:]) == _by_id(_announcements(rows))
    [record] = [log for log in logs if log["event"] == REPUBLISHED_EVENT]
    assert record["log_level"] == "info"
    assert record["cleaned_id"] == str(cleaned_id)
    assert record["source_url"] == _source_url(cleaned_id)
    assert sorted(record["entity_event_ids"]) == sorted(str(row["entity_event_id"]) for row in rows)
    assert _completed(logs, cleaned_id) == []
    assert await streams.pending() == {}


@pytest.mark.parametrize(
    "outcome",
    [
        "no_entities",
        "not_found",
        LLMRateLimitError,
        LLMProviderError,
        LLMRequestError,
        LLMAuthenticationError,
        LLMSchemaViolationError,
    ],
    ids=lambda outcome: outcome if isinstance(outcome, str) else outcome.__name__,
)
async def start_nothing_stored(
    start_consumer, streams, stub_sadi, fake_llm, entity_events, outcome
):
    """An article with no listed company, one SADI doesn't have, or one whose analysis failed
    writes and announces nothing; only the first two are ACKed."""
    cleaned_id = uuid.uuid4()
    stub_sadi.serve(cleaned_id, ARTICLE_TITLE, ARTICLE_BODY)
    acked = outcome in ("no_entities", "not_found")
    if outcome == "not_found":
        stub_sadi.respond(cleaned_id, 404)
    elif isinstance(outcome, type):
        fake_llm.error = outcome("scripted failure", category=ErrorCategory.LLM)

    with structlog.testing.capture_logs() as logs:
        async with start_consumer():
            message_id = await streams.add(cleaned_id)
            if acked:
                await _wait_acked(streams, stub_sadi, {cleaned_id: message_id}, 5)
            else:
                await _wait_completed(logs, cleaned_id, 5)
            await asyncio.sleep(SETTLE_S)

    assert await entity_events.rows(_source_url(cleaned_id)) == []
    assert await streams.published() == []
    assert await streams.pending() == ({} if acked else {message_id: 1})
    records = _completed(logs, cleaned_id)
    if outcome == "not_found":
        assert records == []
    else:
        [record] = records
        assert record["entity_event_ids"] == []
        expected_errors = [] if acked else [outcome.__name__]
        assert [e["error_type"] for e in record["llm_errors"]] == expected_errors


async def start_postgres_down_at_write(
    start_consumer, streams, stub_sadi, fake_llm, entity_events, infra
):
    """With Postgres stopped by the time an article is written, the write fails once its
    retries run out, nothing is announced and the message stays pending; once Postgres is
    back, the same app instance stores the next article."""
    stopped, recovered = uuid.uuid4(), uuid.uuid4()
    stub_sadi.serve(stopped, ARTICLE_TITLE, ARTICLE_BODY)
    stub_sadi.serve(recovered, ARTICLE_TITLE, ARTICLE_BODY)
    fake_llm.items = [TENCENT_ITEM]
    fake_llm.hold = asyncio.Event()

    with structlog.testing.capture_logs(processors=[logger.add_sapi_error_fields]) as logs:
        async with start_consumer():
            stopped_message_id = await streams.add(stopped)
            await asyncio.wait_for(fake_llm.entered.wait(), 5)
            infra.postgres.stop()
            fake_llm.hold.set()

            async def failed() -> bool:
                return bool(_sapi_errors(logs, error_codes.SERVICE_UNAVAILABLE))

            await _wait_for(failed, 10)
            infra.postgres.restore()
            fake_llm.hold = None
            recovered_message_id = await streams.add(recovered)
            await _wait_acked(streams, stub_sadi, {recovered: recovered_message_id}, 15)

    assert await entity_events.rows(_source_url(stopped)) == []
    recovered_rows = await entity_events.rows(_source_url(recovered))
    assert len(recovered_rows) == 1
    assert await streams.published() == _announcements(recovered_rows)
    assert await streams.pending() == {stopped_message_id: 1}
    assert _sapi_errors(logs, error_codes.SERVICE_UNAVAILABLE) == [
        ("error", ErrorCategory.DB, str(stopped))
    ]
    [failure] = [log for log in logs if log.get("error_code") == error_codes.SERVICE_UNAVAILABLE]
    assert isinstance(failure["exc_info"], ServiceUnavailableError)


async def start_stream_redis_down_at_publish(
    start_consumer, streams, stub_sadi, fake_llm, entity_events, infra
):
    """With the stream Redis stopped once an article's rows are stored, publishing warns and
    retries; once Redis is back, every row is announced exactly once and the message is ACKed
    by the same app instance."""
    cleaned_id = uuid.uuid4()
    stub_sadi.serve(cleaned_id, ARTICLE_TITLE, ARTICLE_BODY)
    fake_llm.items = [TENCENT_ITEM, ALIBABA_ITEM]
    fake_llm.hold = asyncio.Event()

    async with start_consumer():
        message_id = await streams.add(cleaned_id)
        await asyncio.wait_for(fake_llm.entered.wait(), 5)
        await asyncio.sleep(1.5)  # the delivery reaches the append-only file within a second
        infra.redis_stream.stop()
        with structlog.testing.capture_logs() as logs:
            fake_llm.hold.set()
            await asyncio.sleep(3)
        rows_during_outage = await entity_events.rows(_source_url(cleaned_id))

        infra.redis_stream.restore()
        await streams.reconnect()

        async def acked() -> bool:
            return message_id not in await streams.pending()

        await _wait_for(acked, 10)

    assert len(rows_during_outage) == 2
    retries = [log for log in logs if log["event"] == "redis_stream_publish_retrying"]
    assert len(retries) >= 2
    assert {log["log_level"] for log in retries} == {"warning"}
    assert not [log for log in logs if log["log_level"] in ("error", "critical")]
    assert _by_id(await streams.published()) == _by_id(_announcements(rows_during_outage))
    assert await streams.pending() == {}


async def start_state_redis_down(
    start_consumer, streams, stub_sadi, fake_llm, entity_events, infra
):
    """Analysing, storing and announcing an article doesn't need the state Redis."""
    cleaned_id = uuid.uuid4()
    stub_sadi.serve(cleaned_id, ARTICLE_TITLE, ARTICLE_BODY)
    fake_llm.items = [TENCENT_ITEM]

    async with start_consumer():
        infra.redis_state.stop()
        message_id = await streams.add(cleaned_id)
        await _wait_acked(streams, stub_sadi, {cleaned_id: message_id}, 10)

    rows = await entity_events.rows(_source_url(cleaned_id))
    assert len(rows) == 1
    assert await streams.published() == _announcements(rows)
