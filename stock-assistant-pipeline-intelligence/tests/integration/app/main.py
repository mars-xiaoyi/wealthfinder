import asyncio

import pytest
import structlog.testing

from app import logger
from app.common import error_codes
from app.common.error_categories import ErrorCategory
from tests.integration.conftest import REFUSED_URL

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


async def lifespan_hk_stock_list_load_failed(start_app, hkex_urls):
    """With HKEX unreachable, startup still completes and GET /v1/health reports not_ready."""
    hkex_urls(every=REFUSED_URL)

    with structlog.testing.capture_logs(processors=[logger.add_sapi_error_fields]) as logs:
        async with start_app() as client:
            response = await asyncio.wait_for(client.get("/v1/health"), REQUEST_TIMEOUT_S)

    failures = [log for log in logs if log["event"] == "hk_stock_list_startup_load_failed"]
    assert len(failures) == 1
    assert failures[0]["log_level"] == "critical"
    assert failures[0]["category"] == ErrorCategory.STOCK_LIST
    assert failures[0]["error_code"] == error_codes.UPSTREAM_UNAVAILABLE
    assert response.status_code == 503
    assert response.json() == NOT_READY_BODY
