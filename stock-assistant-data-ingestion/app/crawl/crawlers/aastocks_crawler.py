import asyncio
import logging
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
from app.common.error_codes import DocumentParseErrorCode, NetworkErrorCode
from app.crawl.exceptions import CrawlBlockedError, CrawlFatalError, CrawlNetworkError
from app.crawl.parsers.html_parser import extract_body_css
from app.crawl.fetchers.page_crawler import PageCrawler
from app.db.connection import DatabaseClient

logger = logging.getLogger(__name__)


AASTOCKS_LIST_URL = "https://www.aastocks.com/tc/stocks/news/aafn/latest-news"
AASTOCKS_BASE_URL = "https://www.aastocks.com"
ARTICLE_LINK_SELECTOR = "a[href*='/aafn-con/NOW.']"
ARTICLE_BODY_SELECTOR = "[class*='newscon']"

_PUBLISHED_AT_PATTERN = re.compile(r"(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2})")
_HKT = timezone(timedelta(hours=8))


class AAStocksCrawler(BaseCrawler):
    """
    AAStocks crawler — list page (httpx) + per-article httpx fetch.
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
        logger.info("[aastocks_crawler] Starting crawl")
        try:
            list_response = await self.page_crawler.fetch(AASTOCKS_LIST_URL)
        except (CrawlBlockedError, CrawlNetworkError) as exc:
            logger.exception("[aastocks_crawler] List page fetch failed")
            raise CrawlFatalError(f"AAStocks list page fetch failed: {exc}") from exc

        urls = self._extract_article_urls(list_response.text)
        logger.info("[aastocks_crawler] Discovered %d article URLs", len(urls))

        result = CrawlResult()
        if not urls:
            return result

        await self._crawl_articles(urls, result)
        logger.info(
            "[aastocks_crawler] Completed: %d successes, %d failures",
            len(result.successes),
            len(result.failures),
        )
        return result

    # --------------------------------------------------------- discovery

    @staticmethod
    def _extract_article_urls(html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        urls: list[str] = []
        seen: set[str] = set()
        for link in soup.select(ARTICLE_LINK_SELECTOR):
            href = link.get("href")
            if not href:
                continue
            full = href if href.startswith("http") else AASTOCKS_BASE_URL + href
            if full in seen:
                continue
            seen.add(full)
            urls.append(full)
        return urls

    # --------------------------------------------------------- workers

    async def _crawl_articles(self, urls: list[str], result: CrawlResult) -> None:
        semaphore = asyncio.Semaphore(self.source_config.max_concurrent)

        async def worker(url: str) -> None:
            if await self._is_url_in_error_log(url):
                logger.info("[aastocks_crawler] Skipping URL in error log: %s", url)
                return
            async with semaphore:
                await self._fetch_one_article(url, result)

        await asyncio.gather(*(worker(u) for u in urls))

    async def _fetch_one_article(self, url: str, result: CrawlResult) -> None:
        max_retry = self.page_crawler._config.max_retry
        try:
            response = await self.page_crawler.fetch(url)
        except CrawlBlockedError as exc:
            code = NetworkErrorCode.HTTP_403 if "403" in str(exc) else NetworkErrorCode.HTTP_404
            logger.warning("[aastocks_crawler] Blocked %s: %s", url, exc)
            result.failures.append(
                CrawlFailItem(
                    source_url=url,
                    error_type=code.error_type,
                    error_code=code.error_code,
                    attempt_count=1,
                )
            )
            return
        except CrawlNetworkError as exc:
            logger.warning("[aastocks_crawler] Network error %s: %s", url, exc)
            result.failures.append(
                CrawlFailItem(
                    source_url=url,
                    error_type=NetworkErrorCode.NETWORK_ERROR.error_type,
                    error_code=NetworkErrorCode.NETWORK_ERROR.error_code,
                    attempt_count=max_retry,
                )
            )
            return

        try:
            body = extract_body_css(response.text, ARTICLE_BODY_SELECTOR)
        except Exception as exc:
            logger.exception("[aastocks_crawler] body extraction failed for %s: %s", url, exc)
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
            logger.warning("[aastocks_crawler] Empty body for %s — skipping", url)
            return

        title = self._extract_title(response.text) or ""
        published_at = self._parse_published_at(response.text)

        result.successes.append(
            CrawlSuccessItem(
                title=title,
                body=body.strip(),
                source_url=url,
                published_at=published_at,
            )
        )

    # --------------------------------------------------------- helpers

    @staticmethod
    def _extract_title(page_html: str) -> Optional[str]:
        soup = BeautifulSoup(page_html, "html.parser")
        if soup.title and soup.title.text:
            return soup.title.text.strip()
        return None

    @staticmethod
    def _parse_published_at(page_html: str) -> Optional[datetime]:
        match = _PUBLISHED_AT_PATTERN.search(page_html)
        if match is None:
            return None
        year, month, day, hour, minute = (int(g) for g in match.groups())
        hkt = datetime(year, month, day, hour, minute, tzinfo=_HKT)
        return hkt.astimezone(timezone.utc)
