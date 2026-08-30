"""
Error code catalog — single source of truth for all SADI error codes.

Codes follow the unified MWP error format defined in docs/api.md §1.2:
    {SERVICE_PREFIX}-{CODE}

COMMON-4xxx / COMMON-5xxx = shared HTTP error responses (all services).
SADI-61xx = NETWORK (crawler fetch failures).
SADI-62xx = PARSE (crawler content extraction failures).
"""
import logging
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorCode:
    """Base error code record. One frozen instance per distinct code."""

    error_type: str    # Category: "NETWORK" | "PARSE"
    error_code: str    # Unique identifier, e.g. "SADI-6101"
    dev_message: str   # Static technical description, for debugging. Used server-side
                        # in logs, and as the API response's `detail` fallback when the
                        # raised exception has no more specific per-occurrence detail
                        # (see app.common.exceptions.SADIException.detail) — safe to
                        # return because SADI has no untrusted external callers.
    message: str       # Short, generic, public-facing summary — safe for a caller to
                        # relay further (e.g. to its own end-user-facing UI)
    log_level: int = logging.WARNING
                        # Declared severity for this error code — the single source of
                        # truth for how loggers should treat it, instead of every call
                        # site re-deciding ad hoc (which drifts: the same exception type
                        # was logged at different severities in different crawlers
                        # before this field existed). Default WARNING fits most codes —
                        # an individual failure (one URL, one PDF) at fleet scale isn't
                        # ops-actionable on its own. Override to logging.ERROR only for
                        # codes that are: genuinely unexpected (INTERNAL_ERROR) or
                        # represent a dependency being down (SERVICE_UNAVAILABLE).
                        # Callers log with `logger.log(error_code.log_level, ...)`.


class CommonErrorCode:
    """COMMON-4xxx / COMMON-5xxx — shared HTTP error responses."""

    MALFORMED_REQUEST = ErrorCode(
        error_type="COMMON",
        error_code="COMMON-4000",
        dev_message="Malformed request — unparseable JSON or missing Content-Type",
        message="Malformed request",
    )
    VALIDATION_FAILED = ErrorCode(
        error_type="COMMON",
        error_code="COMMON-4001",
        dev_message="Request validation failed — one or more fields failed validation",
        message="Request validation failed",
    )
    NOT_FOUND = ErrorCode(
        error_type="COMMON",
        error_code="COMMON-4004",
        dev_message="Requested resource does not exist",
        message="Resource not found",
    )
    INTERNAL_ERROR = ErrorCode(
        error_type="COMMON",
        error_code="COMMON-5000",
        dev_message="Unexpected internal server error",
        message="Internal server error",
        log_level=logging.ERROR,
    )
    SERVICE_UNAVAILABLE = ErrorCode(
        error_type="COMMON",
        error_code="COMMON-5001",
        dev_message="Dependency unreachable — database or Redis",
        message="Service unavailable",
        log_level=logging.ERROR,
    )
    RATE_LIMITED = ErrorCode(
        error_type="COMMON",
        error_code="COMMON-4029",
        dev_message="Rate limited — too many requests to upstream source",
        message="Rate limited",
    )


class CrawlErrorCode:
    """SADI-61xx — crawler errors (fetch itself failed)."""

    URL_GET_FAILED = ErrorCode(
        error_type="CRAWL",
        error_code="SADI-6101",
        dev_message="Failed to access the page — HTTP error, timeout, DNS, connection reset, or TLS error",
        message="Failed to fetch article",
    )
    BROWSER_FETCH_FAILED = ErrorCode(
        error_type="CRAWL",
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
