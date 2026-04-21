from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import CrawlConfig, CrawlSourceConfig
from app.crawl.crawlers.aastocks_crawler import AAStocksCrawler
from app.crawl.crawlers.base_crawler import CrawlResult
from app.common.error_codes import CrawlErrorCode, DocumentParseErrorCode
from app.crawl.exceptions import CrawlBlockedException, CrawlFatalException


def make_source_config() -> CrawlSourceConfig:
    return CrawlSourceConfig(
        max_concurrent=3,
        request_interval_min_ms=0,
        request_interval_max_ms=0,
    )


def make_page_crawler(max_retry: int = 3) -> MagicMock:
    pc = MagicMock()
    pc.config = CrawlConfig(
        max_retry=max_retry,
        retry_base_wait_ms=10,
        request_timeout_s=10,
        browser_navigation_timeout_ms=30000,
        crawl_sources={},
    )
    pc.fetch = AsyncMock()
    return pc


def make_db(in_error_log: bool = False) -> MagicMock:
    db = MagicMock()
    db.fetch_one = AsyncMock(return_value={"exists": in_error_log})
    return db


def make_crawler(page_crawler=None, db=None) -> AAStocksCrawler:
    return AAStocksCrawler(
        source_config=make_source_config(),
        page_crawler=page_crawler or make_page_crawler(),
        db=db or make_db(),
    )


def make_response(text: str) -> MagicMock:
    r = MagicMock()
    r.text = text
    return r


# ---------------------------------------------------------------------------
# _extract_article_urls
# ---------------------------------------------------------------------------

class TestExtractArticleUrls:
    def test_extracts_relative_links(self):
        html = """
        <a href="/en/stocks/news/aafn-con/NOW.111/x">a</a>
        <a href="/en/stocks/news/aafn-con/NOW.222/y">b</a>
        """
        urls = AAStocksCrawler._extract_article_urls(html)
        assert urls == [
            "https://www.aastocks.com/en/stocks/news/aafn-con/NOW.111/x",
            "https://www.aastocks.com/en/stocks/news/aafn-con/NOW.222/y",
        ]

    def test_dedupes(self):
        html = """
        <a href="/en/stocks/news/aafn-con/NOW.111/x">a</a>
        <a href="/en/stocks/news/aafn-con/NOW.111/x">a-dup</a>
        """
        urls = AAStocksCrawler._extract_article_urls(html)
        assert len(urls) == 1

    def test_returns_all_matching_links(self):
        links = "".join(
            f'<a href="/en/stocks/news/aafn-con/NOW.{i}/x">x</a>' for i in range(20)
        )
        urls = AAStocksCrawler._extract_article_urls(links)
        assert len(urls) == 20


# ---------------------------------------------------------------------------
# _parse_published_at
# ---------------------------------------------------------------------------

class TestParsePublishedAt:
    def test_converts_hkt_to_utc_from_newstime5_div(self):
        html = (
            "<div class='float_l newstime5'>"
            "<script type='text/javascript'>"
            "document.write(ConvertToLocalTime({dt:'2026/04/06 01:27'}));"
            "</script></div>"
        )
        result = AAStocksCrawler._parse_published_at(html)
        # 01:27 HKT = 17:27 previous day UTC
        assert result == datetime(2026, 4, 5, 17, 27, tzinfo=timezone.utc)

    def test_ignores_dates_outside_newstime5(self):
        # A sidebar/related-stories date must NOT be picked up: before the
        # CSS scoping, the regex would greedily match anywhere in the page.
        html = (
            "<div class='sidebar'>Related: 2020/01/01 00:00</div>"
            "<div class='float_l newstime5'>"
            "<script>document.write(ConvertToLocalTime({dt:'2026/04/06 09:00'}));</script>"
            "</div>"
        )
        result = AAStocksCrawler._parse_published_at(html)
        # Must pick up 09:00 HKT (01:00 UTC), not 2020-01-01.
        assert result == datetime(2026, 4, 6, 1, 0, tzinfo=timezone.utc)

    def test_skips_newshead_source_sibling(self):
        # The source-label cell shares the `newstime5` class but is marked
        # `newshead-Source`; it must not be treated as the timestamp cell.
        html = (
            "<div class='float_l newstime5 newshead-Source'>AASTOCKS 2020/01/01 00:00</div>"
            "<div class='float_l newstime5'>"
            "<script>document.write(ConvertToLocalTime({dt:'2026/04/06 09:00'}));</script>"
            "</div>"
        )
        result = AAStocksCrawler._parse_published_at(html)
        assert result == datetime(2026, 4, 6, 1, 0, tzinfo=timezone.utc)

    def test_returns_none_when_selector_missing(self):
        assert AAStocksCrawler._parse_published_at("<p>nothing</p>") is None

    def test_returns_none_when_selector_present_but_no_pattern(self):
        html = "<div class='float_l newstime5'>no timestamp here</div>"
        assert AAStocksCrawler._parse_published_at(html) is None


