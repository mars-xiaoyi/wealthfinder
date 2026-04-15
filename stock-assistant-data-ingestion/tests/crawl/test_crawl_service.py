from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from app.config import CrawlConfig, CrawlSourceConfig
from app.crawl.crawlers.aastocks_crawler import AAStocksCrawler
from app.crawl.crawlers.base_crawler import (
    CrawlFailItem,
    CrawlResult,
    CrawlSuccessItem,
)
from app.crawl.crawl_service import CrawlService
from app.common.error_codes import NetworkErrorCode
from app.crawl.exceptions import CrawlFatalError
from app.crawl.crawlers.hkex_crawler import HKEXCrawler
from app.crawl.crawlers.mingpao_crawler import MingPaoCrawler
from app.crawl.source_name import CrawlSourceName
from app.crawl.crawlers.yahoo_hk_crawler import YahooHKCrawler
from app.redis.stream_client import (
    STREAM_CRAWL_COMPLETED,
    STREAM_RAW_NEWS_INSERTED,
)


def make_config() -> CrawlConfig:
    sources = {
        name: CrawlSourceConfig(
            max_concurrent=3,
            request_interval_min_ms=0,
            request_interval_max_ms=0,
        )
        for name in ("HKEX", "MINGPAO", "AASTOCKS", "YAHOO_HK")
    }
    return CrawlConfig(
        max_retry=3,
        retry_base_wait_ms=10,
        request_timeout_s=10,
        browser_navigation_timeout_ms=30000,
        crawl_sources=sources,
    )


def make_service() -> tuple[CrawlService, MagicMock, MagicMock, MagicMock]:
    db = MagicMock()
    db.execute = AsyncMock()
    db.fetch_one = AsyncMock()
    stream = MagicMock()
    stream.publish = AsyncMock()
    page_crawler = MagicMock()
    service = CrawlService(
        db=db,
        stream_client=stream,
        page_crawler=page_crawler,
        config=make_config(),
    )
    return service, db, stream, page_crawler


def make_success(url="https://x/1") -> CrawlSuccessItem:
    return CrawlSuccessItem(
        title="Sample 標題",
        body="body content",
        source_url=url,
        published_at=datetime(2026, 4, 6, tzinfo=timezone.utc),
        extra_metadata={"stock_code": ["00700"]},
    )


def make_failure(url="https://x/bad") -> CrawlFailItem:
    return CrawlFailItem(
        source_url=url,
        error_type=NetworkErrorCode.HTTP_403.error_type,
        error_code=NetworkErrorCode.HTTP_403.error_code,
        attempt_count=1,
    )


# ---------------------------------------------------------------------------
# _create_crawler
# ---------------------------------------------------------------------------

class TestCreateCrawler:
    def test_dispatches_hkex(self):
        service, *_ = make_service()
        c = service._create_crawler(CrawlSourceName.HKEX, date(2026, 4, 2))
        assert isinstance(c, HKEXCrawler)
        assert c.crawl_date == date(2026, 4, 2)

    def test_dispatches_mingpao(self):
        service, *_ = make_service()
        assert isinstance(
            service._create_crawler(CrawlSourceName.MINGPAO, None), MingPaoCrawler
        )

    def test_dispatches_aastocks(self):
        service, *_ = make_service()
        assert isinstance(
            service._create_crawler(CrawlSourceName.AASTOCKS, None), AAStocksCrawler
        )

    def test_dispatches_yahoo_hk(self):
        service, *_ = make_service()
        assert isinstance(
            service._create_crawler(CrawlSourceName.YAHOO_HK, None), YahooHKCrawler
        )

    def test_uniform_constructor_signature(self):
        service, db, _, pc = make_service()
        c = service._create_crawler(CrawlSourceName.YAHOO_HK, None)
        assert c.db is db
        assert c.page_crawler is pc


# ---------------------------------------------------------------------------
# _persist_successes
# ---------------------------------------------------------------------------

class TestPersistSuccesses:
    @pytest.mark.asyncio
    async def test_inserts_and_publishes_per_record(self):
        service, db, stream, _ = make_service()
        items = [make_success("https://x/1"), make_success("https://x/2")]

        await service._persist_successes(CrawlSourceName.HKEX, items)

        assert db.execute.call_count == 2
        assert stream.publish.call_count == 2
        for call in stream.publish.call_args_list:
            args, _ = call
            assert args[0] == STREAM_RAW_NEWS_INSERTED
            assert "raw_id" in args[1]

    @pytest.mark.asyncio
    async def test_unique_violation_skips_publish(self):
        service, db, stream, _ = make_service()
        db.execute.side_effect = asyncpg.UniqueViolationError("dup")

        await service._persist_successes(
            CrawlSourceName.HKEX, [make_success()]
        )

        stream.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_failure_swallowed_other_records_still_processed(self):
        service, db, stream, _ = make_service()
        db.execute.side_effect = [Exception("boom"), None]

        await service._persist_successes(
            CrawlSourceName.HKEX,
            [make_success("https://x/1"), make_success("https://x/2")],
        )

        # Only the second item should publish
        assert stream.publish.call_count == 1

    @pytest.mark.asyncio
    async def test_extra_metadata_serialised_as_json(self):
        service, db, _, _ = make_service()
        item = make_success()

        await service._persist_successes(CrawlSourceName.HKEX, [item])

        # Last positional arg of execute should be the JSON string
        args, _ = db.execute.call_args
        assert args[-1] == '{"stock_code": ["00700"]}'

    @pytest.mark.asyncio
    async def test_extra_metadata_none_passes_none(self):
        service, db, _, _ = make_service()
        item = CrawlSuccessItem(
            title="t",
            body="b",
            source_url="https://x",
            published_at=None,
            extra_metadata=None,
        )

        await service._persist_successes(CrawlSourceName.MINGPAO, [item])

        args, _ = db.execute.call_args
        assert args[-1] is None


