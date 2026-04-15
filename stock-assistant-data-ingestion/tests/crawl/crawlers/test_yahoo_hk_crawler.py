from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import CrawlConfig, CrawlSourceConfig
from app.crawl.crawlers.base_crawler import CrawlResult
from app.common.error_codes import DocumentParseErrorCode, NetworkErrorCode
from app.crawl.exceptions import CrawlBlockedError, CrawlFatalError, CrawlNetworkError
from app.crawl.fetchers.feed_fetcher import FeedEntry, FeedFetchError
from app.crawl.crawlers.yahoo_hk_crawler import YahooHKCrawler


def make_source_config() -> CrawlSourceConfig:
    return CrawlSourceConfig(
        max_concurrent=3,
        request_interval_min_ms=0,
        request_interval_max_ms=0,
    )


def make_page_crawler(max_retry: int = 3) -> MagicMock:
    pc = MagicMock()
    pc._config = CrawlConfig(
        max_retry=max_retry,
        retry_base_wait_ms=10,
        request_timeout_s=10,
        browser_navigation_timeout_ms=30000,
        crawl_sources={},
    )
    pc.fetch = AsyncMock()
    return pc


def make_db(in_error_log=False, previous_crawl=None) -> MagicMock:
    db = MagicMock()

    async def fetch_one(query, *args):
        if "crawl_error_log" in query:
            return {"exists": in_error_log}
        if "raw_news" in query:
            return {"max_created_at": previous_crawl}
        return None

    db.fetch_one = AsyncMock(side_effect=fetch_one)
    return db


def make_crawler(page_crawler=None, db=None) -> YahooHKCrawler:
    return YahooHKCrawler(
        source_config=make_source_config(),
        page_crawler=page_crawler or make_page_crawler(),
        db=db or make_db(),
    )


def make_entry(url="https://hk.finance.yahoo.com/news/article-1.html", published_at=None):
    return FeedEntry(title="Title", url=url, published_at=published_at)


def make_response(text):
    r = MagicMock()
    r.text = text
    return r


# ---------------------------------------------------------------------------
# _get_previous_crawl_time
# ---------------------------------------------------------------------------

class TestGetPreviousCrawlTime:
    @pytest.mark.asyncio
    async def test_returns_max_created_at(self):
        prev = datetime(2026, 4, 6, tzinfo=timezone.utc)
        crawler = make_crawler(db=make_db(previous_crawl=prev))
        result = await crawler._get_previous_crawl_time()
        assert result == prev

    @pytest.mark.asyncio
    async def test_returns_none_when_empty(self):
        crawler = make_crawler(db=make_db(previous_crawl=None))
        assert await crawler._get_previous_crawl_time() is None


# ---------------------------------------------------------------------------
# _check_coverage_gap
# ---------------------------------------------------------------------------

