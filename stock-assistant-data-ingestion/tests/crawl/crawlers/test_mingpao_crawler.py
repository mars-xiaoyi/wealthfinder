from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import CrawlSourceConfig
from app.crawl.crawlers.base_crawler import CrawlResult
from app.crawl.crawlers.mingpao_crawler import MingPaoCrawler
from app.crawl.exceptions import CrawlFatalException
from app.crawl.fetchers.feed_fetcher import FeedEntry, FeedFetchException


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


def make_entry(
    url: str = "https://news.mingpao.com/article/1",
    published_at=None,
    description: str | None = "teaser body text",
):
    return FeedEntry(
        title="標題",
        url=url,
        published_at=published_at,
        description=description,
    )


# ---------------------------------------------------------------------------
# _process_entry
# ---------------------------------------------------------------------------

class TestProcessEntry:
    @pytest.mark.asyncio
    async def test_success_uses_rss_description_as_body(self):
        crawler = make_crawler()
        result = CrawlResult()
        published = datetime(2026, 4, 6, 1, 0, tzinfo=timezone.utc)

        await crawler._process_entry(
            make_entry(description="  teaser body text  ", published_at=published),
            result,
        )

        assert len(result.successes) == 1
        s = result.successes[0]
        assert s.title == "標題"
        assert s.body == "teaser body text"  # stripped
        assert s.source_url == "https://news.mingpao.com/article/1"
        assert s.published_at == published

    @pytest.mark.asyncio
    async def test_empty_description_skips_without_failure(self):
        crawler = make_crawler()
        result = CrawlResult()

        await crawler._process_entry(make_entry(description=""), result)

        assert result.successes == []
        assert result.failures == []

    @pytest.mark.asyncio
    async def test_none_description_skips_without_failure(self):
        crawler = make_crawler()
        result = CrawlResult()

        await crawler._process_entry(make_entry(description=None), result)

        assert result.successes == []
        assert result.failures == []

    @pytest.mark.asyncio
    async def test_whitespace_only_description_skips(self):
        crawler = make_crawler()
        result = CrawlResult()

        await crawler._process_entry(make_entry(description="   \n  "), result)

        assert result.successes == []
        assert result.failures == []

    @pytest.mark.asyncio
    async def test_skips_url_in_error_log(self):
        crawler = make_crawler(db=make_db(in_error_log=True))
        result = CrawlResult()

        await crawler._process_entry(make_entry(), result)

        assert result.successes == []
        assert result.failures == []

    @pytest.mark.asyncio
    async def test_no_published_at_passes_through_none(self):
        crawler = make_crawler()
        result = CrawlResult()

        await crawler._process_entry(make_entry(published_at=None), result)

        assert result.successes[0].published_at is None


# ---------------------------------------------------------------------------
# run() — top level
# ---------------------------------------------------------------------------

class TestRun:
    @pytest.mark.asyncio
    async def test_rss_failure_raises_fatal(self):
        crawler = make_crawler()
        with patch(
            "app.crawl.crawlers.mingpao_crawler.fetch_rss",
            new=AsyncMock(side_effect=FeedFetchException("dead")),
        ):
            with pytest.raises(CrawlFatalException, match="MingPao RSS"):
                await crawler.run()

    @pytest.mark.asyncio
    async def test_happy_path(self):
        crawler = make_crawler()
        entry = make_entry(
            published_at=datetime(2026, 4, 6, 1, 0, tzinfo=timezone.utc)
        )

        with patch(
            "app.crawl.crawlers.mingpao_crawler.fetch_rss",
            new=AsyncMock(return_value=[entry]),
        ):
            result = await crawler.run()

        assert len(result.successes) == 1
        assert result.successes[0].body == "teaser body text"

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

    @pytest.mark.asyncio
    async def test_multiple_entries_mixed_outcomes(self):
        crawler = make_crawler()
        good = make_entry(url="https://news.mingpao.com/a", description="ok body")
        empty = make_entry(url="https://news.mingpao.com/b", description="")

        with patch(
            "app.crawl.crawlers.mingpao_crawler.fetch_rss",
            new=AsyncMock(return_value=[good, empty]),
        ):
            result = await crawler.run()

        assert len(result.successes) == 1
        assert result.successes[0].source_url == "https://news.mingpao.com/a"
        assert result.failures == []