# ---------------------------------------------------------------------------
# _save_crawl_error / _persist_failures
# ---------------------------------------------------------------------------

class TestSaveCrawlError:
    @pytest.mark.asyncio
    async def test_inserts_error_record(self):
        service, db, _, _ = make_service()
        await service._save_crawl_error("exec-1", CrawlSourceName.HKEX, make_failure())
        assert db.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_swallows_db_failure(self):
        service, db, _, _ = make_service()
        db.execute.side_effect = Exception("db down")
        # Should not raise
        await service._save_crawl_error("exec-1", CrawlSourceName.HKEX, make_failure())

    @pytest.mark.asyncio
    async def test_persist_failures_iterates_all(self):
        service, db, _, _ = make_service()
        await service._persist_failures(
            "exec-1",
            CrawlSourceName.HKEX,
            [make_failure("https://x/1"), make_failure("https://x/2")],
        )
        assert db.execute.call_count == 2


# ---------------------------------------------------------------------------
# _publish_completed
# ---------------------------------------------------------------------------

class TestPublishCompleted:
    @pytest.mark.asyncio
    async def test_success_payload_omits_error_detail(self):
        service, _, stream, _ = make_service()
        await service._publish_completed("exec-1", "SUCCESS")
        stream.publish.assert_called_once_with(
            STREAM_CRAWL_COMPLETED,
            {"execution_id": "exec-1", "status": "SUCCESS"},
        )
        # Redis Streams have no NULL — absence IS the NULL representation
        payload = stream.publish.call_args.args[1]
        assert "error_detail" not in payload

    @pytest.mark.asyncio
    async def test_failed_payload_includes_error_detail(self):
        service, _, stream, _ = make_service()
        await service._publish_completed("exec-1", "FAILED", "boom")
        args, _ = stream.publish.call_args
        assert args[1]["status"] == "FAILED"
        assert args[1]["error_detail"] == "boom"

    @pytest.mark.asyncio
    async def test_none_error_detail_is_omitted(self):
        service, _, stream, _ = make_service()
        await service._publish_completed("exec-1", "SUCCESS", None)
        payload = stream.publish.call_args.args[1]
        assert "error_detail" not in payload


# ---------------------------------------------------------------------------
# execute() — orchestration
# ---------------------------------------------------------------------------

def _patch_crawler(service, fake_run):
    """Replace _create_crawler with one that returns a stub crawler."""
    crawler = MagicMock()
    crawler.run = fake_run
    service._create_crawler = MagicMock(return_value=crawler)
    return crawler


class TestExecute:
    @pytest.mark.asyncio
    async def test_success_publishes_all_signals(self):
        service, db, stream, _ = make_service()
        result = CrawlResult(
            successes=[make_success("https://x/1")],
            failures=[make_failure("https://x/bad")],
        )
        _patch_crawler(service, AsyncMock(return_value=result))

        await service.execute("exec-1", CrawlSourceName.HKEX, None)

        # 1 raw_news insert + 1 crawl_error_log insert
        assert db.execute.call_count == 2
        # 1 raw_news_inserted + 1 crawl_completed SUCCESS
        publish_calls = stream.publish.call_args_list
        streams_published = [call.args[0] for call in publish_calls]
        assert STREAM_RAW_NEWS_INSERTED in streams_published
        assert STREAM_CRAWL_COMPLETED in streams_published
        completed_call = next(
            c for c in publish_calls if c.args[0] == STREAM_CRAWL_COMPLETED
        )
        assert completed_call.args[1]["status"] == "SUCCESS"

    @pytest.mark.asyncio
    async def test_fatal_error_publishes_failed(self):
        service, db, stream, _ = make_service()
        _patch_crawler(
            service, AsyncMock(side_effect=CrawlFatalError("RSS dead"))
        )

        await service.execute("exec-1", CrawlSourceName.MINGPAO, None)

        # Only the crawl_completed FAILED publish should fire
        stream.publish.assert_called_once()
        args = stream.publish.call_args.args
        assert args[0] == STREAM_CRAWL_COMPLETED
        assert args[1]["status"] == "FAILED"
        assert args[1]["error_detail"] == "RSS dead"
        # No raw_news inserts
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_records_still_publishes_success(self):
        service, db, stream, _ = make_service()
        _patch_crawler(service, AsyncMock(return_value=CrawlResult()))

        await service.execute("exec-1", CrawlSourceName.HKEX, None)

        stream.publish.assert_called_once()
        args = stream.publish.call_args.args
        assert args[0] == STREAM_CRAWL_COMPLETED
        assert args[1]["status"] == "SUCCESS"

    @pytest.mark.asyncio
    async def test_unexpected_exception_never_raises(self):
        service, _, stream, _ = make_service()
        _patch_crawler(
            service, AsyncMock(side_effect=RuntimeError("unexpected"))
        )

        # Must not raise
        await service.execute("exec-1", CrawlSourceName.HKEX, None)

        # Should still publish FAILED
        stream.publish.assert_called_once()
        assert stream.publish.call_args.args[1]["status"] == "FAILED"

    @pytest.mark.asyncio
    async def test_publish_completed_failure_does_not_propagate(self):
        service, _, stream, _ = make_service()
        _patch_crawler(
            service, AsyncMock(side_effect=RuntimeError("unexpected"))
        )
        stream.publish.side_effect = RuntimeError("redis dead")

        # Both crawler.run AND stream publish blow up — execute must still not raise
        await service.execute("exec-1", CrawlSourceName.HKEX, None)
