import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.config import CrawlSourceConfig
from app.crawl.crawlers.base_crawler import (
    BaseCrawler,
    CrawlFailItem,
    CrawlResult,
    CrawlSuccessItem,
)
from app.crawl.fetchers.browser_manager import BrowserManager
from app.common.error_codes import CrawlErrorCode, DocumentParseErrorCode
from app.crawl.exceptions import CrawlBlockedException, CrawlFatalException, CrawlRateLimitedException
from app.crawl.fetchers.page_crawler import PageCrawler
from app.crawl.parsers.pdf_parser import PdfEncryptedException, PdfParseException, parse_pdf
from app.db.connection import DatabaseClient

logger = logging.getLogger(__name__)


HKEX_BASE_URL = "https://www1.hkexnews.hk"
HKEX_SEARCH_URL_TEMPLATE = (
    "https://www1.hkexnews.hk/search/titlesearch.xhtml"
    "?lang=EN&category=0&market=SEHK&searchType=0&stockId="
    "&from={date}&to={date}&title="
)

PAGINATION_SELECTOR = ".component-loadmore-leftPart__container"
LOAD_MORE_SELECTOR = "a.component-loadmore__link"
ROW_SELECTOR = "table tbody tr"
PDF_LINK_SELECTOR = "td > div.doc-link > a[href$='.pdf']"
HEADLINE_SELECTOR = "td > div.headline"
RELEASE_TIME_SELECTOR = "td.release-time"
STOCK_CODE_SELECTOR = "td.stock-short-code"

MAX_LOAD_MORE_CLICKS = 30  # safety upper bound; ~14 expected in practice


@dataclass
class HKEXAnnouncement:
    """One HKEXnews disclosure entry parsed from the filings table."""

    pdf_url: str
    title: str
    published_at: Optional[datetime]
    stock_codes: list[str]


