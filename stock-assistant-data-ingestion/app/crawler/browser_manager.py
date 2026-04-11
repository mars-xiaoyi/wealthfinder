import logging

from playwright.async_api import Browser, BrowserContext, async_playwright

logger = logging.getLogger(__name__)


class BrowserManager:
    """
    Manages a single shared playwright Browser instance.
    Each crawler that needs a browser calls acquire_context() to get
    a fresh BrowserContext; it must call release_context() when done.
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        """Launch the playwright Chromium browser. Call once at crawl execution start."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        logger.info("[browser_manager] Chromium browser started")

    async def stop(self) -> None:
        """Close the browser and all open contexts. Call once at crawl execution end."""
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        logger.info("[browser_manager] Browser stopped")

    async def acquire_context(self) -> BrowserContext:
        """
        Create and return a fresh BrowserContext (no cookie carryover).
        Each call creates a new context — contexts are NOT shared between sources.
        """
        if self._browser is None:
            raise RuntimeError("BrowserManager not started. Call start() first.")
        return await self._browser.new_context()

    async def release_context(self, context: BrowserContext) -> None:
        """Close the context. Always call in a finally block."""
        await context.close()
