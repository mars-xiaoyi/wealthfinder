import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.logger import get_logger

logger = get_logger(__name__)


class WorkerPool:
    """Bounds how much of a layer's work runs at once, and pauses the layer's new work after a
    rate limit. layer names the layer in logs."""

    def __init__(self, layer: str, max_concurrent: int, rate_limit_backoff_s: int) -> None:
        self._layer = layer
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._rate_limit_backoff_s = rate_limit_backoff_s
        self._rate_limit_until = 0.0

    async def dispatch(self, coro_factory: Callable[[], Awaitable[None]]) -> None:
        """Run the work once a slot is free and no backoff is in force. Returns once the work
        has completed."""
        async with self._semaphore:
            # Sleeps until the deadline rather than polling; re-checked after every sleep, so a
            # backoff extended mid-sleep is honoured.
            loop = asyncio.get_running_loop()
            while (remaining := self._rate_limit_until - loop.time()) > 0:  # noqa: ASYNC110
                await asyncio.sleep(remaining)
            await coro_factory()

    def trigger_backoff(self) -> None:
        """Hold back the layer's new work for the configured backoff. Work already running
        carries on."""
        self._rate_limit_until = asyncio.get_running_loop().time() + self._rate_limit_backoff_s
        # The loop clock is monotonic, with no time-of-day meaning, so the log gets a UTC time.
        backoff_until = datetime.now(UTC) + timedelta(seconds=self._rate_limit_backoff_s)
        logger.warning(
            "rate_limit_backoff_started",
            layer=self._layer,
            backoff_until=backoff_until.isoformat(),
        )
