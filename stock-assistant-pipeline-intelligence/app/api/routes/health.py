import asyncio
from collections.abc import Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status

from app.api.schemas import (
    ComponentStatus,
    HealthResponse,
    HealthStatus,
    HKStockListStatus,
    LayerStatus,
)
from app.config import AppConfig, get_config
from app.db.connection import DatabaseClient, get_db_client
from app.logger import get_logger
from app.redis.state_client import RedisStateClient, get_redis_state_client
from app.redis.stream_client import RedisStreamClient, get_redis_stream_client

logger = get_logger(__name__)

router = APIRouter()

# The HK Stock List cache and the layer Tasks don't exist yet, so these report their
# honest failure state rather than a fabricated "ok".
_HK_STOCK_LIST_STATUS = HKStockListStatus.NOT_READY
_LAYER_STATUS = LayerStatus(
    entity_analysis=ComponentStatus.ERROR,
    aggregation=ComponentStatus.ERROR,
    scoring=ComponentStatus.ERROR,
    cache=ComponentStatus.ERROR,
)


# Probes cancelled by a timeout or a cancelled request, still unwinding. Holding a reference
# keeps them from being garbage-collected mid-cleanup; each removes itself once done.
_abandoned_probes: set[asyncio.Task[object]] = set()


def _discard_abandoned(task: asyncio.Task[object]) -> None:
    """Done-callback of an abandoned probe: drop our reference once its cleanup has ended.

    task.exception() doesn't raise; it returns the exception the probe ended with (if any) and
    marks it as retrieved. Nobody else ever looks at an abandoned probe's result, and asyncio
    logs "Task exception was never retrieved" for any exception left unread.
    """
    _abandoned_probes.discard(task)
    if not task.cancelled():
        task.exception()


def _abandon(task: asyncio.Task[object]) -> None:
    """Cancel a probe without waiting for its cleanup, and keep it alive until that cleanup ends.

    asyncio keeps only weak references to tasks, so a cancelled probe whose cleanup is still
    waiting on a server could be garbage-collected mid-way without _abandoned_probes.
    """
    task.cancel()
    _abandoned_probes.add(task)
    task.add_done_callback(_discard_abandoned)


async def _check(
    component: str, probe: Coroutine[Any, Any, object], timeout_s: float
) -> ComponentStatus:
    """Run one dependency probe, bounded by timeout_s; any failure or timeout reports ERROR.

    Not asyncio.wait_for: it waits for the cancelled probe to finish unwinding, and a driver's
    cancellation cleanup can itself block on an unresponsive server (system-design.md §7.1).
    """
    task = asyncio.create_task(probe)
    try:
        # asyncio.wait never raises the probe's own exception; that stays in the task.
        done, _ = await asyncio.wait({task}, timeout=timeout_s)
    except asyncio.CancelledError:
        # The request itself was cancelled (client gone or server shutting down): there is no
        # response left to return, and cancellation must propagate, so only stop the probe.
        _abandon(task)
        raise
    if not done:
        _abandon(task)
        logger.warning("health_check_failed", component=component, timeout_s=timeout_s)
        return ComponentStatus.ERROR
    if task.cancelled():
        logger.warning("health_check_failed", component=component, reason="probe_cancelled")
        return ComponentStatus.ERROR
    if (exc := task.exception()) is not None:
        logger.warning("health_check_failed", component=component, exc_info=exc)
        return ComponentStatus.ERROR
    return ComponentStatus.OK


def _overall_status(
    database: ComponentStatus,
    redis_stream: ComponentStatus,
    redis_state: ComponentStatus,
    hk_stock_list: HKStockListStatus,
    layers: LayerStatus,
) -> HealthStatus:
    if (
        ComponentStatus.ERROR in (database, redis_stream, redis_state)
        or hk_stock_list is HKStockListStatus.NOT_READY
    ):
        return HealthStatus.UNHEALTHY
    layer_statuses = (layers.entity_analysis, layers.aggregation, layers.scoring, layers.cache)
    if ComponentStatus.ERROR in layer_statuses:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def get_health(
    response: Response,
    config: Annotated[AppConfig, Depends(get_config)],
    db: Annotated[DatabaseClient, Depends(get_db_client)],
    redis_stream: Annotated[RedisStreamClient, Depends(get_redis_stream_client)],
    redis_state: Annotated[RedisStateClient, Depends(get_redis_state_client)],
) -> HealthResponse:
    timeout_s = config.health.check_timeout_s
    database_status, redis_stream_status, redis_state_status = await asyncio.gather(
        _check("database", db.fetch_one("SELECT 1"), timeout_s),
        _check("redis_stream", redis_stream.ping(), timeout_s),
        _check("redis_state", redis_state.ping(), timeout_s),
    )

    overall = _overall_status(
        database_status,
        redis_stream_status,
        redis_state_status,
        _HK_STOCK_LIST_STATUS,
        _LAYER_STATUS,
    )
    if overall is HealthStatus.UNHEALTHY:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status=overall,
        database=database_status,
        redis_stream=redis_stream_status,
        redis_state=redis_state_status,
        hk_stock_list=_HK_STOCK_LIST_STATUS,
        layers=_LAYER_STATUS,
    )
