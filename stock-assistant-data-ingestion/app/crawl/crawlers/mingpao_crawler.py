import logging
from datetime import date
from typing import Optional

from app.config import CrawlSourceConfig
from app.crawl.crawlers.base_crawler import (
    BaseCrawler,
    CrawlResult,
    CrawlSuccessItem,
)
from app.crawl.exceptions import CrawlFatalException
from app.crawl.fetchers.feed_fetcher import FeedEntry, FeedFetchException, fetch_rss
from app.crawl.fetchers.page_crawler import PageCrawler
from app.db.connection import DatabaseClient

logger = logging.getLogger(__name__)


MINGPAO_RSS_URL = "https://news.mingpao.com/rss/pns/s00004.xml"


class MingPaoCrawler(BaseCrawler):
    """
    MingPao crawler — RSS discovery, RSS description as body.

    Full-article fetch (Playwright + `extract_body_css`) was removed 2026-08-31:
    Cloudflare hard-blocks the article pages on this network — confirmed with
    the same anti-fingerprint BrowserManager context used successfully in
    April, so it is not a stale-selector or timing bug (see
    docs/local-test-plan.md investigation). The RSS feed's <description> is
    not blocked and carries a real, if short (~150-250 char), lead-paragraph
    teaser — used directly as the body instead. This is a deliberate depth
    tradeoff versus the other three sources, which capture full article text;
    Ming Pao is lower priority as a source given this. Revisit full-body
    fetch if the block turns out to be specific to this dev network once
    running from the actual deployment target.
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
            # Not logged here — crawl_service.py logs the escalated
            # CrawlFatalException with exc_info=exc, which surfaces this
            # exception's own traceback via chaining. Logging both here and
            # there would double-log the same failure.
            raise CrawlFatalException(f"MingPao RSS fetch failed: {exc}") from exc

        logger.info("[mingpao_crawler] RSS returned %d entries", len(entries))

        result = CrawlResult()
        for entry in entries:
            await self._process_entry(entry, result)

        logger.info(
            "[mingpao_crawler] Completed: %d successes, %d failures",
            len(result.successes),
            len(result.failures),
        )
        return result

    async def _process_entry(self, entry: FeedEntry, result: CrawlResult) -> None:
        if await self._is_url_in_error_log(entry.url):
            logger.info("[mingpao_crawler] Skipping URL in error log: %s", entry.url)
            return

        if not entry.description or not entry.description.strip():
            logger.warning(
                "[mingpao_crawler] Empty RSS description for %s — skipping", entry.url
            )
            return

        result.successes.append(
            CrawlSuccessItem(
                title=entry.title,
                body=entry.description.strip(),
                source_url=entry.url,
                published_at=entry.published_at,
            )
        )
