from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import CrawlSourceConfig
from app.crawler.base_crawler import CrawlResult
from app.crawler.exceptions import CrawlFatalError
from app.crawler.feed_fetcher import FeedEntry, FeedFetchError
from app.crawler.mingpao_crawler import MingPaoCrawler


def make_source_config() -> CrawlSourceConfig:
    return CrawlSourceConfig(
        max_concurrent=3,
        request_interval_min_ms=0,
        request_interval_max_ms=0,
    )


def make_db(in_error_log: bool = False) -> MagicMock:
    db = MagicMock()
    db.fetch_one = AsyncMock(return_value={"exists": in_error_log})
    return db


def make_crawler(db=None) -> MingPaoCrawler:
    return MingPaoCrawler(
        source_config=make_source_config(),
        page_crawler=MagicMock(),
        db=db or make_db(),
    )


def make_entry(url: str = "https://news.mingpao.com/article/1", published_at=None):
    return FeedEntry(
        title="標題",
        url=url,
        published_at=published_at,
    )


def _make_fake_context(html: str | Exception = "<html><article>body text</article></html>"):
    page = MagicMock()
    if isinstance(html, Exception):
        page.goto = AsyncMock(side_effect=html)
    else:
        page.goto = AsyncMock()
        page.content = AsyncMock(return_value=html)
    page.close = AsyncMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    return context, page


# ---------------------------------------------------------------------------
# _extract_published_at_from_page
# ---------------------------------------------------------------------------

class TestExtractPublishedAtFromPage:
    def test_parses_morning_time(self):
        html = "<time>2026年4月6日 星期一 06:00AM</time>"
        result = MingPaoCrawler._extract_published_at_from_page(html)
        # 06:00 HKT = 22:00 previous day UTC
        assert result == datetime(2026, 4, 5, 22, 0, tzinfo=timezone.utc)

    def test_parses_evening_time(self):
        html = "<time>2026年4月6日 星期一 06:00PM</time>"
        result = MingPaoCrawler._extract_published_at_from_page(html)
        # 18:00 HKT = 10:00 UTC same day
        assert result == datetime(2026, 4, 6, 10, 0, tzinfo=timezone.utc)

    def test_no_match_returns_none(self):
        assert MingPaoCrawler._extract_published_at_from_page("<html></html>") is None


# ---------------------------------------------------------------------------
# _fetch_one_article
# ---------------------------------------------------------------------------

class TestFetchOneArticle:
    @pytest.mark.asyncio
    async def test_success_with_rss_published_at(self):
        crawler = make_crawler()
        context, _ = _make_fake_context()
        result = CrawlResult()
        rss_published = datetime(2026, 4, 6, 1, 0, tzinfo=timezone.utc)

        await crawler._fetch_one_article(
            context, make_entry(published_at=rss_published), result
        )

        assert len(result.successes) == 1
        s = result.successes[0]
        assert s.body == "body text"
        assert s.published_at == rss_published

    @pytest.mark.asyncio
    async def test_success_falls_back_to_page_time(self):
        crawler = make_crawler()
        html = (
            "<html><article>some body content</article>"
            "<time>2026年4月6日 06:00PM</time></html>"
        )
        context, _ = _make_fake_context(html)
        result = CrawlResult()

        await crawler._fetch_one_article(
            context, make_entry(published_at=None), result
        )

        s = result.successes[0]
        assert s.published_at == datetime(2026, 4, 6, 10, 0, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_browser_failure_creates_fail_item(self):
        crawler = make_crawler()
        context, _ = _make_fake_context(html=RuntimeError("browser crashed"))
        result = CrawlResult()

        await crawler._fetch_one_article(context, make_entry(), result)

        assert len(result.failures) == 1
        assert result.failures[0].error_code == "BROWSER_FETCH_FAILED"
        assert result.failures[0].error_type == "NETWORK"

    @pytest.mark.asyncio
    async def test_empty_body_creates_fail_item(self):
        crawler = make_crawler()
        context, _ = _make_fake_context(html="<html><article></article></html>")
        result = CrawlResult()

        await crawler._fetch_one_article(context, make_entry(), result)

        assert result.failures[0].error_code == "EMPTY_BODY"
        assert result.failures[0].error_type == "PARSE"

    @pytest.mark.asyncio
    async def test_parse_exception_creates_fail_item(self):
        crawler = make_crawler()
        context, _ = _make_fake_context()
        result = CrawlResult()

        with patch(
            "app.crawler.mingpao_crawler.extract_body_css",
            side_effect=RuntimeError("lxml exploded"),
        ):
            await crawler._fetch_one_article(context, make_entry(), result)

        assert result.failures[0].error_code == "PARSE_ERROR"
        assert result.failures[0].error_type == "PARSE"


# ---------------------------------------------------------------------------
# _crawl_articles — error log skip
# ---------------------------------------------------------------------------

class TestCrawlArticlesSkip:
    @pytest.mark.asyncio
    async def test_skips_url_in_error_log(self):
        crawler = make_crawler(db=make_db(in_error_log=True))
        context, page = _make_fake_context()
        result = CrawlResult()

        await crawler._crawl_articles(context, [make_entry()], result)

        context.new_page.assert_not_called()
        assert result.successes == []
        assert result.failures == []


# ---------------------------------------------------------------------------
# run() — top level
# ---------------------------------------------------------------------------

class TestRun:
    @pytest.mark.asyncio
    async def test_rss_failure_raises_fatal(self):
        crawler = make_crawler()
        with patch(
            "app.crawler.mingpao_crawler.fetch_rss",
            new=AsyncMock(side_effect=FeedFetchError("dead")),
        ):
            with pytest.raises(CrawlFatalError, match="MingPao RSS"):
                await crawler.run()

    @pytest.mark.asyncio
    async def test_happy_path(self):
        crawler = make_crawler()
        entry = make_entry(
            published_at=datetime(2026, 4, 6, 1, 0, tzinfo=timezone.utc)
        )

        context, _ = _make_fake_context()
        bm = MagicMock()
        bm.start = AsyncMock()
        bm.stop = AsyncMock()
        bm.acquire_context = AsyncMock(return_value=context)
        bm.release_context = AsyncMock()

        with (
            patch(
                "app.crawler.mingpao_crawler.fetch_rss",
                new=AsyncMock(return_value=[entry]),
            ),
            patch("app.crawler.mingpao_crawler.BrowserManager", return_value=bm),
        ):
            result = await crawler.run()

        assert len(result.successes) == 1
        bm.start.assert_called_once()
        bm.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_rss_returns_empty_result(self):
        crawler = make_crawler()
        with patch(
            "app.crawler.mingpao_crawler.fetch_rss",
            new=AsyncMock(return_value=[]),
        ):
            result = await crawler.run()
        assert result.successes == []
        assert result.failures == []
