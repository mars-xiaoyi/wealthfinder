import asyncio
import logging

import httpx

from app.config import CrawlConfig
from app.crawl.exceptions import CrawlBlockedException, CrawlRateLimitedException

logger = logging.getLogger(__name__)


class PageCrawler:
    """
    Wraps the shared httpx.AsyncClient with retry logic.
    Does not own the client lifecycle — the client is created at service startup.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        config: CrawlConfig,
    ) -> None:
        self._client = client
        self.config = config

    async def fetch(self, url: str) -> httpx.Response:
        """
        Fetch the URL with exponential backoff retry.

        Raises:
            CrawlBlockedException: HTTP error or all retries exhausted
            CrawlRateLimitedException: immediately on HTTP 429
        """
        logger.debug("[page_crawler] Fetching URL: %s", url)
        last_exc: Exception | None = None
        for attempt in range(1, self.config.max_retry + 1):
            try:
                response = await self._client.get(
                    url, timeout=self.config.request_timeout_s
                )

                if response.status_code == 429:
                    logger.warning("[page_crawler] Rate limited (HTTP 429) for %s", url)
                    raise CrawlRateLimitedException(f"HTTP 429 for {url}")

                if 400 <= response.status_code < 500:
                    logger.warning("[page_crawler] Client error HTTP %d for %s", response.status_code, url)
                    raise CrawlBlockedException(
                        f"HTTP {response.status_code} for {url}"
                    )

                response.raise_for_status()
                logger.debug("[page_crawler] Fetched OK: %s", url)
                return response

            except (CrawlBlockedException, CrawlRateLimitedException):
                raise

            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                last_exc = exc
                if attempt < self.config.max_retry:
                    wait_s = (
                        self.config.retry_base_wait_ms * (2 ** (attempt - 1))
                    ) / 1000
                    logger.warning(
                        "[page_crawler] Attempt %d/%d failed for %s, retrying in %.3fs: %s",
                        attempt,
                        self.config.max_retry,
                        url,
                        wait_s,
                        exc,
                    )
                    await asyncio.sleep(wait_s)

        logger.error("[page_crawler] All %d retries exhausted for %s: %s", self.config.max_retry, url, last_exc)
        raise CrawlBlockedException(
            f"All {self.config.max_retry} retries exhausted for {url}: {last_exc}"
        )
