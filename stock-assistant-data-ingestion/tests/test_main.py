from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.main as main_module


@pytest.mark.asyncio
async def test_lifespan_http_client_sends_browser_identity_headers():
    """
    The shared httpx.AsyncClient used by PageCrawler (AAStocks, HKEX's PDF
    phase, Yahoo HK) must send a realistic browser User-Agent/Accept-Language.
    Without it, httpx's default `python-httpx/x.y.z` UA gets fingerprinted and
    blocked (HTTP 429) on the very first request, regardless of request
    volume — confirmed live against Yahoo HK. See docs/local-test-plan.md.
    """
    fake_config = MagicMock()
    fake_config.crawl.request_timeout_s = 10

    fake_app = MagicMock()

    with patch.object(main_module, "load_config", return_value=fake_config), \
         patch.object(main_module, "create_db_client", AsyncMock(return_value=AsyncMock())), \
         patch.object(main_module, "create_stream_client", AsyncMock(return_value=AsyncMock())), \
         patch.object(main_module.StreamHandler, "ensure_consumer_group", AsyncMock()), \
         patch.object(main_module.CleaningService, "start", AsyncMock()):

        async with main_module.lifespan(fake_app):
            http_client = fake_app.state.crawl_service._page_crawler._client
            assert http_client.headers["user-agent"] == main_module.DEFAULT_USER_AGENT
            assert (
                http_client.headers["accept-language"]
                == main_module.DEFAULT_ACCEPT_LANGUAGE
            )
            assert "Chrome" in http_client.headers["user-agent"]
            assert "python-httpx" not in http_client.headers["user-agent"]
