class CrawlBlockedException(Exception):
    """Page fetch failed — HTTP error, network failure, or all retries exhausted."""


class CrawlRateLimitedException(Exception):
    """HTTP 429 — transient, do not persist to crawl_error_log."""


class CrawlFatalException(Exception):
    """
    Unrecoverable source-level failure (e.g. RSS unreachable after all retries,
    HKEX search page playwright load failure).
    CrawlService catches this and publishes crawl_completed FAILED.
    """
