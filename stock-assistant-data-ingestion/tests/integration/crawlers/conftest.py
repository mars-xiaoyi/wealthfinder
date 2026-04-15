"""Shared fixtures for live crawler integration tests (opt-in via `-m live`)."""

from datetime import datetime
from typing import Any, Optional

import httpx
import pytest
import pytest_asyncio

from app.config import CrawlConfig, CrawlSourceConfig
from app.crawl.fetchers.page_crawler import PageCrawler


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)


class StubDB:
    """
    Minimal DB stub — crawlers only call fetch_one for two queries:
      - crawl_error_log existence pre-check
      - YahooHKCrawler's MAX(created_at) coverage-gap lookup
    Integration tests target the fetch/parse path, not DB plumbing.
    """

    async def fetch_one(self, sql: str, *args: Any) -> Optional[dict]:
        if "crawl_error_log" in sql:
            return {"exists": False}
        if "MAX(created_at)" in sql:
            return {"max_created_at": None}
        return None


def _make_source_config() -> CrawlSourceConfig:
    return CrawlSourceConfig(
        max_concurrent=5,
        request_interval_min_ms=500,
        request_interval_max_ms=1000,
    )


def _make_crawl_config() -> CrawlConfig:
    return CrawlConfig(
        max_retry=3,
        retry_base_wait_ms=500,
        request_timeout_s=20,
        browser_navigation_timeout_ms=15000,
        crawl_sources={},
    )


@pytest.fixture
def stub_db() -> StubDB:
    return StubDB()


@pytest.fixture
def source_config() -> CrawlSourceConfig:
    return _make_source_config()


@pytest.fixture
def crawl_config() -> CrawlConfig:
    return _make_crawl_config()


@pytest_asyncio.fixture
async def http_client():
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def page_crawler(http_client, crawl_config) -> PageCrawler:
    return PageCrawler(client=http_client, config=crawl_config)


def assert_success_shape(item, *, host_contains: str) -> None:
    """Loose assertions that resist natural source churn."""
    assert item.title and item.title.strip(), "empty title"
    assert item.body and len(item.body) >= 100, f"body too short: {len(item.body)} chars"
    assert host_contains in item.source_url, f"unexpected host in {item.source_url!r}"
    assert item.published_at is None or isinstance(item.published_at, datetime)


def summarize(label: str, result) -> None:
    print(
        f"\n[{label}] successes={len(result.successes)} failures={len(result.failures)}"
    )
    if result.successes:
        first = result.successes[0]
        body_preview = first.body[:80].replace("\n", " ")
        print(
            f"[{label}] first: {first.title[:60]!r} "
            f"@ {first.published_at} — {body_preview!r}..."
        )
    for f in result.failures[:3]:
        print(f"[{label}] failure: {f.error_code} {f.source_url}")
