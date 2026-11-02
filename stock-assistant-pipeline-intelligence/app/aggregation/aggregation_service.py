import asyncio
import time
from collections.abc import Awaitable, Callable
from uuid import UUID

from app.aggregation.event_aggregation_store import get_event_aggregation_store
from app.aggregation.group_coroutine import GroupCoroutine, GroupKey
from app.aggregation.sliding_window import is_late, merge_or_create, ttl_for
from app.common.error_categories import ErrorCategory
from app.common.errors import SAPIError, ServiceUnavailableError
from app.config import AggregationConfig
from app.logger import get_logger
from app.models.entity_event import EntityEvent
from app.persistent.entity_event_repository import get_entity_event_repository

logger = get_logger(__name__)


class AggregationService:
    """Merges each EntityEvent into its group's active Event."""

    def __init__(self, config: AggregationConfig) -> None:
        self._entity_event_repo = get_entity_event_repository()
        self._event_store = get_event_aggregation_store()
        self._config = config
        self._groups: dict[GroupKey, GroupCoroutine] = {}
        # One permit per live group, held until the group idles out.
        self._group_semaphore = asyncio.Semaphore(config.max_concurrent)

    async def process_entity_event(
        self,
        entity_event_id: UUID,
        on_success: Callable[[], Awaitable[None]],
        on_failure: Callable[[], None],
    ) -> None:
        """
        Merge the EntityEvent into its group's active Event, in the order its group's
        EntityEvents arrive. on_success is awaited once the outcome is final; on_failure is
        called if the merge fails.
        Raises ServiceUnavailableError or DatabaseError.
        """
        entity_event = await self._entity_event_repo.fetch_entity_event(entity_event_id)
        if entity_event is None:
            # A redelivery can't make the row appear, so the outcome is final.
            logger.warning("aggregation_entity_event_missing", entity_event_id=entity_event_id)
            await on_success()
            return
        group_key = GroupKey(
            entity_event.stock.exchange,
            entity_event.stock.stock_code,
            entity_event.analysis_outcome.output.event_type_primary,
        )
        group = await self._get_group(group_key)
        await group.enqueue(lambda: self._merge(entity_event, group_key, on_success, on_failure))

    async def close(self) -> None:
        """Stop every group, abandoning the work they have not run yet."""
        await asyncio.gather(*(group.close() for group in list(self._groups.values())))

    async def _get_group(self, group_key: GroupKey) -> GroupCoroutine:
        """Return the group's GroupCoroutine, starting one if the group has none."""
        group = self._groups.get(group_key)
        if group is not None:
            return group
        start = time.perf_counter()
        await self._group_semaphore.acquire()
        # Another call may have started this group while this one waited for a permit.
        group = self._groups.get(group_key)
        if group is not None:
            self._group_semaphore.release()
            return group
        group = GroupCoroutine(group_key, self._config, self._on_idle)
        self._groups[group_key] = group
        # Sizes AGG_MAX_CONCURRENT and AGG_GROUP_IDLE_TIMEOUT_S: peak live groups, how long a
        # new group waited for a permit, and how soon a group starts again after idling out.
        logger.info(
            "aggregation_group_started",
            group_key=group_key,
            live_groups=len(self._groups),
            permit_wait_ms=int((time.perf_counter() - start) * 1000),
        )
        return group

    def _on_idle(self, group_key: GroupKey) -> None:
        # Synchronous, so no enqueue can reach the group between its idle check and this.
        self._groups.pop(group_key, None)
        self._group_semaphore.release()

    async def _merge(
        self,
        entity_event: EntityEvent,
        group_key: GroupKey,
        on_success: Callable[[], Awaitable[None]],
        on_failure: Callable[[], None],
    ) -> None:
        """Merge the EntityEvent into its group's active Event."""
        entity_event_id = entity_event.entity_event_id
        try:
            active_event = await self._event_store.get_active_event(
                group_key.exchange, group_key.stock_code, group_key.event_type_primary
            )
            if active_event is not None:
                # Set on an active Event, which is all the store returns.
                assert active_event.entity_event_ids is not None
                # Checked inside the group's queue, so it can't race a merge of the same id.
                if entity_event_id in active_event.entity_event_ids:
                    logger.info(
                        "aggregation_duplicate_skipped",
                        entity_event_id=entity_event_id,
                        group_key=group_key,
                    )
                    await on_success()
                    return
                # Merging it would start an older Event and close the live one early.
                if is_late(active_event, entity_event):
                    logger.warning(
                        "aggregation_late_entity_event_skipped",
                        entity_event_id=entity_event_id,
                        group_key=group_key,
                        published_at=entity_event.get_published_at(),
                        active_event_id=active_event.event_id,
                        active_last_seen_at=active_event.last_seen_at,
                    )
                    await on_success()
                    return
            updated = merge_or_create(active_event, entity_event)
            await self._event_store.upsert_active_event(updated, ttl_for(updated))
            await on_success()
        except SAPIError as exc:
            logger.log(
                exc.log_level,
                exc.dev_message,
                entity_event_id=entity_event_id,
                group_key=group_key,
                exc_info=exc,
            )
            on_failure()
        except Exception as exc:
            logger.error(
                f"Unhandled exception: {exc!r}",
                entity_event_id=entity_event_id,
                group_key=group_key,
                exc_info=exc,
            )
            on_failure()


_aggregation_service: AggregationService | None = None


def get_aggregation_service() -> AggregationService:
    """Return the process's one AggregationService instance."""
    if _aggregation_service is None:
        raise ServiceUnavailableError(
            "aggregation service is not initialized", category=ErrorCategory.AGGREGATION
        )
    return _aggregation_service


def create_aggregation_service(config: AggregationConfig) -> AggregationService:
    """Create the AggregationService and register it as this module's singleton."""
    global _aggregation_service
    _aggregation_service = AggregationService(config)
    return _aggregation_service
