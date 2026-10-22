import asyncio
import time
from collections.abc import MutableMapping
from typing import Any

import httpx
import pytest
import structlog.testing
from httpx import AsyncClient

from app import logger
from app.api.schemas import ErrorResponse, HKStockListSyncResponse
from app.common import error_codes, hk_stock_list, http_client
from tests.integration.conftest import HKEX_URL_CONSTANTS, REFUSED_URL, StubResponse

pytestmark = [pytest.mark.live, pytest.mark.timeout(180)]

SYNC_PATH = "/v1/admin/hk-stock-list-sync"
# A real HKEX sync downloads three files; give it room.
SYNC_TIMEOUT_S = 60
REQUEST_TIMEOUT_S = 10
REAL_HKEX_URLS = {name: getattr(hk_stock_list, name) for name in HKEX_URL_CONSTANTS}
# HKEX lists roughly 2,600 equities; anything far outside means a broken parse.
MIN_ENTRIES, MAX_ENTRIES = 1000, 5000
WRONG_SHAPE_JSON = b'{"unexpected": "shape"}'

READY_BODY = {
    "status": "degraded",
    "database": "ok",
    "redis_stream": "ok",
    "redis_state": "ok",
    "hk_stock_list": "ok",
    "layers": {
        "entity_analysis": "error",
        "aggregation": "error",
        "scoring": "error",
        "cache": "error",
    },
}
NOT_READY_BODY = {**READY_BODY, "status": "unhealthy", "hk_stock_list": "not_ready"}


async def _sync(client: AsyncClient) -> httpx.Response:
    return await asyncio.wait_for(client.post(SYNC_PATH), SYNC_TIMEOUT_S)


async def _get_health(client: AsyncClient) -> httpx.Response:
    return await asyncio.wait_for(client.get("/v1/health"), REQUEST_TIMEOUT_S)


