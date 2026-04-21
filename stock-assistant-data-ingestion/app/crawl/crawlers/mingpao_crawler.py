import asyncio
import logging
import random
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from bs4 import BeautifulSoup

from app.config import CrawlSourceConfig
from app.crawl.crawlers.base_crawler import (
    BaseCrawler,
    CrawlFailItem,
    CrawlResult,
    CrawlSuccessItem,
)
from app.crawl.fetchers.browser_manager import BrowserManager
from app.common.error_codes import CrawlErrorCode, DocumentParseErrorCode
from app.crawl.exceptions import CrawlFatalException
from app.crawl.fetchers.feed_fetcher import FeedEntry, FeedFetchException, fetch_rss
from app.crawl.parsers.html_parser import extract_body_css
from app.crawl.fetchers.page_crawler import PageCrawler
from app.db.connection import DatabaseClient

logger = logging.getLogger(__name__)


MINGPAO_RSS_URL = "https://news.mingpao.com/rss/pns/s00004.xml"
ARTICLE_BODY_SELECTOR = "article"
# Article header date cell, e.g. "2026年4月15日 星期三　6:04AM". The sibling
# `div.date[itemprop="datePublished"]` only carries the date (no time), so
# we avoid it.
PUBLISHED_AT_SELECTOR = "div.date.color2nd"

# Fallback published_at pattern (e.g. "2026年4月6日 星期一 06:00AM")
_PUBLISHED_AT_PATTERN = re.compile(
    r"(\d{4})年(\d{1,2})月(\d{1,2})日.*?(\d{1,2}):(\d{2})\s*([AP])M",
    re.IGNORECASE,
)

_HKT = timezone(timedelta(hours=8))


class MingPaoCrawler(BaseCrawler):
    """
    MingPao crawler — RSS discovery + playwright headless browser fetch.
    httpx is blocked by TLS fingerprint detection, so playwright is required.
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
        logger.info("[mingpao_crawler] Starting crawl")
        try:
            entries = await fetch_rss(MINGPAO_RSS_URL)
        except FeedFetchException as exc:
            logger.exception("[mingpao_crawler] RSS fetch failed")
            raise CrawlFatalException(f"MingPao RSS fetch failed: {exc}") from exc

        logger.info("[mingpao_crawler] RSS returned %d entries", len(entries))

        result = CrawlResult()
        if not entries:
            return result

        browser_manager = BrowserManager()
        await browser_manager.start()
        try:
            context = await browser_manager.acquire_context()
            try:
                await self._crawl_articles(context, entries, result)
            finally:
                await browser_manager.release_context(context)
        finally:
            await browser_manager.stop()

        logger.info(
            "[mingpao_crawler] Completed: %d successes, %d failures",
            len(result.successes),
            len(result.failures),
        )
        return result

    async def _crawl_articles(
        self, context, entries: list[FeedEntry], result: CrawlResult
    ) -> None:
        semaphore = asyncio.Semaphore(self.source_config.max_concurrent)

        async def worker(entry: FeedEntry) -> None:
            if await self._is_url_in_error_log(entry.url):
                logger.info(
                    "[mingpao_crawler] Skipping URL in error log: %s", entry.url
                )
                return
            async with semaphore:
                await self._fetch_one_article(context, entry, result)

        await asyncio.gather(*(worker(e) for e in entries))

    async def _fetch_one_article(
        self, context, entry: FeedEntry, result: CrawlResult
    ) -> None:
        url = entry.url
        jitter_s = random.uniform(
            self.source_config.request_interval_min_ms / 1000,
            self.source_config.request_interval_max_ms / 1000,
        )
        await asyncio.sleep(jitter_s)
        try:
            page = await context.new_page()
            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.page_crawler.config.browser_navigation_timeout_ms,
                )
                html = await page.content()
            finally:
                await page.close()
        except Exception as exc:
            logger.warning("[mingpao_crawler] playwright fetch failed for %s: %s", url, exc)
            result.failures.append(
                CrawlFailItem(
                    source_url=url,
                    error_type=CrawlErrorCode.BROWSER_FETCH_FAILED.error_type,
                    error_code=CrawlErrorCode.BROWSER_FETCH_FAILED.error_code,
                    attempt_count=1,
                )
            )
            return

        try:
            body = extract_body_css(html, ARTICLE_BODY_SELECTOR)
        except Exception as exc:
            logger.exception("[mingpao_crawler] body extraction failed for %s: %s", url, exc)
            result.failures.append(
                CrawlFailItem(
                    source_url=url,
                    error_type=DocumentParseErrorCode.PARSE_ERROR.error_type,
                    error_code=DocumentParseErrorCode.PARSE_ERROR.error_code,
                    attempt_count=1,
                )
            )
            return

        if not body or not body.strip():
            logger.warning("[mingpao_crawler] Empty body for %s — skipping", url)
            return

        published_at = entry.published_at or self._extract_published_at_from_page(html)

        result.successes.append(
            CrawlSuccessItem(
                title=entry.title,
                body=body.strip(),
                source_url=url,
                published_at=published_at,
            )
        )

    # --------------------------------------------------------- helpers

    @staticmethod
    def _extract_published_at_from_page(page_html: str) -> Optional[datetime]:
        """
        Fallback parser — reads the article header date cell
        (`div.date.color2nd`, e.g. "2026年4月15日 星期三　6:04AM"). Returns UTC
        datetime, or None if the cell or timestamp pattern is missing.
        """
        soup = BeautifulSoup(page_html, "html.parser")
        date_div = soup.select_one(PUBLISHED_AT_SELECTOR)
        if date_div is None:
            return None
        match = _PUBLISHED_AT_PATTERN.search(date_div.get_text())
        if match is None:
            return None
        year, month, day, hour, minute, ampm = match.groups()
        hour_i = int(hour) % 12
        if ampm.upper() == "P":
            hour_i += 12
        hkt = datetime(
            int(year), int(month), int(day), hour_i, int(minute), tzinfo=_HKT
        )
        return hkt.astimezone(timezone.utc)