class HKEXCrawler(BaseCrawler):
    """
    HKEX crawler — two phases:
    Phase 1: Sequential playwright LOAD MORE pagination on titlesearch.xhtml.
    Phase 2: Parallel PDF fetch via httpx + parsing.
    """

    def __init__(
        self,
        source_config: CrawlSourceConfig,
        page_crawler: PageCrawler,
        db: DatabaseClient,
        crawl_date: Optional[date] = None,
    ) -> None:
        super().__init__(source_config, page_crawler, db, crawl_date)

    # ------------------------------------------------------------------ run

    async def run(self) -> CrawlResult:
        target_date = self.crawl_date or datetime.now(timezone.utc).date()
        logger.info("[hkex_crawler] Starting crawl for date=%s", target_date)

        result = CrawlResult()
        browser_manager = BrowserManager()
        await browser_manager.start()
        try:
            try:
                announcements = await self._collect_announcements(browser_manager, target_date)
            except CrawlFatalException:
                raise
            except Exception as exc:
                logger.exception("[hkex_crawler] Phase 1 failed")
                raise CrawlFatalException(
                    f"HKEX Phase 1 (playwright pagination) failed: {exc}"
                ) from exc
        finally:
            await browser_manager.stop()

        logger.info(
            "[hkex_crawler] Phase 1 collected %d announcements", len(announcements)
        )

        await self._fetch_pdfs(announcements, result)
        logger.info(
            "[hkex_crawler] Completed: %d successes, %d failures",
            len(result.successes),
            len(result.failures),
        )
        return result

    # --------------------------------------------------------- Phase 1

    async def _collect_announcements(
        self,
        browser_manager: BrowserManager,
        target_date: date,
    ) -> list[HKEXAnnouncement]:
        url = HKEX_SEARCH_URL_TEMPLATE.format(date=target_date.strftime("%Y%m%d"))
        nav_timeout_ms = self.page_crawler._config.browser_navigation_timeout_ms
        context = await browser_manager.acquire_context()
        try:
            page = await context.new_page()
            logger.info("[hkex_crawler] Loading search page %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)

            for click_idx in range(MAX_LOAD_MORE_CLICKS):
                pagination_text = await page.text_content(PAGINATION_SELECTOR) or ""
                showing, total = self._parse_pagination_counts(pagination_text)
                logger.debug(
                    "[hkex_crawler] Pagination state: showing=%d total=%d", showing, total
                )
                if showing >= total:
                    logger.info("[hkex_crawler] All %d rows visible", total)
                    break
                logger.debug("[hkex_crawler] Clicking LOAD MORE (#%d)", click_idx + 1)
                await page.click(LOAD_MORE_SELECTOR, timeout=nav_timeout_ms)
                # LOAD MORE fires an XHR invisible to load/domcontentloaded events.
                # Poll the pagination counter until it advances past the pre-click
                # value — otherwise the next iteration reads stale text and re-clicks
                # a detached button mid-rerender, hanging until timeout.
                await page.wait_for_function(
                    """
                    ({selector, prev}) => {
                        const el = document.querySelector(selector);
                        if (!el) return false;
                        const m = (el.textContent || '').match(/(\\d+)\\D+(\\d+)/);
                        if (!m) return false;
                        return parseInt(m[1], 10) > prev;
                    }
                    """,
                    arg={"selector": PAGINATION_SELECTOR, "prev": showing},
                    timeout=nav_timeout_ms,
                )
            else:
                logger.warning(
                    "[hkex_crawler] Reached MAX_LOAD_MORE_CLICKS=%d without termination",
                    MAX_LOAD_MORE_CLICKS,
                )

            row_elements = await page.query_selector_all(ROW_SELECTOR)
            announcements: list[HKEXAnnouncement] = []
            drop_reasons: Counter[str] = Counter()
            for row in row_elements:
                parsed, reason = await self._extract_row(row)
                if parsed is not None:
                    announcements.append(parsed)
                else:
                    drop_reasons[reason] += 1
            logger.info(
                "[hkex_crawler] Phase 1 row buckets: total=%d parsed=%d drops=%s",
                len(row_elements),
                len(announcements),
                dict(drop_reasons),
            )
            return announcements
        finally:
            await browser_manager.release_context(context)

    async def _extract_row(
        self, row
    ) -> tuple[Optional[HKEXAnnouncement], str]:
        """
        Returns (announcement | None, reason). Reason is one of:
        "ok", "no_pdf_link", "no_href". On a drop, logs the row's inner HTML
        at DEBUG so dropped rows can be spot-checked.
        """
        pdf_el = await row.query_selector(PDF_LINK_SELECTOR)
        if pdf_el is None:
            if logger.isEnabledFor(logging.DEBUG):
                try:
                    html = await row.inner_html()
                except Exception:
                    html = "<inner_html unavailable>"
                logger.debug(
                    "[hkex_crawler] Drop row (no_pdf_link); html[:200]=%s",
                    html[:200],
                )
            return None, "no_pdf_link"
        href = await pdf_el.get_attribute("href")
        if not href:
            if logger.isEnabledFor(logging.DEBUG):
                try:
                    html = await row.inner_html()
                except Exception:
                    html = "<inner_html unavailable>"
                logger.debug(
                    "[hkex_crawler] Drop row (no_href); html[:200]=%s",
                    html[:200],
                )
            return None, "no_href"
        pdf_url = href if href.startswith("http") else HKEX_BASE_URL + href

        headline_el = await row.query_selector(HEADLINE_SELECTOR)
        title = (await headline_el.text_content() if headline_el else "") or ""
        title = title.strip()

        release_el = await row.query_selector(RELEASE_TIME_SELECTOR)
        release_raw = (await release_el.text_content() if release_el else "") or ""
        try:
            published_at = self._parse_release_time(release_raw.strip())
        except ValueError:
            logger.warning(
                "[hkex_crawler] Failed to parse release time '%s' for %s",
                release_raw,
                pdf_url,
            )
            published_at = None

        stock_el = await row.query_selector(STOCK_CODE_SELECTOR)
        stock_raw = (await stock_el.text_content() if stock_el else "") or ""
        stock_codes = self._parse_stock_codes(stock_raw)

        return (
            HKEXAnnouncement(
                pdf_url=pdf_url,
                title=title,
                published_at=published_at,
                stock_codes=stock_codes,
            ),
            "ok",
        )

    # --------------------------------------------------------- Phase 2

    async def _fetch_pdfs(
        self, announcements: list[HKEXAnnouncement], result: CrawlResult
    ) -> None:
        if not announcements:
            return
        semaphore = asyncio.Semaphore(self.source_config.max_concurrent)

        async def worker(announcement: HKEXAnnouncement) -> None:
            if await self._is_url_in_error_log(announcement.pdf_url):
                logger.info(
                    "[hkex_crawler] Skipping URL in error log: %s", announcement.pdf_url
                )
                return
            async with semaphore:
                await self._fetch_one_pdf(announcement, result)

        await asyncio.gather(*(worker(a) for a in announcements))

    async def _fetch_one_pdf(
        self, announcement: HKEXAnnouncement, result: CrawlResult
    ) -> None:
        pdf_url = announcement.pdf_url
        max_retry = self.page_crawler._config.max_retry
        try:
            response = await self.page_crawler.fetch(pdf_url)
        except CrawlRateLimitedException as exc:
            logger.warning("[hkex_crawler] Rate limited %s: %s", pdf_url, exc)
            return
        except CrawlBlockedException as exc:
            logger.warning("[hkex_crawler] Fetch failed %s: %s", pdf_url, exc)
            result.failures.append(
                CrawlFailItem(
                    source_url=pdf_url,
                    error_type=CrawlErrorCode.URL_GET_FAILED.error_type,
                    error_code=CrawlErrorCode.URL_GET_FAILED.error_code,
                    attempt_count=max_retry,
                )
            )
            return

        try:
            body = await parse_pdf(response.content)
        except PdfEncryptedException as exc:
            logger.warning("[hkex_crawler] Encrypted PDF %s: %s", pdf_url, exc)
            result.failures.append(
                CrawlFailItem(
                    source_url=pdf_url,
                    error_type=DocumentParseErrorCode.PDF_ENCRYPTED.error_type,
                    error_code=DocumentParseErrorCode.PDF_ENCRYPTED.error_code,
                    attempt_count=1,
                )
            )
            return
        except PdfParseException as exc:
            logger.warning("[hkex_crawler] PDF parse error %s: %s", pdf_url, exc)
            result.failures.append(
                CrawlFailItem(
                    source_url=pdf_url,
                    error_type=DocumentParseErrorCode.PDF_PARSE_ERROR.error_type,
                    error_code=DocumentParseErrorCode.PDF_PARSE_ERROR.error_code,
                    attempt_count=1,
                )
            )
            return
        except Exception as exc:
            logger.exception("[hkex_crawler] Unexpected PDF parse failure %s", pdf_url)
            result.failures.append(
                CrawlFailItem(
                    source_url=pdf_url,
                    error_type=DocumentParseErrorCode.PARSE_ERROR.error_type,
                    error_code=DocumentParseErrorCode.PARSE_ERROR.error_code,
                    attempt_count=1,
                )
            )
            return

        if not body or not body.strip():
            logger.warning("[hkex_crawler] Empty PDF body for %s — skipping", pdf_url)
            return

        result.successes.append(
            CrawlSuccessItem(
                title=announcement.title,
                body=body.strip(),
                source_url=pdf_url,
                published_at=announcement.published_at,
                extra_metadata={"stock_code": announcement.stock_codes},
            )
        )

    # --------------------------------------------------------- helpers

    _RELEASE_TIME_PATTERN = re.compile(r"(\d{2}/\d{2}/\d{4} \d{2}:\d{2})")

    @classmethod
    def _parse_release_time(cls, raw: str) -> datetime:
        """
        Parse 'DD/MM/YYYY HH:mm' (HKT) → UTC datetime. The cell text may be
        wrapped in a label prefix like 'Release Time: 14/04/2026 22:52' on
        live pages, so we regex-extract the datetime substring first.
        Raises ValueError if no timestamp pattern is found.
        """
        match = cls._RELEASE_TIME_PATTERN.search(raw)
        if match is None:
            raise ValueError(f"No timestamp pattern in {raw!r}")
        hkt = datetime.strptime(match.group(1), "%d/%m/%Y %H:%M")
        # HKT is UTC+8; convert to UTC
        utc = hkt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
        return utc

    @staticmethod
    def _parse_pagination_counts(text: str) -> tuple[int, int]:
        """
        Extract (showing, total) from a label like 'Showing 1471 of 1471 records'.
        Raises ValueError if the regex does not match.
        """
        match = re.search(r"(\d+)\D+(\d+)", text)
        if match is None:
            raise ValueError(f"Cannot parse pagination text: {text!r}")
        return int(match.group(1)), int(match.group(2))

    @staticmethod
    def _parse_stock_codes(raw: str) -> list[str]:
        """
        Split a stock-code cell into a list of codes. Cells may contain a single
        code, comma-separated codes, or whitespace-separated codes. Empty input
        returns [].
        """
        if not raw:
            return []
        tokens = re.split(r"[\s,]+", raw.strip())
        return [t for t in tokens if t]
