from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import CrawlSourceConfig
from app.crawl.crawlers.base_crawler import CrawlResult
from app.common.error_codes import DocumentParseErrorCode, NetworkErrorCode
from app.crawl.exceptions import CrawlFatalError
from app.crawl.fetchers.feed_fetcher import FeedEntry, FeedFetchError
from app.crawl.crawlers.mingpao_crawler import MingPaoCrawler


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
    def test_parses_morning_time_from_date_color2nd(self):
        html = "<div class='date color2nd'>2026年4月6日 星期一　06:00AM</div>"
        result = MingPaoCrawler._extract_published_at_from_page(html)
        # 06:00 HKT = 22:00 previous day UTC
        assert result == datetime(2026, 4, 5, 22, 0, tzinfo=timezone.utc)

    def test_parses_evening_time(self):
        html = "<div class='date color2nd'>2026年4月6日 星期一　06:00PM</div>"
        result = MingPaoCrawler._extract_published_at_from_page(html)
        # 18:00 HKT = 10:00 UTC same day
        assert result == datetime(2026, 4, 6, 10, 0, tzinfo=timezone.utc)

    def test_ignores_dates_outside_date_color2nd(self):
        # A footer/copyright date elsewhere on the page must not be picked up.
        html = (
            "<footer>Copyright 2020年1月1日 星期三 12:00AM</footer>"
            "<div class='date color2nd'>2026年4月6日 星期一　06:00AM</div>"
        )
        result = MingPaoCrawler._extract_published_at_from_page(html)
        assert result == datetime(2026, 4, 5, 22, 0, tzinfo=timezone.utc)

    def test_ignores_date_only_datepublished_sibling(self):
        # `div.date[itemprop="datePublished"]` only carries the date (no time)
        # and must not be used.
        html = (
            "<div class='date' itemprop='datePublished'>2026年4月6日星期一</div>"
        )
        assert MingPaoCrawler._extract_published_at_from_page(html) is None

    def test_no_selector_returns_none(self):
        assert MingPaoCrawler._extract_published_at_from_page("<html></html>") is None

    def test_selector_present_but_no_pattern_returns_none(self):
        html = "<div class='date color2nd'>2026年4月6日 星期一</div>"
        assert MingPaoCrawler._extract_published_at_from_page(html) is None


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
            "<div class='date color2nd'>2026年4月6日 星期一　06:00PM</div></html>"
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
        assert result.failures[0].error_code == NetworkErrorCode.BROWSER_FETCH_FAILED.error_code
        assert result.failures[0].error_type == NetworkErrorCode.BROWSER_FETCH_FAILED.error_type

    @pytest.mark.asyncio
    async def test_empty_body_skips_without_failure(self):
        crawler = make_crawler()
        context, _ = _make_fake_context(html="<html><article></article></html>")
        result = CrawlResult()

        await crawler._fetch_one_article(context, make_entry(), result)

        assert result.successes == []
        assert result.failures == []

    @pytest.mark.asyncio
    async def test_parse_exception_creates_fail_item(self):
        crawler = make_crawler()
        context, _ = _make_fake_context()
        result = CrawlResult()

        with patch(
            "app.crawl.crawlers.mingpao_crawler.extract_body_css",
            side_effect=RuntimeError("lxml exploded"),
        ):
            await crawler._fetch_one_article(context, make_entry(), result)

        assert result.failures[0].error_code == DocumentParseErrorCode.PARSE_ERROR.error_code
        assert result.failures[0].error_type == DocumentParseErrorCode.PARSE_ERROR.error_type


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
            "app.crawl.crawlers.mingpao_crawler.fetch_rss",
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
                "app.crawl.crawlers.mingpao_crawler.fetch_rss",
                new=AsyncMock(return_value=[entry]),
            ),
            patch("app.crawl.crawlers.mingpao_crawler.BrowserManager", return_value=bm),
        ):
            result = await crawler.run()

        assert len(result.successes) == 1
        bm.start.assert_called_once()
        bm.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_rss_returns_empty_result(self):
        crawler = make_crawler()
        with patch(
            "app.crawl.crawlers.mingpao_crawler.fetch_rss",
            new=AsyncMock(return_value=[]),
        ):
            result = await crawler.run()
        assert result.successes == []
        assert result.failures == []
