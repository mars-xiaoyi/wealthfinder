from unittest.mock import AsyncMock

import httpx

from app.api import main as api_main
from app.api.routes import hk_stock_list_sync
from app.api.schemas import ErrorResponse, HKStockListSyncResponse
from app.common import error_codes
from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError


async def post_hk_stock_list_sync_success(monkeypatch):
    """entries_loaded is refresh_hk_stock_list()'s own return value."""
    refresh = AsyncMock(return_value=2431)
    monkeypatch.setattr(hk_stock_list_sync, "refresh_hk_stock_list", refresh)

    body = await hk_stock_list_sync.post_hk_stock_list_sync()

    assert body == HKStockListSyncResponse(status="success", entries_loaded=2431)
    refresh.assert_awaited_once_with()


async def post_hk_stock_list_sync_error(monkeypatch):
    """A failed refresh reaches the caller as an ErrorResponse with its HTTP status. Sent through
    the app, since the mapping happens in its registered error handler (whose full mapping is
    tested in tests/app/api/error_handlers.py)."""
    error = ServiceUnavailableError.upstream_unavailable(
        "HKEXnews unreachable", category=ErrorCategory.STOCK_LIST
    )
    monkeypatch.setattr(hk_stock_list_sync, "refresh_hk_stock_list", AsyncMock(side_effect=error))
    transport = httpx.ASGITransport(app=api_main.create_app())

    async with httpx.AsyncClient(transport=transport, base_url="http://sapi") as client:
        response = await client.post("/v1/admin/hk-stock-list-sync")

    assert response.status_code == 503
    body = ErrorResponse.model_validate(response.json())
    assert body.error_code == error_codes.UPSTREAM_UNAVAILABLE
    assert body.detail == {}
