import json

import pytest
from fastapi import Request

from app.api import error_handlers
from app.common import error_codes
from app.common.error_categories import ErrorCategory
from app.common.errors import ParseError, ServiceUnavailableError
from app.db.errors import DatabaseError


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/v1/any", "headers": []})


@pytest.mark.parametrize(
    ("exc", "status_code", "error_code"),
    [
        pytest.param(
            ServiceUnavailableError.upstream_unavailable(
                "HKEXnews unreachable", category=ErrorCategory.STOCK_LIST
            ),
            503,
            error_codes.UPSTREAM_UNAVAILABLE,
            id="upstream_unavailable",
        ),
        pytest.param(
            ServiceUnavailableError("http client not initialized", category=ErrorCategory.COMMON),
            503,
            error_codes.SERVICE_UNAVAILABLE,
            id="service_unavailable",
        ),
        pytest.param(
            ParseError("HK Stock List response shape mismatch", category=ErrorCategory.STOCK_LIST),
            500,
            error_codes.PARSE_ERROR,
            id="parse_error",
        ),
        pytest.param(
            DatabaseError("syntax error", category=ErrorCategory.DB),
            500,
            error_codes.DATABASE_ERROR,
            id="not_in_http_status",
        ),
    ],
)
async def sapi_error_handler_maps_error(exc, status_code, error_code):
    """Each error becomes an ErrorResponse with its HTTP_STATUS status (500 if it has none),
    its own error_code, and an empty detail: dev_message never leaves SAPI."""
    response = await error_handlers.sapi_error_handler(_request(), exc)

    assert response.status_code == status_code
    body = json.loads(bytes(response.body))
    assert body["error_code"] == error_code
    assert body["detail"] == {}


async def unhandled_error_handler_internal_error():
    response = await error_handlers.unhandled_error_handler(_request(), RuntimeError("boom"))

    assert response.status_code == 500
    body = json.loads(bytes(response.body))
    assert body["error_code"] == error_codes.INTERNAL_ERROR
    assert body["detail"] == {}
