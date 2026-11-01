import asyncio

import httpx
import pytest
from httpx import AsyncClient

from app.api.schemas import HKStockListSyncResponse
from app.common import hk_stock_list
from tests.integration.conftest import HKEX_URL_CONSTANTS, REFUSED_URL

pytestmark = [pytest.mark.live, pytest.mark.timeout(180)]

SYNC_PATH = "/v1/admin/hk-stock-list-sync"
# A real HKEX sync downloads three files; give it room.
SYNC_TIMEOUT_S = 60
REQUEST_TIMEOUT_S = 10
REAL_HKEX_URLS = {name: getattr(hk_stock_list, name) for name in HKEX_URL_CONSTANTS}
# HKEX lists roughly 2,600 equities; anything far outside means a broken parse.
MIN_ENTRIES, MAX_ENTRIES = 1000, 5000

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


async def post_hk_stock_list_sync_success(start_app):
    """The real lists load, without RMB counters (80700 騰訊控股－Ｒ) or trading-only lines
    (－Ｔ / -T): HKEX marks them the way the parser expects."""
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
    extra_counters = [
        code
        for code, stock in cache.entries.items()
        if (code.startswith("8") and stock.company_name_zh.endswith("Ｒ"))
        or stock.company_name_zh.endswith("Ｔ")
        or stock.company_name_en.endswith("-T")
    ]
    assert extra_counters == []


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
