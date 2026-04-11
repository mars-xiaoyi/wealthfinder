from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import CrawlConfig, CrawlSourceConfig
from app.crawler.exceptions import CrawlBlockedError, CrawlNetworkError
from app.crawler.page_crawler import PageCrawler


def make_config(**overrides) -> CrawlConfig:
    defaults = dict(
        max_retry=3,
        retry_base_wait_ms=100,
        request_timeout_s=10,
        crawl_sources={},
    )
    return CrawlConfig(**{**defaults, **overrides})


def make_page_crawler(config=None):
    config = config or make_config()
    client = AsyncMock(spec=httpx.AsyncClient)
    return PageCrawler(client, config), client


def _ok_response(status_code=200):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# fetch — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_success():
    crawler, client = make_page_crawler()
    expected_resp = _ok_response()
    client.get = AsyncMock(return_value=expected_resp)

    result = await crawler.fetch("https://example.com/article")

    assert result is expected_resp
    client.get.assert_called_once_with("https://example.com/article", timeout=10)


# ---------------------------------------------------------------------------
# fetch — HTTP 403 raises CrawlBlockedError immediately
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_blocked_on_403():
    crawler, client = make_page_crawler()
    resp = _ok_response(status_code=403)
    client.get = AsyncMock(return_value=resp)

    with pytest.raises(CrawlBlockedError, match="HTTP 403"):
        await crawler.fetch("https://example.com/blocked")

    # Should not retry — only one call
    assert client.get.call_count == 1


@pytest.mark.asyncio
async def test_fetch_blocked_on_404():
    crawler, client = make_page_crawler()
    resp = _ok_response(status_code=404)
    client.get = AsyncMock(return_value=resp)

    with pytest.raises(CrawlBlockedError, match="HTTP 404"):
        await crawler.fetch("https://example.com/notfound")

    assert client.get.call_count == 1


# ---------------------------------------------------------------------------
# fetch — retries on network error then raises CrawlNetworkError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_retries_then_raises_network_error():
    config = make_config(max_retry=3, retry_base_wait_ms=10)
    crawler, client = make_page_crawler(config=config)
    client.get = AsyncMock(side_effect=httpx.RequestError("timeout"))

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(CrawlNetworkError, match="All 3 retries exhausted"):
            await crawler.fetch("https://example.com/slow")

    assert client.get.call_count == 3
    # Backoff: 10ms, 20ms (attempt 3 fails without sleeping)
    assert mock_sleep.call_count == 2


# ---------------------------------------------------------------------------
# fetch — retries on HTTP 500 then succeeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_retries_on_500_then_succeeds():
    config = make_config(max_retry=3, retry_base_wait_ms=10)
    crawler, client = make_page_crawler(config=config)

    error_resp = MagicMock(spec=httpx.Response)
    error_resp.status_code = 500
    error_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=error_resp)
    )

    ok_resp = _ok_response()

    client.get = AsyncMock(side_effect=[error_resp, ok_resp])

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await crawler.fetch("https://example.com/flaky")

    assert result is ok_resp
    assert client.get.call_count == 2
