import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.config import CrawlSourceConfig
from app.crawler.base_crawler import (
    BaseCrawler,
    CrawlFailItem,
    CrawlResult,
    CrawlSuccessItem,
)
from app.crawler.browser_manager import BrowserManager
from app.crawler.exceptions import CrawlBlockedError, CrawlFatalError, CrawlNetworkError
from app.crawler.page_crawler import PageCrawler
from app.crawler.pdf_parser import PdfEncryptedError, PdfParseError, parse_pdf
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
                rows = await self._collect_rows(browser_manager, target_date)
            except CrawlFatalError:
                raise
            except Exception as exc:
                logger.exception("[hkex_crawler] Phase 1 failed")
                raise CrawlFatalError(
                    f"HKEX Phase 1 (playwright pagination) failed: {exc}"
                ) from exc
        finally:
            await browser_manager.stop()

        logger.info("[hkex_crawler] Phase 1 collected %d rows", len(rows))

        await self._fetch_pdfs(rows, result)
        logger.info(
            "[hkex_crawler] Completed: %d successes, %d failures",
            len(result.successes),
            len(result.failures),
        )
        return result

    # --------------------------------------------------------- Phase 1

    async def _collect_rows(
        self,
        browser_manager: BrowserManager,
        target_date: date,
    ) -> list[dict]:
        url = HKEX_SEARCH_URL_TEMPLATE.format(date=target_date.strftime("%Y%m%d"))
        context = await browser_manager.acquire_context()
        try:
            page = await context.new_page()
            logger.info("[hkex_crawler] Loading search page %s", url)
            await page.goto(url, wait_until="domcontentloaded")

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
                await page.click(LOAD_MORE_SELECTOR)
                await page.wait_for_load_state("domcontentloaded")
            else:
                logger.warning(
                    "[hkex_crawler] Reached MAX_LOAD_MORE_CLICKS=%d without termination",
                    MAX_LOAD_MORE_CLICKS,
                )

            row_elements = await page.query_selector_all(ROW_SELECTOR)
            rows: list[dict] = []
            for row in row_elements:
                parsed = await self._extract_row(row)
                if parsed is not None:
                    rows.append(parsed)
            return rows
        finally:
            await browser_manager.release_context(context)

    async def _extract_row(self, row) -> Optional[dict]:
        pdf_el = await row.query_selector(PDF_LINK_SELECTOR)
        if pdf_el is None:
            return None
        href = await pdf_el.get_attribute("href")
        if not href:
            return None
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

        return {
            "pdf_url": pdf_url,
            "title": title,
            "published_at": published_at,
            "stock_codes": stock_codes,
        }

    # --------------------------------------------------------- Phase 2

    async def _fetch_pdfs(self, rows: list[dict], result: CrawlResult) -> None:
        if not rows:
            return
        semaphore = asyncio.Semaphore(self.source_config.max_concurrent)

        async def worker(row: dict) -> None:
            pdf_url = row["pdf_url"]
            if await self._is_url_in_error_log(pdf_url):
                logger.info("[hkex_crawler] Skipping URL in error log: %s", pdf_url)
                return
            async with semaphore:
                await self._fetch_one_pdf(row, result)

        await asyncio.gather(*(worker(row) for row in rows))

    async def _fetch_one_pdf(self, row: dict, result: CrawlResult) -> None:
        pdf_url = row["pdf_url"]
        max_retry = self.page_crawler._config.max_retry
        try:
            response = await self.page_crawler.fetch(pdf_url)
        except CrawlBlockedError as exc:
            code = "HTTP_403" if "403" in str(exc) else "HTTP_404"
            logger.warning("[hkex_crawler] Blocked %s: %s", pdf_url, exc)
            result.failures.append(
                CrawlFailItem(
                    source_url=pdf_url,
                    error_type="NETWORK",
                    error_code=code,
                    attempt_count=1,
                )
            )
            return
        except CrawlNetworkError as exc:
            logger.warning("[hkex_crawler] Network error %s: %s", pdf_url, exc)
            result.failures.append(
                CrawlFailItem(
                    source_url=pdf_url,
                    error_type="NETWORK",
                    error_code="NETWORK_ERROR",
                    attempt_count=max_retry,
                )
            )
            return

        try:
            body = await parse_pdf(response.content)
        except PdfEncryptedError as exc:
            logger.warning("[hkex_crawler] Encrypted PDF %s: %s", pdf_url, exc)
            result.failures.append(
                CrawlFailItem(
                    source_url=pdf_url,
                    error_type="PARSE",
                    error_code="PDF_ENCRYPTED",
                    attempt_count=1,
                )
            )
            return
        except PdfParseError as exc:
            logger.warning("[hkex_crawler] PDF parse error %s: %s", pdf_url, exc)
            result.failures.append(
                CrawlFailItem(
                    source_url=pdf_url,
                    error_type="PARSE",
                    error_code="PDF_PARSE_ERROR",
                    attempt_count=1,
                )
            )
            return
        except Exception as exc:
            logger.exception("[hkex_crawler] Unexpected PDF parse failure %s", pdf_url)
            result.failures.append(
                CrawlFailItem(
                    source_url=pdf_url,
                    error_type="PARSE",
                    error_code="PARSE_ERROR",
                    attempt_count=1,
                )
            )
            return

        if not body or not body.strip():
            logger.warning("[hkex_crawler] PDF body empty %s", pdf_url)
            result.failures.append(
                CrawlFailItem(
                    source_url=pdf_url,
                    error_type="PARSE",
                    error_code="EMPTY_BODY",
                    attempt_count=1,
                )
            )
            return

        result.successes.append(
            CrawlSuccessItem(
                title=row["title"],
                body=body.strip(),
                source_url=pdf_url,
                published_at=row["published_at"],
                extra_metadata={"stock_code": row["stock_codes"]},
            )
        )

    # --------------------------------------------------------- helpers

    @staticmethod
    def _parse_release_time(raw: str) -> datetime:
        """
        Parse 'DD/MM/YYYY HH:mm' (HKT) → UTC datetime.
        Raises ValueError if the format does not match.
        """
        hkt = datetime.strptime(raw, "%d/%m/%Y %H:%M")
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