class TestCheckCoverageGap:
    @pytest.mark.asyncio
    async def test_warns_when_oldest_newer_than_previous(self, caplog):
        prev = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
        crawler = make_crawler(db=make_db(previous_crawl=prev))
        entries = [
            make_entry(published_at=datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc))
        ]
        with caplog.at_level("WARNING"):
            await crawler._check_coverage_gap(entries)
        assert any("coverage gap" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_warning_when_oldest_older_than_previous(self, caplog):
        prev = datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc)
        crawler = make_crawler(db=make_db(previous_crawl=prev))
        entries = [
            make_entry(published_at=datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc))
        ]
        with caplog.at_level("WARNING"):
            await crawler._check_coverage_gap(entries)
        assert not any("coverage gap" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_previous_no_warning(self, caplog):
        crawler = make_crawler(db=make_db(previous_crawl=None))
        entries = [
            make_entry(published_at=datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc))
        ]
        with caplog.at_level("WARNING"):
            await crawler._check_coverage_gap(entries)
        assert not any("coverage gap" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _fetch_one_article
# ---------------------------------------------------------------------------

class TestFetchOneArticle:
    @pytest.mark.asyncio
    async def test_success(self):
        pc = make_page_crawler()
        pc.fetch.return_value = make_response("<html><p>some body</p></html>")
        crawler = make_crawler(page_crawler=pc)
        result = CrawlResult()
        published = datetime(2026, 4, 6, 1, 0, tzinfo=timezone.utc)

        with patch(
            "app.crawl.crawlers.yahoo_hk_crawler.extract_body_auto",
            return_value="extracted body",
        ):
            await crawler._fetch_one_article(make_entry(published_at=published), result)

        assert len(result.successes) == 1
        assert result.successes[0].body == "extracted body"
        assert result.successes[0].published_at == published

    @pytest.mark.asyncio
    async def test_blocked(self):
        pc = make_page_crawler()
        pc.fetch.side_effect = CrawlBlockedError("HTTP 403")
        crawler = make_crawler(page_crawler=pc)
        result = CrawlResult()
        await crawler._fetch_one_article(make_entry(), result)
        assert result.failures[0].error_code == NetworkErrorCode.HTTP_403.error_code

    @pytest.mark.asyncio
    async def test_network_error(self):
        pc = make_page_crawler(max_retry=4)
        pc.fetch.side_effect = CrawlNetworkError("dead")
        crawler = make_crawler(page_crawler=pc)
        result = CrawlResult()
        await crawler._fetch_one_article(make_entry(), result)
        assert result.failures[0].attempt_count == 4
        assert result.failures[0].error_code == NetworkErrorCode.NETWORK_ERROR.error_code

    @pytest.mark.asyncio
    async def test_empty_body_skips_without_failure(self):
        pc = make_page_crawler()
        pc.fetch.return_value = make_response("html")
        crawler = make_crawler(page_crawler=pc)
        result = CrawlResult()
        with patch(
            "app.crawl.crawlers.yahoo_hk_crawler.extract_body_auto",
            return_value=None,
        ):
            await crawler._fetch_one_article(make_entry(), result)
        assert result.successes == []
        assert result.failures == []

    @pytest.mark.asyncio
    async def test_parse_exception(self):
        pc = make_page_crawler()
        pc.fetch.return_value = make_response("html")
        crawler = make_crawler(page_crawler=pc)
        result = CrawlResult()
        with patch(
            "app.crawl.crawlers.yahoo_hk_crawler.extract_body_auto",
            side_effect=RuntimeError("boom"),
        ):
            await crawler._fetch_one_article(make_entry(), result)
        assert result.failures[0].error_code == DocumentParseErrorCode.PARSE_ERROR.error_code


# ---------------------------------------------------------------------------
# run() — top level (URL filter, fatal, happy path)
# ---------------------------------------------------------------------------

class TestRun:
    @pytest.mark.asyncio
    async def test_filters_ad_urls(self):
        pc = make_page_crawler()
        pc.fetch.return_value = make_response("html")
        crawler = make_crawler(page_crawler=pc)

        ad = make_entry(url="https://promotions.yahoo.com/special-deal")
        real = make_entry(
            url="https://hk.finance.yahoo.com/news/real-article.html",
            published_at=datetime(2026, 4, 6, tzinfo=timezone.utc),
        )

        with (
            patch(
                "app.crawl.crawlers.yahoo_hk_crawler.fetch_rss",
                new=AsyncMock(return_value=[ad, real]),
            ),
            patch(
                "app.crawl.crawlers.yahoo_hk_crawler.extract_body_auto",
                return_value="body",
            ),
        ):
            result = await crawler.run()

        assert len(result.successes) == 1
        assert result.successes[0].source_url == real.url
        # ad URL should not have been fetched
        pc.fetch.assert_called_once_with(real.url)

    @pytest.mark.asyncio
    async def test_rss_failure_raises_fatal(self):
        crawler = make_crawler()
        with patch(
            "app.crawl.crawlers.yahoo_hk_crawler.fetch_rss",
            new=AsyncMock(side_effect=FeedFetchError("dead")),
        ):
            with pytest.raises(CrawlFatalError, match="Yahoo HK RSS"):
                await crawler.run()

    @pytest.mark.asyncio
    async def test_skips_url_in_error_log(self):
        pc = make_page_crawler()
        crawler = make_crawler(
            page_crawler=pc,
            db=make_db(in_error_log=True),
        )
        real = make_entry(
            url="https://hk.finance.yahoo.com/news/x.html",
            published_at=datetime(2026, 4, 6, tzinfo=timezone.utc),
        )
        with patch(
            "app.crawl.crawlers.yahoo_hk_crawler.fetch_rss",
            new=AsyncMock(return_value=[real]),
        ):
            result = await crawler.run()

        pc.fetch.assert_not_called()
        assert result.successes == []
