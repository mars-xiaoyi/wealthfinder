import json
import logging
from datetime import date, datetime, timezone
from typing import Optional
from uuid import uuid4

import asyncpg

from app.common.text_utils import compute_hash, normalise
from app.config import CrawlConfig
from app.crawl.crawlers.aastocks_crawler import AAStocksCrawler
from app.crawl.crawlers.base_crawler import (
    BaseCrawler,
    CrawlFailItem,
    CrawlResult,
    CrawlSuccessItem,
)
from app.crawl.exceptions import CrawlFatalError
from app.crawl.crawlers.hkex_crawler import HKEXCrawler
from app.crawl.crawlers.mingpao_crawler import MingPaoCrawler
from app.crawl.fetchers.page_crawler import PageCrawler
from app.crawl.source_name import CrawlSourceName
from app.crawl.crawlers.yahoo_hk_crawler import YahooHKCrawler
from app.db.connection import DatabaseClient
from app.redis.stream_client import (
    STREAM_CRAWL_COMPLETED,
    STREAM_RAW_NEWS_INSERTED,
    StreamClient,
)

logger = logging.getLogger(__name__)


_INSERT_RAW_NEWS_SQL = """
INSERT INTO raw_news (
    raw_id, source_name, source_url, title, body,
    published_at, created_at, raw_hash, extra_metadata
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (source_url) DO NOTHING
"""


_INSERT_CRAWL_ERROR_LOG_SQL = """
INSERT INTO crawl_error_log (
    error_id, execution_id, source_name, url,
    error_type, error_code, attempt_count, created_at
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""


class CrawlService:
    """
    Orchestrates one full crawl execution: instantiate the right crawler, run it,
    persist successes/failures, and publish stream signals. execute() never raises —
    all outcomes are signalled via stream:crawl_completed.
    """

    CRAWLER_REGISTRY: dict[CrawlSourceName, type[BaseCrawler]] = {
        CrawlSourceName.HKEX: HKEXCrawler,
        CrawlSourceName.MINGPAO: MingPaoCrawler,
        CrawlSourceName.AASTOCKS: AAStocksCrawler,
        CrawlSourceName.YAHOO_HK: YahooHKCrawler,
    }

    def __init__(
        self,
        db: DatabaseClient,
        stream_client: StreamClient,
        page_crawler: PageCrawler,
        config: CrawlConfig,
    ) -> None:
        self._db = db
        self._stream_client = stream_client
        self._page_crawler = page_crawler
        self._config = config

    # ------------------------------------------------------------------ public

    async def execute(
        self,
        execution_id: str,
        source_name: CrawlSourceName,
        crawl_date: Optional[date],
    ) -> None:
        """
        Run one full crawl execution end-to-end. Never raises — all outcomes are
        signalled via stream:crawl_completed.
        """
        logger.info(
            "[crawl_service] execute start: execution_id=%s source=%s date=%s",
            execution_id,
            source_name.value,
            crawl_date,
        )
        try:
            crawler = self._create_crawler(source_name, crawl_date)
            try:
                result = await crawler.run()
            except CrawlFatalError as exc:
                logger.error(
                    "[crawl_service] CrawlFatalError for %s: %s",
                    source_name.value,
                    exc,
                )
                await self._publish_completed(execution_id, "FAILED", str(exc))
                return

            await self._persist_failures(execution_id, source_name, result.failures)
            await self._persist_successes(source_name, result.successes)
            await self._publish_completed(execution_id, "SUCCESS")
            logger.info(
                "[crawl_service] execute done: execution_id=%s successes=%d failures=%d",
                execution_id,
                len(result.successes),
                len(result.failures),
            )
        except Exception as exc:
            logger.exception(
                "[crawl_service] Unexpected error during execute (execution_id=%s)",
                execution_id,
            )
            try:
                await self._publish_completed(execution_id, "FAILED", str(exc))
            except Exception:
                logger.exception(
                    "[crawl_service] Failed to publish crawl_completed FAILED signal"
                )

    # --------------------------------------------------------- internals

    def _create_crawler(
        self,
        source_name: CrawlSourceName,
        crawl_date: Optional[date],
    ) -> BaseCrawler:
        source_config = self._config.crawl_sources[source_name.value]
        crawler_cls = self.CRAWLER_REGISTRY[source_name]
        return crawler_cls(source_config, self._page_crawler, self._db, crawl_date)

    async def _persist_successes(
        self,
        source_name: CrawlSourceName,
        successes: list[CrawlSuccessItem],
    ) -> None:
        for item in successes:
            raw_id = uuid4()
            try:
                normalised_title = normalise(item.title)
                raw_hash = compute_hash(normalised_title)
            except Exception:
                logger.exception(
                    "[crawl_service] Failed to compute raw_hash for %s — skipping",
                    item.source_url,
                )
                continue

            extra_metadata_json = (
                json.dumps(item.extra_metadata) if item.extra_metadata is not None else None
            )

            try:
                await self._db.execute(
                    _INSERT_RAW_NEWS_SQL,
                    raw_id,
                    source_name.value,
                    item.source_url,
                    item.title,
                    item.body,
                    item.published_at,
                    datetime.now(timezone.utc),
                    raw_hash,
                    extra_metadata_json,
                )
            except asyncpg.UniqueViolationError:
                logger.info(
                    "[crawl_service] Duplicate raw_news (hash collision) for %s — no-op",
                    item.source_url,
                )
                continue
            except Exception:
                logger.exception(
                    "[crawl_service] Failed to insert raw_news for %s",
                    item.source_url,
                )
                continue

            try:
                await self._stream_client.publish(
                    STREAM_RAW_NEWS_INSERTED,
                    {"raw_id": str(raw_id)},
                )
            except Exception:
                logger.exception(
                    "[crawl_service] Failed to publish raw_news_inserted for %s",
                    raw_id,
                )

    async def _persist_failures(
        self,
        execution_id: str,
        source_name: CrawlSourceName,
        failures: list[CrawlFailItem],
    ) -> None:
        for failure in failures:
            await self._save_crawl_error(execution_id, source_name, failure)

    async def _save_crawl_error(
        self,
        execution_id: str,
        source_name: CrawlSourceName,
        failure: CrawlFailItem,
    ) -> None:
        """
        Best-effort insert into crawl_error_log. Failures here are logged and
        swallowed — they must not abort the broader crawl execution.
        """
        try:
            await self._db.execute(
                _INSERT_CRAWL_ERROR_LOG_SQL,
                uuid4(),
                execution_id,
                source_name.value,
                failure.source_url,
                failure.error_type,
                failure.error_code,
                failure.attempt_count,
                datetime.now(timezone.utc),
            )
        except Exception:
            logger.exception(
                "[crawl_service] Failed to insert crawl_error_log for %s",
                failure.source_url,
            )

    async def _publish_completed(
        self,
        execution_id: str,
        status: str,
        error_detail: Optional[str] = None,
    ) -> None:
        """
        Publish the terminal ``stream:crawl_completed`` signal.

        ``error_detail`` is only included when non-None (i.e. on FAILED). On
        SUCCESS, the field is omitted entirely — Redis Streams have no native
        NULL, and omitting the field is the idiomatic "absent" representation.
        Admin-side consumers should check ``"error_detail" in fields``.
        """
        payload: dict[str, str] = {
            "execution_id": execution_id,
            "status": status,
        }
        if error_detail is not None:
            payload["error_detail"] = error_detail
        await self._stream_client.publish(STREAM_CRAWL_COMPLETED, payload)