def _restore_real_hkex(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, url in REAL_HKEX_URLS.items():
        monkeypatch.setattr(hk_stock_list, name, url)


def _assert_error(response: httpx.Response, status_code: int, error_code: str) -> None:
    assert response.status_code == status_code
    body = ErrorResponse.model_validate(response.json())
    assert body.error_code == error_code
    assert body.message
    assert body.detail == {}


def _error_logs(logs: list[MutableMapping[str, Any]]) -> list[tuple[str, str]]:
    """(log_level, error_code) of each SAPIError the error handler logged."""
    return [(log["log_level"], log["error_code"]) for log in logs if "error_code" in log]


async def post_hk_stock_list_sync_success(start_app):
    async with start_app() as client:
        cache = hk_stock_list.get_hk_stock_list_cache()
        old = cache.entries

        response = await _sync(client)

    assert response.status_code == 200
    body = HKStockListSyncResponse.model_validate(response.json())
    assert body.status == "success"
    assert body.entries_loaded == len(cache.entries)
    assert MIN_ENTRIES <= body.entries_loaded <= MAX_ENTRIES
    assert hk_stock_list.get_hk_stock_list_cache() is cache
    assert cache.entries is not old


async def post_hk_stock_list_sync_after_failed_startup(start_app, hkex_urls, monkeypatch):
    """A sync recovers a failed startup load without restarting the app."""
    hkex_urls(every=REFUSED_URL)

    async with start_app() as client:
        health = await _get_health(client)
        assert health.status_code == 503
        assert health.json() == NOT_READY_BODY

        _restore_real_hkex(monkeypatch)
        response = await _sync(client)
        health = await _get_health(client)

    assert response.status_code == 200
    assert HKStockListSyncResponse.model_validate(response.json()).entries_loaded > 0
    assert health.status_code == 200
    assert health.json() == READY_BODY


@pytest.mark.parametrize("trigger", ["refused", "http_500", "timeout"])
async def post_hk_stock_list_sync_upstream_unavailable(
    start_app, hkex_urls, stub_hkex, black_hole_url, monkeypatch, trigger
):
    """HKEX refusing, erroring or never answering: 503 COMMON-5002, logged at WARNING, and the
    loaded list stays in place. The timeout case lowers the shared client's timeout after the
    real startup load, which needs the default."""
    async with start_app() as client:
        cache = hk_stock_list.get_hk_stock_list_cache()
        old, n = cache.entries, len(cache)
        if trigger == "refused":
            hkex_urls(every=REFUSED_URL)
        elif trigger == "http_500":
            hkex_urls(every=stub_hkex(StubResponse(500, b"")).url)
        else:
            monkeypatch.setattr(http_client.get_http_client(), "timeout", httpx.Timeout(1))
            hkex_urls(every=black_hole_url)

        with structlog.testing.capture_logs(processors=[logger.add_sapi_error_fields]) as logs:
            started = time.monotonic()
            response = await _sync(client)
            elapsed = time.monotonic() - started
        health = await _get_health(client)

    _assert_error(response, 503, error_codes.UPSTREAM_UNAVAILABLE)
    assert _error_logs(logs) == [("warning", error_codes.UPSTREAM_UNAVAILABLE)]
    if trigger == "timeout":
        assert 1 <= elapsed < 2
    assert cache.entries is old
    assert len(cache) == n
    assert health.json() == READY_BODY


@pytest.mark.parametrize("trigger", ["json_wrong_shape", "securities_not_xlsx"])
async def post_hk_stock_list_sync_unparseable(start_app, hkex_urls, stub_hkex, trigger):
    """HKEX answering with the wrong content: 500 COMMON-6000, logged at ERROR, and the loaded
    list stays in place. securities_not_xlsx keeps the real JSON files."""
    async with start_app() as client:
        cache = hk_stock_list.get_hk_stock_list_cache()
        old, n = cache.entries, len(cache)
        if trigger == "json_wrong_shape":
            stub = stub_hkex(StubResponse(200, WRONG_SHAPE_JSON))
            hkex_urls(_ACTIVESTOCK_EN_URL=stub.url, _ACTIVESTOCK_ZH_URL=stub.url)
        else:
            stub = stub_hkex(StubResponse(200, b"not a spreadsheet"))
            hkex_urls(_LIST_OF_SECURITIES_URL=stub.url)

        with structlog.testing.capture_logs(processors=[logger.add_sapi_error_fields]) as logs:
            response = await _sync(client)

    _assert_error(response, 500, error_codes.PARSE_ERROR)
    assert _error_logs(logs) == [("error", error_codes.PARSE_ERROR)]
    assert cache.entries is old
    assert len(cache) == n


async def post_hk_stock_list_sync_failure_while_empty(start_app, hkex_urls, stub_hkex):
    """With the startup load failed, a failed sync keeps the same empty list and not_ready."""
    hkex_urls(every=REFUSED_URL)

    async with start_app() as client:
        cache = hk_stock_list.get_hk_stock_list_cache()

        refused = await _sync(client)
        health_after_refused = await _get_health(client)
        hkex_urls(every=stub_hkex(StubResponse(200, WRONG_SHAPE_JSON)).url)
        unparseable = await _sync(client)
        health_after_unparseable = await _get_health(client)

    _assert_error(refused, 503, error_codes.UPSTREAM_UNAVAILABLE)
    _assert_error(unparseable, 500, error_codes.PARSE_ERROR)
    assert hk_stock_list.get_hk_stock_list_cache() is cache
    assert len(cache) == 0
    for health in (health_after_refused, health_after_unparseable):
        assert health.status_code == 503
        assert health.json() == NOT_READY_BODY


async def post_hk_stock_list_sync_repeated(start_app):
    """Each sync replaces the list rather than adding to it."""
    async with start_app() as client:
        cache = hk_stock_list.get_hk_stock_list_cache()
        loaded = []
        for _ in range(2):
            before = cache.entries
            response = await _sync(client)
            assert response.status_code == 200
            entries_loaded = HKStockListSyncResponse.model_validate(response.json()).entries_loaded
            assert entries_loaded == len(cache.entries)
            assert cache.entries is not before
            assert hk_stock_list.get_hk_stock_list_cache() is cache
            loaded.append(entries_loaded)

    first, second = loaded
    assert abs(first - second) < first * 0.01


async def post_hk_stock_list_sync_readers_never_see_empty(start_app):
    """GET /v1/health polled every 50 ms during a sync never sees the list missing."""
    async with start_app() as client:
        sync = asyncio.create_task(_sync(client))
        polls = []
        while not sync.done():
            polls.append(await _get_health(client))
            await asyncio.sleep(0.05)
        response = await sync

    assert response.status_code == 200
    assert polls
    for poll in polls:
        assert poll.status_code == 200
        assert poll.json() == READY_BODY
