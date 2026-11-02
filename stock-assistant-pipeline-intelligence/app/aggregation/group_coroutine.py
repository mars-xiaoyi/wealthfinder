import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.config import AggregationConfig
from app.logger import get_logger
from app.models.entity_event import EventTypeEnum

logger = get_logger(__name__)


@dataclass(frozen=True)
class GroupKey:
    """The group an EntityEvent merges in: one stock's events of one type."""

    exchange: str
    stock_code: str
    event_type_primary: EventTypeEnum


class GroupCoroutine:
    """Runs one group's units of work one at a time, in the order they were enqueued, and ends
    once the group has had none for the idle timeout."""

    def __init__(
        self,
        group_key: GroupKey,
        config: AggregationConfig,
        on_idle: Callable[[GroupKey], None],
    ) -> None:
        self._group_key = group_key
        self._idle_timeout_s = config.group_idle_timeout_s
        self._on_idle = on_idle
        # Unbounded, so put() never suspends: an enqueue can't interleave with the idle check.
        self._queue: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue()
        self._task = asyncio.create_task(self._run())

    async def enqueue(self, work: Callable[[], Awaitable[None]]) -> None:
        """Queue a unit of work to run after every one enqueued before it."""
        await self._queue.put(work)

    async def close(self) -> None:
        """Stop the group, abandoning any work not run yet."""
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                work = await asyncio.wait_for(self._queue.get(), timeout=self._idle_timeout_s)
            except TimeoutError:
                # An enqueue may land between the timeout firing and this handler running.
                if not self._queue.empty():
                    continue
                # Synchronous, so no enqueue can slip in between the check above and this.
                self._on_idle(self._group_key)
                return
            try:
                await work()
            except Exception:
                # The work handles its own failures, so this is a bug in that handling. Logged
                # and skipped, so the group's later work still runs.
                logger.critical(
                    "aggregation_group_work_failed", group_key=self._group_key, exc_info=True
                )
