"""
Crawler error code catalog — single source of truth for SADI internal
crawl errors recorded in crawl_error_log and stream:crawl_completed.

Codes follow the unified MWP error format defined in docs/api.md §1.2:
    {SERVICE_PREFIX}-{CODE}

The 6xxx range is reserved for internal classification codes (not exposed
via HTTP). Convention: SADI-61xx = NETWORK, SADI-62xx = PARSE.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorCode:
    """Base error code record. One frozen instance per distinct code."""

    error_type: str    # Category: "NETWORK" | "PARSE"
    error_code: str    # Unique identifier, e.g. "SADI-6101"
    dev_message: str   # Technical description for devs/ops (logs, debugging)
    message: str       # Short human-readable summary (dashboards, ops alerts)


class NetworkErrorCode:
    """SADI-61xx — crawler NETWORK errors (fetch itself failed)."""

    HTTP_403 = ErrorCode(
        error_type="NETWORK",
        error_code="SADI-6101",
        dev_message="Source returned HTTP 403 Forbidden during article fetch",
        message="Source blocked the request (HTTP 403)",
    )
    HTTP_404 = ErrorCode(
        error_type="NETWORK",
        error_code="SADI-6102",
        dev_message="Source returned HTTP 404 Not Found during article fetch",
        message="Article URL not found (HTTP 404)",
    )
    NETWORK_ERROR = ErrorCode(
        error_type="NETWORK",
        error_code="SADI-6103",
        dev_message="Network-level failure: timeout, DNS, connection reset, or TLS error",
        message="Network failure fetching article",
    )
    BROWSER_FETCH_FAILED = ErrorCode(
        error_type="NETWORK",
        error_code="SADI-6104",
        dev_message="Playwright browser fetch failed (navigation timeout or context crash)",
        message="Browser fetch failed",
    )


class DocumentParseErrorCode:
    """SADI-62xx — crawler PARSE errors (fetch succeeded but content unusable)."""

    PARSE_ERROR = ErrorCode(
        error_type="PARSE",
        error_code="SADI-6201",
        dev_message="HTML/text content extraction raised an exception",
        message="Content extraction failed",
    )
    PDF_ENCRYPTED = ErrorCode(
        error_type="PARSE",
        error_code="SADI-6202",
        dev_message="PDF document is encrypted/password-protected and cannot be read",
        message="PDF is encrypted",
    )
    PDF_PARSE_ERROR = ErrorCode(
        error_type="PARSE",
        error_code="SADI-6203",
        dev_message="PDF content extraction raised an exception",
        message="PDF parse failed",
    )
