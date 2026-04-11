import asyncio
import logging

import httpx

from app.config import CrawlConfig
from app.crawler.exceptions import CrawlBlockedError, CrawlNetworkError

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
        self._config = config

    async def fetch(self, url: str) -> httpx.Response:
        """
        Fetch the URL with exponential backoff retry.

        Raises:
            CrawlNetworkError: after all retries exhausted
            CrawlBlockedError: immediately on HTTP 403 or 404
        """
        logger.debug("[page_crawler] Fetching URL: %s", url)
        last_exc: Exception | None = None
        for attempt in range(1, self._config.max_retry + 1):
            try:
                response = await self._client.get(
                    url, timeout=self._config.request_timeout_s
                )

                if response.status_code in (403, 404):
                    logger.warning("[page_crawler] Blocked HTTP %d for %s", response.status_code, url)
                    raise CrawlBlockedError(
                        f"HTTP {response.status_code} for {url}"
                    )

                response.raise_for_status()
                logger.debug("[page_crawler] Fetched OK: %s", url)
                return response

            except CrawlBlockedError:
                raise

            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                last_exc = exc
                if attempt < self._config.max_retry:
                    wait_s = (
                        self._config.retry_base_wait_ms * (2 ** (attempt - 1))
                    ) / 1000
                    logger.warning(
                        "[page_crawler] Attempt %d/%d failed for %s, retrying in %.3fs: %s",
                        attempt,
                        self._config.max_retry,
                        url,
                        wait_s,
                        exc,
                    )
                    await asyncio.sleep(wait_s)

        logger.error("[page_crawler] All %d retries exhausted for %s: %s", self._config.max_retry, url, last_exc)
        raise CrawlNetworkError(
            f"All {self._config.max_retry} retries exhausted for {url}: {last_exc}"
        )
