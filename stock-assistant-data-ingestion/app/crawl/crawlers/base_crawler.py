from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from app.config import CrawlSourceConfig
from app.db.connection import DatabaseClient
from app.crawl.fetchers.page_crawler import PageCrawler


@dataclass
class CrawlSuccessItem:
    title: str
    body: str                           # Plain text; guaranteed non-empty by the crawler
    source_url: str                     # Original article URL; used as dedup key
    published_at: Optional[datetime]    # UTC; None if source does not provide it
    extra_metadata: Optional[dict] = None
    # extra_metadata is only populated by HKEXCrawler: {"stock_code": ["00700", "00388"]}
    # The list always contains at least one entry; multi-issuer rows produce multiple entries.


@dataclass
class CrawlFailItem:
    source_url: str       # URL that failed — always present; RSS-level failures are never CrawlFailItems
    error_type: str       # "NETWORK" / "PARSE" — use ErrorCode constants from app/common/error_codes.py
    error_code: str       # e.g. "SADI-6101" — use ErrorCode constants from app/common/error_codes.py
    attempt_count: int    # Total attempts made before giving up


@dataclass
class CrawlResult:
    successes: list[CrawlSuccessItem] = field(default_factory=list)
    failures: list[CrawlFailItem] = field(default_factory=list)


class BaseCrawler(ABC):
    """
    Abstract base for all source crawlers. Subclasses implement run() to fetch
    and parse all articles for one source.

    Crawlers may *read* from DB (crawl_error_log pre-check, and YahooHKCrawler's
    coverage gap check) but must never *write* to DB or Redis. Persistence and
    stream signals are CrawlService's responsibility.
    """

    def __init__(
        self,
        source_config: CrawlSourceConfig,
        page_crawler: PageCrawler,
        db: DatabaseClient,
        crawl_date: Optional[date] = None,
    ) -> None:
        self.source_config = source_config
        self.page_crawler = page_crawler
        self.db = db
        self.crawl_date = crawl_date

    @abstractmethod
    async def run(self) -> CrawlResult:
        """
        Fetch and parse all articles for this source.

        Returns:
            CrawlResult with:
              - successes: one CrawlSuccessItem per successfully parsed article
              - failures:  one CrawlFailItem per article that failed after all retries

        Raises:
            CrawlFatalError on unrecoverable source-level failure.
        """
        ...

    async def _is_url_in_error_log(self, url: str) -> bool:
        """
        Read-only pre-check against crawl_error_log. Returns True if the URL has
        previously failed and should be skipped this run.
        """
        row = await self.db.fetch_one(
            "SELECT EXISTS(SELECT 1 FROM crawl_error_log WHERE url = $1) AS exists",
            url,
        )
        if row is None:
            return False
        return bool(row["exists"])
