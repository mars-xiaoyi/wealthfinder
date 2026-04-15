import logging

from playwright.async_api import Browser, BrowserContext, async_playwright

logger = logging.getLogger(__name__)

# Realistic desktop Chrome UA — paired with zh-HK locale/Accept-Language so the
# context looks like a regular HK visitor. MingPao's Cloudflare challenge blocks
# the default headless-shell UA; keeping these defaults on the shared manager
# also improves robustness on any future CF-gated source.
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)
_DEFAULT_LOCALE = "zh-HK"
_DEFAULT_ACCEPT_LANGUAGE = "zh-HK,zh;q=0.9,en;q=0.8"


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
        # channel="chromium" forces the full Chromium build (not headless-shell),
        # which Cloudflare fingerprints less aggressively. The blink flag hides
        # the navigator.webdriver automation tell.
        self._browser = await self._playwright.chromium.launch(
            channel="chromium",
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
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
        return await self._browser.new_context(
            user_agent=_DEFAULT_USER_AGENT,
            locale=_DEFAULT_LOCALE,
            extra_http_headers={"Accept-Language": _DEFAULT_ACCEPT_LANGUAGE},
        )

    async def release_context(self, context: BrowserContext) -> None:
        """Close the context. Always call in a finally block."""
        await context.close()
