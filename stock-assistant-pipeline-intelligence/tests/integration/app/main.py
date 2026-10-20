import asyncio
import io
import time

import openpyxl
import pytest
import redis
import structlog.testing

from app import logger, main
from app.common import error_codes
from app.common.error_categories import ErrorCategory
from tests.integration.conftest import REFUSED_URL, StubResponse

pytestmark = [pytest.mark.live, pytest.mark.timeout(120)]

REQUEST_TIMEOUT_S = 10

NOT_READY_BODY = {
    "status": "unhealthy",
    "database": "ok",
    "redis_stream": "ok",
    "redis_state": "ok",
    "hk_stock_list": "not_ready",
    "layers": {
        "entity_analysis": "error",
        "aggregation": "error",
        "scoring": "error",
        "cache": "error",
    },
}


def _no_equities_xlsx() -> bytes:
    """A List of Securities whose rows all parse, but none has Category "Equity"."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(("List of Securities",))
    sheet.append(("Updated as at 20/10/2026",))
    sheet.append(("Stock Code", "Name of Securities", "Category"))
    sheet.append(("00700", "TENCENT", "Equities"))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("component", "error"),
    [
        pytest.param("database", OSError, id="database"),
        pytest.param("redis_stream", redis.ConnectionError, id="redis_stream"),
        pytest.param("redis_state", redis.ConnectionError, id="redis_state"),
    ],
)
async def lifespan_dependency_down(infra, hkex_urls, stub_hkex, component, error):
    """DB/Redis connects are mandatory and come before the HK Stock List load."""
    hkex = stub_hkex(StubResponse(200, b""))
    hkex_urls(every=hkex.url)
    infra.for_component(component).stop()

    with pytest.raises(error):
        async with main.app.router.lifespan_context(main.app):
            pytest.fail("startup should not complete")

    assert hkex.requests == 0


@pytest.mark.parametrize(
    ("trigger", "expected_error_code"),
    [
        pytest.param("refused", error_codes.UPSTREAM_UNAVAILABLE, id="connection_refused"),
        pytest.param("http_500", error_codes.UPSTREAM_UNAVAILABLE, id="http_500"),
        pytest.param("json_wrong_shape", error_codes.PARSE_ERROR, id="json_wrong_shape"),
        pytest.param("securities_not_xlsx", error_codes.PARSE_ERROR, id="securities_not_xlsx"),
        pytest.param("no_equities", error_codes.PARSE_ERROR, id="no_equities"),
    ],
)
async def lifespan_hk_stock_list_load_failed(
    start_app, hkex_urls, stub_hkex, trigger, expected_error_code
):
    """A failed HK Stock List load never aborts startup; GET /v1/health reports not_ready.
    The two securities cases keep the real JSON files, so only the spreadsheet is wrong."""
    if trigger == "refused":
        hkex_urls(every=REFUSED_URL)
    elif trigger == "http_500":
        hkex_urls(every=stub_hkex(StubResponse(500, b"")).url)
    elif trigger == "json_wrong_shape":
        hkex_urls(every=stub_hkex(StubResponse(200, b'{"unexpected": "shape"}')).url)
    elif trigger == "securities_not_xlsx":
        stub = stub_hkex(StubResponse(200, b"not a spreadsheet"))
        hkex_urls(_LIST_OF_SECURITIES_URL=stub.url)
    else:
        stub = stub_hkex(StubResponse(200, _no_equities_xlsx()))
        hkex_urls(_LIST_OF_SECURITIES_URL=stub.url)

    with structlog.testing.capture_logs(processors=[logger.add_sapi_error_fields]) as logs:
        async with start_app() as client:
            response = await asyncio.wait_for(client.get("/v1/health"), REQUEST_TIMEOUT_S)

    failures = [log for log in logs if log["event"] == "hk_stock_list_startup_load_failed"]
    assert len(failures) == 1
    assert failures[0]["log_level"] == "critical"
    assert failures[0]["category"] == ErrorCategory.STOCK_LIST
    assert failures[0]["error_code"] == expected_error_code
    assert response.status_code == 503
    assert response.json() == NOT_READY_BODY


async def lifespan_hk_stock_list_timeout_from_config(start_app, hkex_urls, black_hole_url):
    """HKEX accepting connections but never answering: the load gives up after HTTP_TIMEOUT_S
    and startup continues."""
    hkex_urls(every=black_hole_url)

    with structlog.testing.capture_logs(processors=[logger.add_sapi_error_fields]) as logs:
        started = time.monotonic()
        async with start_app(HTTP_TIMEOUT_S="1") as client:
            startup_s = time.monotonic() - started
            response = await asyncio.wait_for(client.get("/v1/health"), REQUEST_TIMEOUT_S)

    failures = [log for log in logs if log["event"] == "hk_stock_list_startup_load_failed"]
    assert len(failures) == 1
    assert failures[0]["error_code"] == error_codes.UPSTREAM_UNAVAILABLE
    assert 1 <= startup_s < 2
    assert response.status_code == 503
    assert response.json() == NOT_READY_BODY


async def lifespan_shutdown_db_unresponsive(infra, monkeypatch):
    """Closing the pool against a paused Postgres gives up after DB_CLOSE_TIMEOUT_S."""
    monkeypatch.setenv("DB_CLOSE_TIMEOUT_S", "1")

    async with main.app.router.lifespan_context(main.app):
        infra.postgres.pause()
        started = time.monotonic()

    assert 1 <= time.monotonic() - started < 2
