class CrawlSkippedError(Exception):
    """URL is already in crawl_error_log — caller raises this after its own pre-check."""


class CrawlNetworkError(Exception):
    """All retries exhausted — caller adds to result.failures."""


class CrawlBlockedError(Exception):
    """HTTP 403 or 404 — immediate failure, caller adds to result.failures."""


class CrawlFatalError(Exception):
    """
    Unrecoverable source-level failure (e.g. RSS unreachable after all retries,
    HKEX search page playwright load failure).
    CrawlService catches this and publishes crawl_completed FAILED.
    """
