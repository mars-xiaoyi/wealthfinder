from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.crawl.crawlers.base_crawler import (
    BaseCrawler,
    CrawlFailItem,
    CrawlResult,
    CrawlSuccessItem,
)
from app.crawl.exceptions import CrawlFatalError


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------

class TestCrawlSuccessItem:
    def test_minimal_construction(self):
        item = CrawlSuccessItem(
            title="t",
            body="b",
            source_url="https://example.com/a",
            published_at=None,
        )
        assert item.extra_metadata is None

    def test_with_extra_metadata_list(self):
        item = CrawlSuccessItem(
            title="t",
            body="b",
            source_url="https://example.com/a",
            published_at=datetime.now(timezone.utc),
            extra_metadata={"stock_code": ["00700", "00388"]},
        )
        assert item.extra_metadata == {"stock_code": ["00700", "00388"]}


class TestCrawlFailItem:
    def test_required_fields(self):
        f = CrawlFailItem(
            source_url="https://example.com/x",
            error_type="NETWORK",
            error_code="HTTP_403",
            attempt_count=3,
        )
        assert f.error_type == "NETWORK"
        assert f.attempt_count == 3


class TestCrawlResult:
    def test_default_empty_lists(self):
        result = CrawlResult()
        assert result.successes == []
        assert result.failures == []

    def test_independent_default_lists(self):
        a = CrawlResult()
        b = CrawlResult()
        a.successes.append(
            CrawlSuccessItem(title="t", body="b", source_url="u", published_at=None)
        )
        assert b.successes == []  # default_factory not shared


# ---------------------------------------------------------------------------
# CrawlFatalError
# ---------------------------------------------------------------------------

class TestCrawlFatalError:
    def test_is_exception(self):
        with pytest.raises(CrawlFatalError, match="boom"):
            raise CrawlFatalError("boom")


# ---------------------------------------------------------------------------
# BaseCrawler — abstract enforcement + helper
# ---------------------------------------------------------------------------

class TestBaseCrawlerAbstract:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            BaseCrawler(  # type: ignore[abstract]
                source_config=MagicMock(),
                page_crawler=MagicMock(),
                db=MagicMock(),
            )


class _DummyCrawler(BaseCrawler):
    async def run(self) -> CrawlResult:  # pragma: no cover - not used
        return CrawlResult()


class TestBaseCrawlerInit:
    def test_stores_attributes(self):
        sc = MagicMock()
        pc = MagicMock()
        db = MagicMock()
        c = _DummyCrawler(source_config=sc, page_crawler=pc, db=db, crawl_date=None)
        assert c.source_config is sc
        assert c.page_crawler is pc
        assert c.db is db
        assert c.crawl_date is None


class TestIsUrlInErrorLog:
    @pytest.mark.asyncio
    async def test_returns_true_when_row_exists(self):
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value={"exists": True})
        c = _DummyCrawler(source_config=MagicMock(), page_crawler=MagicMock(), db=db)
        assert await c._is_url_in_error_log("https://x") is True
        db.fetch_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_row_missing(self):
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value={"exists": False})
        c = _DummyCrawler(source_config=MagicMock(), page_crawler=MagicMock(), db=db)
        assert await c._is_url_in_error_log("https://x") is False

    @pytest.mark.asyncio
    async def test_returns_false_when_fetch_returns_none(self):
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value=None)
        c = _DummyCrawler(source_config=MagicMock(), page_crawler=MagicMock(), db=db)
        assert await c._is_url_in_error_log("https://x") is False
