import asyncio
import logging
import random
from datetime import date, datetime
from typing import Optional

from app.config import CrawlSourceConfig
from app.crawl.crawlers.base_crawler import (
    BaseCrawler,
    CrawlFailItem,
    CrawlResult,
    CrawlSuccessItem,
)
from app.common.error_codes import CrawlErrorCode, DocumentParseErrorCode
from app.crawl.exceptions import CrawlBlockedException, CrawlFatalException, CrawlRateLimitedException
from app.crawl.fetchers.feed_fetcher import FeedEntry, FeedFetchException, fetch_rss
from app.crawl.parsers.html_parser import extract_body_auto
from app.crawl.fetchers.page_crawler import PageCrawler
from app.db.connection import DatabaseClient

logger = logging.getLogger(__name__)


YAHOO_HK_RSS_URL = "https://hk.finance.yahoo.com/news/rssindex"
YAHOO_HK_URL_PREFIX_FILTER = "hk.finance.yahoo.com/news/"


class YahooHKCrawler(BaseCrawler):
    """
    Yahoo HK crawler — RSS discovery + httpx + trafilatura body extraction.
    Filters ad URLs from the feed and emits a coverage gap warning when the
    oldest entry is newer than the previous successful crawl.
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
        logger.info("[yahoo_hk_crawler] Starting crawl")
        try:
            entries = await fetch_rss(YAHOO_HK_RSS_URL)
        except FeedFetchException as exc:
            logger.exception("[yahoo_hk_crawler] RSS fetch failed")
            raise CrawlFatalException(f"Yahoo HK RSS fetch failed: {exc}") from exc

        filtered = [e for e in entries if YAHOO_HK_URL_PREFIX_FILTER in e.url]
        logger.info(
            "[yahoo_hk_crawler] RSS returned %d entries; %d after URL filter",
            len(entries),
            len(filtered),
        )

        await self._check_coverage_gap(filtered)

        result = CrawlResult()
        if not filtered:
            return result

        await self._crawl_articles(filtered, result)
        logger.info(
            "[yahoo_hk_crawler] Completed: %d successes, %d failures",
            len(result.successes),
            len(result.failures),
        )
        return result

    # --------------------------------------------------------- coverage gap

    async def _check_coverage_gap(self, entries: list[FeedEntry]) -> None:
        if not entries:
            return
        timestamps = [e.published_at for e in entries if e.published_at is not None]
        if not timestamps:
            return
        oldest = min(timestamps)
        previous = await self._get_previous_crawl_time()
        if previous is None:
            return
        if oldest > previous:
            logger.warning(
                "[yahoo_hk_crawler] Potential coverage gap: oldest entry %s "
                "is newer than previous crawl %s — older entries may have "
                "dropped off the RSS feed",
                oldest.isoformat(),
                previous.isoformat(),
            )

    async def _get_previous_crawl_time(self) -> Optional[datetime]:
        """
        Look up the most recent raw_news.created_at for YAHOO_HK.
        Returns None if no records exist.
        """
        row = await self.db.fetch_one(
            "SELECT MAX(created_at) AS max_created_at FROM raw_news WHERE source_name = $1",
            "YAHOO_HK",
        )
        if row is None:
            return None
        return row["max_created_at"]

    # --------------------------------------------------------- workers

    async def _crawl_articles(
        self, entries: list[FeedEntry], result: CrawlResult
    ) -> None:
        semaphore = asyncio.Semaphore(self.source_config.max_concurrent)

        async def worker(entry: FeedEntry) -> None:
            if await self._is_url_in_error_log(entry.url):
                logger.info(
                    "[yahoo_hk_crawler] Skipping URL in error log: %s", entry.url
                )
                return
            async with semaphore:
                await self._fetch_one_article(entry, result)

        await asyncio.gather(*(worker(e) for e in entries))

    async def _fetch_one_article(
        self, entry: FeedEntry, result: CrawlResult
    ) -> None:
        url = entry.url
        max_retry = self.page_crawler.config.max_retry
        jitter_s = random.uniform(
            self.source_config.request_interval_min_ms / 1000,
            self.source_config.request_interval_max_ms / 1000,
        )
        await asyncio.sleep(jitter_s)
        try:
            response = await self.page_crawler.fetch(url)
        except CrawlRateLimitedException as exc:
            logger.warning("[yahoo_hk_crawler] Rate limited %s: %s", url, exc)
            return
        except CrawlBlockedException as exc:
            logger.warning("[yahoo_hk_crawler] Fetch failed %s: %s", url, exc)
            result.failures.append(
                CrawlFailItem(
                    source_url=url,
                    error_type=CrawlErrorCode.URL_GET_FAILED.error_type,
                    error_code=CrawlErrorCode.URL_GET_FAILED.error_code,
                    attempt_count=max_retry,
                )
            )
            return

        try:
            body = extract_body_auto(response.text)
        except Exception as exc:
            logger.exception("[yahoo_hk_crawler] body extraction failed for %s: %s", url, exc)
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
            logger.warning("[yahoo_hk_crawler] Empty body for %s — skipping", url)
            return

        result.successes.append(
            CrawlSuccessItem(
                title=entry.title,
                body=body.strip(),
                source_url=url,
                published_at=entry.published_at,
            )
        )
