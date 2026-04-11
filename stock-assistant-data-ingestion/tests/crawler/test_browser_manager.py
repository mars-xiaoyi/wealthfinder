from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.crawler.browser_manager import BrowserManager


# ---------------------------------------------------------------------------
# start / stop lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_launches_browser():
    manager = BrowserManager()
    mock_pw = AsyncMock()
    mock_browser = AsyncMock()
    mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)

    mock_pw_cm = AsyncMock()
    mock_pw_cm.start = AsyncMock(return_value=mock_pw)

    with patch("app.crawler.browser_manager.async_playwright", return_value=mock_pw_cm):
        await manager.start()

    assert manager._browser is mock_browser
    mock_pw.chromium.launch.assert_called_once_with(headless=True)


@pytest.mark.asyncio
async def test_stop_closes_browser_and_playwright():
    manager = BrowserManager()
    mock_browser = AsyncMock()
    mock_playwright = AsyncMock()
    manager._browser = mock_browser
    manager._playwright = mock_playwright

    await manager.stop()

    mock_browser.close.assert_called_once()
    mock_playwright.stop.assert_called_once()
    assert manager._browser is None
    assert manager._playwright is None


@pytest.mark.asyncio
async def test_stop_when_not_started_is_noop():
    manager = BrowserManager()
    await manager.stop()  # should not raise


# ---------------------------------------------------------------------------
# acquire_context / release_context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_acquire_context_creates_new_context():
    manager = BrowserManager()
    mock_browser = AsyncMock()
    mock_context = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    manager._browser = mock_browser

    ctx = await manager.acquire_context()

    assert ctx is mock_context
    mock_browser.new_context.assert_called_once()


@pytest.mark.asyncio
async def test_acquire_context_raises_when_not_started():
    manager = BrowserManager()
    with pytest.raises(RuntimeError, match="not started"):
        await manager.acquire_context()


@pytest.mark.asyncio
async def test_release_context_closes_context():
    manager = BrowserManager()
    mock_context = AsyncMock()

    await manager.release_context(mock_context)

    mock_context.close.assert_called_once()
