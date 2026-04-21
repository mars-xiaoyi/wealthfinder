from app.common.error_codes import CrawlErrorCode, CommonErrorCode
from app.common.exceptions import SADIException


class CrawlBlockedException(SADIException):
    """Page fetch failed — HTTP error, network failure, or all retries exhausted."""
    error_code = CrawlErrorCode.URL_GET_FAILED


class CrawlRateLimitedException(SADIException):
    """HTTP 429 — transient, do not persist to crawl_error_log."""
    error_code = CommonErrorCode.RATE_LIMITED


class CrawlFatalException(SADIException):
    """
    Unrecoverable source-level failure (e.g. RSS unreachable after all retries,
    HKEX search page playwright load failure).
    CrawlService catches this and publishes crawl_completed FAILED.
    """
    error_code = CommonErrorCode.INTERNAL_ERROR