# ---------------------------------------------------------------------------
# _fetch_one_article
# ---------------------------------------------------------------------------

class TestFetchOneArticle:
    @pytest.mark.asyncio
    async def test_success(self):
        pc = make_page_crawler()
        html = (
            "<html><head><title>Some Title</title></head>"
            "<body><div class='newscontent5 fLevel3'>article body here</div>"
            "<div class='float_l newstime5'>"
            "<script>document.write(ConvertToLocalTime({dt:'2026/04/06 01:27'}));</script>"
            "</div></body></html>"
        )
        pc.fetch.return_value = make_response(html)
        crawler = make_crawler(page_crawler=pc)
        result = CrawlResult()

        await crawler._fetch_one_article("https://x", result)

        assert len(result.successes) == 1
        s = result.successes[0]
        assert s.body == "article body here"
        assert s.title == "Some Title"
        assert s.published_at == datetime(2026, 4, 5, 17, 27, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_blocked_404(self):
        pc = make_page_crawler()
        pc.fetch.side_effect = CrawlBlockedException("HTTP 404 for x")
        crawler = make_crawler(page_crawler=pc)
        result = CrawlResult()

        await crawler._fetch_one_article("https://x", result)

        assert result.failures[0].error_code == CrawlErrorCode.URL_GET_FAILED.error_code

    @pytest.mark.asyncio
    async def test_network_error(self):
        pc = make_page_crawler(max_retry=2)
        pc.fetch.side_effect = CrawlBlockedException("dead")
        crawler = make_crawler(page_crawler=pc)
        result = CrawlResult()

        await crawler._fetch_one_article("https://x", result)

        assert result.failures[0].error_code == CrawlErrorCode.URL_GET_FAILED.error_code
        assert result.failures[0].attempt_count == 2

    @pytest.mark.asyncio
    async def test_empty_body_skips_without_failure(self):
        pc = make_page_crawler()
        pc.fetch.return_value = make_response("<html><body></body></html>")
        crawler = make_crawler(page_crawler=pc)
        result = CrawlResult()

        await crawler._fetch_one_article("https://x", result)

        assert result.successes == []
        assert result.failures == []

    @pytest.mark.asyncio
    async def test_parse_exception(self):
        pc = make_page_crawler()
        pc.fetch.return_value = make_response("<html></html>")
        crawler = make_crawler(page_crawler=pc)
        result = CrawlResult()

        with patch(
            "app.crawl.crawlers.aastocks_crawler.extract_body_css",
            side_effect=RuntimeError("lxml exploded"),
        ):
            await crawler._fetch_one_article("https://x", result)

        assert result.failures[0].error_code == DocumentParseErrorCode.PARSE_ERROR.error_code


# ---------------------------------------------------------------------------
# _crawl_articles — error log skip
# ---------------------------------------------------------------------------

class TestCrawlArticlesSkip:
    @pytest.mark.asyncio
    async def test_skips_url_in_error_log(self):
        pc = make_page_crawler()
        crawler = make_crawler(
            page_crawler=pc,
            db=make_db(in_error_log=True),
        )
        result = CrawlResult()

        await crawler._crawl_articles(["https://x"], result)

        pc.fetch.assert_not_called()


# ---------------------------------------------------------------------------
# run() — top level
# ---------------------------------------------------------------------------

class TestRun:
    @pytest.mark.asyncio
    async def test_list_page_failure_raises_fatal(self):
        pc = make_page_crawler()
        pc.fetch.side_effect = CrawlBlockedException("dead")
        crawler = make_crawler(page_crawler=pc)

        with pytest.raises(CrawlFatalException, match="AAStocks list page"):
            await crawler.run()

    @pytest.mark.asyncio
    async def test_blocked_list_page_raises_fatal(self):
        pc = make_page_crawler()
        pc.fetch.side_effect = CrawlBlockedException("HTTP 403")
        crawler = make_crawler(page_crawler=pc)
        with pytest.raises(CrawlFatalException):
            await crawler.run()

    @pytest.mark.asyncio
    async def test_happy_path(self):
        pc = make_page_crawler()
        list_html = '<a href="/en/stocks/news/aafn-con/NOW.1/x">link</a>'
        article_html = (
            "<html><head><title>T</title></head>"
            "<body><div class='newscontent5'>body</div>"
            "<div class='float_l newstime5'>"
            "<script>document.write(ConvertToLocalTime({dt:'2026/04/06 12:00'}));</script>"
            "</div></body></html>"
        )
        pc.fetch.side_effect = [make_response(list_html), make_response(article_html)]
        crawler = make_crawler(page_crawler=pc)

        result = await crawler.run()

        assert len(result.successes) == 1
        assert result.successes[0].body == "body"
