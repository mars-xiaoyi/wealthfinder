from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import CrawlConfig, CrawlSourceConfig
from app.common.error_codes import DocumentParseErrorCode, NetworkErrorCode
from app.crawl.exceptions import CrawlBlockedError, CrawlFatalError, CrawlNetworkError
from app.crawl.crawlers.hkex_crawler import HKEXAnnouncement, HKEXCrawler
from app.crawl.parsers.pdf_parser import PdfEncryptedError, PdfParseError


def make_source_config(max_concurrent: int = 5) -> CrawlSourceConfig:
    return CrawlSourceConfig(
        max_concurrent=max_concurrent,
        request_interval_min_ms=0,
        request_interval_max_ms=0,
    )


def make_page_crawler(max_retry: int = 3) -> MagicMock:
    pc = MagicMock()
    pc._config = CrawlConfig(
        max_retry=max_retry,
        retry_base_wait_ms=10,
        request_timeout_s=10,
        browser_navigation_timeout_ms=15000,
        crawl_sources={},
    )
    pc.fetch = AsyncMock()
    return pc


def make_db(in_error_log: bool = False) -> MagicMock:
    db = MagicMock()
    db.fetch_one = AsyncMock(return_value={"exists": in_error_log})
    return db


def make_crawler(
    page_crawler=None,
    db=None,
    crawl_date: date | None = None,
    max_concurrent: int = 5,
) -> HKEXCrawler:
    return HKEXCrawler(
        source_config=make_source_config(max_concurrent),
        page_crawler=page_crawler or make_page_crawler(),
        db=db or make_db(),
        crawl_date=crawl_date or date(2026, 4, 2),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class TestParseReleaseTime:
    def test_converts_hkt_to_utc(self):
        result = HKEXCrawler._parse_release_time("02/04/2026 22:59")
        # 22:59 HKT = 14:59 UTC same day
        assert result == datetime(2026, 4, 2, 14, 59, tzinfo=timezone.utc)

    def test_strips_release_time_label_prefix(self):
        # Live HKEX cells render as ' Release Time: 14/04/2026 22:52' — the
        # label must be tolerated, not rejected.
        result = HKEXCrawler._parse_release_time(" Release Time: 14/04/2026 22:52")
        assert result == datetime(2026, 4, 14, 14, 52, tzinfo=timezone.utc)

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            HKEXCrawler._parse_release_time("not a date")

    def test_no_timestamp_pattern_raises(self):
        with pytest.raises(ValueError, match="No timestamp pattern"):
            HKEXCrawler._parse_release_time("Release Time:")


class TestParsePaginationCounts:
    def test_equal_counts(self):
        assert HKEXCrawler._parse_pagination_counts("Showing 1471 of 1471 records") == (
            1471,
            1471,
        )

    def test_unequal_counts(self):
        assert HKEXCrawler._parse_pagination_counts("Showing 100 of 1471 records") == (
            100,
            1471,
        )

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            HKEXCrawler._parse_pagination_counts("nothing here")


class TestParseStockCodes:
    def test_single_code(self):
        assert HKEXCrawler._parse_stock_codes("00700") == ["00700"]

    def test_comma_separated(self):
        assert HKEXCrawler._parse_stock_codes("00700, 00388") == ["00700", "00388"]

    def test_whitespace_separated(self):
        assert HKEXCrawler._parse_stock_codes("00700\n00388") == ["00700", "00388"]

    def test_empty(self):
        assert HKEXCrawler._parse_stock_codes("") == []
        assert HKEXCrawler._parse_stock_codes("   ") == []


# ---------------------------------------------------------------------------
# Phase 2 — _fetch_one_pdf branches
# ---------------------------------------------------------------------------

def _announcement(
    pdf_url: str = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0402/test.pdf",
) -> HKEXAnnouncement:
    return HKEXAnnouncement(
        pdf_url=pdf_url,
        title="Sample Title",
        published_at=datetime(2026, 4, 2, 14, 59, tzinfo=timezone.utc),
        stock_codes=["00700"],
    )


class TestFetchOnePdf:
    @pytest.mark.asyncio
    async def test_success(self):
        pc = make_page_crawler()
        response = MagicMock()
        response.content = b"%PDF-1.4 fake"
        pc.fetch.return_value = response
        crawler = make_crawler(page_crawler=pc)

        from app.crawl.crawlers.base_crawler import CrawlResult

        result = CrawlResult()
        with patch(
            "app.crawl.crawlers.hkex_crawler.parse_pdf",
            new=AsyncMock(return_value="extracted body"),
        ):
            await crawler._fetch_one_pdf(_announcement(), result)

        assert len(result.successes) == 1
        assert len(result.failures) == 0
        s = result.successes[0]
        assert s.title == "Sample Title"
        assert s.body == "extracted body"
        assert s.extra_metadata == {"stock_code": ["00700"]}

    @pytest.mark.asyncio
    async def test_blocked_403(self):
        pc = make_page_crawler()
        pc.fetch.side_effect = CrawlBlockedError("HTTP 403 for x")
        crawler = make_crawler(page_crawler=pc)
        from app.crawl.crawlers.base_crawler import CrawlResult

        result = CrawlResult()
        await crawler._fetch_one_pdf(_announcement(), result)

        assert len(result.failures) == 1
        f = result.failures[0]
        assert f.error_type == NetworkErrorCode.HTTP_403.error_type
        assert f.error_code == NetworkErrorCode.HTTP_403.error_code

    @pytest.mark.asyncio
    async def test_network_error_uses_max_retry(self):
        pc = make_page_crawler(max_retry=4)
        pc.fetch.side_effect = CrawlNetworkError("retries exhausted")
        crawler = make_crawler(page_crawler=pc)
        from app.crawl.crawlers.base_crawler import CrawlResult

        result = CrawlResult()
        await crawler._fetch_one_pdf(_announcement(), result)

        assert result.failures[0].error_code == NetworkErrorCode.NETWORK_ERROR.error_code
        assert result.failures[0].attempt_count == 4

    @pytest.mark.asyncio
    async def test_pdf_encrypted(self):
        pc = make_page_crawler()
        response = MagicMock()
        response.content = b"%PDF"
        pc.fetch.return_value = response
        crawler = make_crawler(page_crawler=pc)
        from app.crawl.crawlers.base_crawler import CrawlResult

        result = CrawlResult()
        with patch(
            "app.crawl.crawlers.hkex_crawler.parse_pdf",
            new=AsyncMock(side_effect=PdfEncryptedError("encrypted")),
        ):
            await crawler._fetch_one_pdf(_announcement(), result)

        assert result.failures[0].error_code == DocumentParseErrorCode.PDF_ENCRYPTED.error_code
        assert result.failures[0].error_type == DocumentParseErrorCode.PDF_ENCRYPTED.error_type

    @pytest.mark.asyncio
    async def test_pdf_parse_error(self):
        pc = make_page_crawler()
        pc.fetch.return_value = MagicMock(content=b"%PDF")
        crawler = make_crawler(page_crawler=pc)
        from app.crawl.crawlers.base_crawler import CrawlResult

        result = CrawlResult()
        with patch(
            "app.crawl.crawlers.hkex_crawler.parse_pdf",
            new=AsyncMock(side_effect=PdfParseError("bad")),
        ):
            await crawler._fetch_one_pdf(_announcement(), result)

        assert result.failures[0].error_code == DocumentParseErrorCode.PDF_PARSE_ERROR.error_code

    @pytest.mark.asyncio
    async def test_empty_body_skips_without_failure(self):
        pc = make_page_crawler()
        pc.fetch.return_value = MagicMock(content=b"%PDF")
        crawler = make_crawler(page_crawler=pc)
        from app.crawl.crawlers.base_crawler import CrawlResult

        result = CrawlResult()
        with patch(
            "app.crawl.crawlers.hkex_crawler.parse_pdf",
            new=AsyncMock(return_value="   "),
        ):
            await crawler._fetch_one_pdf(_announcement(), result)

        assert result.successes == []
        assert result.failures == []


# ---------------------------------------------------------------------------
# _fetch_pdfs — pre-check skip
# ---------------------------------------------------------------------------

class TestFetchPdfs:
    @pytest.mark.asyncio
    async def test_skips_url_in_error_log(self):
        pc = make_page_crawler()
        crawler = make_crawler(
            page_crawler=pc,
            db=make_db(in_error_log=True),
        )
        from app.crawl.crawlers.base_crawler import CrawlResult

        result = CrawlResult()
        await crawler._fetch_pdfs([_announcement()], result)

        pc.fetch.assert_not_called()
        assert result.successes == []
        assert result.failures == []

    @pytest.mark.asyncio
    async def test_processes_url_not_in_error_log(self):
        pc = make_page_crawler()
        pc.fetch.return_value = MagicMock(content=b"%PDF")
        crawler = make_crawler(
            page_crawler=pc,
            db=make_db(in_error_log=False),
        )
        from app.crawl.crawlers.base_crawler import CrawlResult

        result = CrawlResult()
        with patch(
            "app.crawl.crawlers.hkex_crawler.parse_pdf",
            new=AsyncMock(return_value="body"),
        ):
            await crawler._fetch_pdfs([_announcement()], result)

        assert len(result.successes) == 1


# ---------------------------------------------------------------------------
# _collect_announcements — Phase 1 pagination flow
# ---------------------------------------------------------------------------

def _make_fake_row(
    pdf_href: str | None,
    headline: str = "",
    release: str = "",
    stock: str = "",
    *,
    inner_html: str = "<td>stub</td>",
):
    """
    Build a playwright-row stub. Pass pdf_href=None to simulate a row with no
    `.doc-link` PDF anchor; pass pdf_href="" to simulate an anchor with an
    empty href attribute.
    """
    pdf_el: AsyncMock | None
    if pdf_href is None:
        pdf_el = None
    else:
        pdf_el = AsyncMock()
        pdf_el.get_attribute = AsyncMock(return_value=pdf_href)

    headline_el = AsyncMock()
    headline_el.text_content = AsyncMock(return_value=headline)

    release_el = AsyncMock()
    release_el.text_content = AsyncMock(return_value=release)

    stock_el = AsyncMock()
    stock_el.text_content = AsyncMock(return_value=stock)

    async def query_selector(sel):
        if "doc-link" in sel:
            return pdf_el
        if "headline" in sel:
            return headline_el
        if "release-time" in sel:
            return release_el
        if "stock-short-code" in sel:
            return stock_el
        return None

    row = MagicMock()
    row.query_selector = AsyncMock(side_effect=query_selector)
    row.inner_html = AsyncMock(return_value=inner_html)
    return row


def _make_fake_page(pagination_states: list[str], rows: list):
    page = MagicMock()
    page.goto = AsyncMock()
    page.click = AsyncMock()
    page.wait_for_function = AsyncMock()
    page.text_content = AsyncMock(side_effect=pagination_states)
    page.query_selector_all = AsyncMock(return_value=rows)
    return page


def _make_fake_browser_manager(page) -> MagicMock:
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    bm = MagicMock()
    bm.start = AsyncMock()
    bm.stop = AsyncMock()
    bm.acquire_context = AsyncMock(return_value=context)
    bm.release_context = AsyncMock()
    return bm


class TestExtractRow:
    @pytest.mark.asyncio
    async def test_ok_row_returns_announcement(self):
        row = _make_fake_row(
            pdf_href="/listedco/test.pdf",
            headline="Title",
            release="02/04/2026 22:59",
            stock="00700",
        )
        crawler = make_crawler()

        parsed, reason = await crawler._extract_row(row)

        assert reason == "ok"
        assert parsed is not None
        assert parsed.pdf_url == "https://www1.hkexnews.hk/listedco/test.pdf"
        assert parsed.published_at == datetime(2026, 4, 2, 14, 59, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_drops_row_without_pdf_link(self):
        row = _make_fake_row(pdf_href=None)
        crawler = make_crawler()

        parsed, reason = await crawler._extract_row(row)

        assert parsed is None
        assert reason == "no_pdf_link"

    @pytest.mark.asyncio
    async def test_drops_row_with_empty_href(self):
        row = _make_fake_row(pdf_href="")
        crawler = make_crawler()

        parsed, reason = await crawler._extract_row(row)

        assert parsed is None
        assert reason == "no_href"

    @pytest.mark.asyncio
    async def test_ok_row_with_unparseable_release_time_keeps_announcement(self):
        row = _make_fake_row(
            pdf_href="/a.pdf",
            headline="Title",
            release="bogus date",
            stock="00700",
        )
        crawler = make_crawler()

        parsed, reason = await crawler._extract_row(row)

        assert reason == "ok"
        assert parsed is not None
        assert parsed.published_at is None


class TestCollectAnnouncements:
    @pytest.mark.asyncio
    async def test_terminates_on_equal_counts(self):
        row = _make_fake_row(
            pdf_href="/listedco/test.pdf",
            headline="Title",
            release="02/04/2026 22:59",
            stock="00700",
        )
        page = _make_fake_page(
            pagination_states=["Showing 2 of 2 records"], rows=[row]
        )
        bm = _make_fake_browser_manager(page)
        crawler = make_crawler()

        announcements = await crawler._collect_announcements(bm, date(2026, 4, 2))

        assert len(announcements) == 1
        assert announcements[0].pdf_url.startswith("https://www1.hkexnews.hk/")
        assert announcements[0].stock_codes == ["00700"]
        page.click.assert_not_called()

    @pytest.mark.asyncio
    async def test_logs_row_bucket_summary(self, caplog):
        import logging as _logging

        ok_row = _make_fake_row(
            pdf_href="/a.pdf",
            headline="T",
            release="02/04/2026 10:00",
            stock="00700",
        )
        no_link_row = _make_fake_row(pdf_href=None)
        no_href_row = _make_fake_row(pdf_href="")
        page = _make_fake_page(
            pagination_states=["Showing 3 of 3 records"],
            rows=[ok_row, no_link_row, no_href_row],
        )
        bm = _make_fake_browser_manager(page)
        crawler = make_crawler()

        with caplog.at_level(_logging.INFO, logger="app.crawl.crawlers.hkex_crawler"):
            announcements = await crawler._collect_announcements(bm, date(2026, 4, 2))

        assert len(announcements) == 1
        summary = next(
            r.getMessage() for r in caplog.records if "row buckets" in r.getMessage()
        )
        assert "total=3" in summary
        assert "parsed=1" in summary
        assert "'no_pdf_link': 1" in summary
        assert "'no_href': 1" in summary

    @pytest.mark.asyncio
    async def test_clicks_load_more_until_complete(self):
        row = _make_fake_row(
            pdf_href="https://www1.hkexnews.hk/abs.pdf",
            headline="t",
            release="02/04/2026 10:00",
            stock="00001",
        )
        page = _make_fake_page(
            pagination_states=[
                "Showing 1 of 3 records",
                "Showing 2 of 3 records",
                "Showing 3 of 3 records",
            ],
            rows=[row],
        )
        bm = _make_fake_browser_manager(page)
        crawler = make_crawler()

        await crawler._collect_announcements(bm, date(2026, 4, 2))

        assert page.click.call_count == 2  # clicked twice before reaching 3 of 3
        # Each click must be followed by wait_for_function polling the counter;
        # domcontentloaded is not a reliable signal for the LOAD MORE XHR.
        assert page.wait_for_function.call_count == 2


# ---------------------------------------------------------------------------
# run() — top-level
# ---------------------------------------------------------------------------

class TestRun:
    @pytest.mark.asyncio
    async def test_phase1_failure_raises_fatal(self):
        crawler = make_crawler()
        bm = MagicMock()
        bm.start = AsyncMock()
        bm.stop = AsyncMock()
        bm.acquire_context = AsyncMock(side_effect=RuntimeError("playwright kaboom"))

        with patch("app.crawl.crawlers.hkex_crawler.BrowserManager", return_value=bm):
            with pytest.raises(CrawlFatalError, match="Phase 1"):
                await crawler.run()

        bm.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_full_run_happy_path(self):
        row = _make_fake_row(
            pdf_href="/listedco/test.pdf",
            headline="Title",
            release="02/04/2026 22:59",
            stock="00700",
        )
        page = _make_fake_page(
            pagination_states=["Showing 1 of 1 records"], rows=[row]
        )
        bm = _make_fake_browser_manager(page)

        pc = make_page_crawler()
        pc.fetch.return_value = MagicMock(content=b"%PDF")
        crawler = make_crawler(page_crawler=pc)

        with (
            patch("app.crawl.crawlers.hkex_crawler.BrowserManager", return_value=bm),
            patch(
                "app.crawl.crawlers.hkex_crawler.parse_pdf",
                new=AsyncMock(return_value="body content"),
            ),
        ):
            result = await crawler.run()

        assert len(result.successes) == 1
        assert result.successes[0].body == "body content"
        bm.start.assert_called_once()
        bm.stop.assert_called_once()
