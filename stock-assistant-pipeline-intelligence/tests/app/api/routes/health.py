import asyncio
import gc
from typing import Any
from unittest.mock import AsyncMock

import pytest
import redis.exceptions
from fastapi import Response

from app.api.routes import health
from app.api.schemas import (
    ComponentStatus,
    HealthStatus,
    HKStockListStatus,
    LayerStatus,
)
from app.common.errors import ServiceUnavailableError
from app.config import AppConfig, DatabaseConfig, HealthConfig, RedisConfig
from app.db.connection import DatabaseClient
from app.redis.state_client import RedisStateClient
from app.redis.stream_client import RedisStreamClient

SHORT_TIMEOUT_S = 0.05


COMPONENTS = ("database", "redis_stream", "redis_state")


def _probe(component: str, db: AsyncMock, redis_stream: AsyncMock, redis_state: AsyncMock) -> Any:
    """The mocked client call behind one health field."""
    probes = {
        "database": db.fetch_one,
        "redis_stream": redis_stream.ping,
        "redis_state": redis_state.ping,
    }
    return probes[component]


async def _hang_forever(*_args: object) -> None:
    await asyncio.Event().wait()


@pytest.fixture
def config():
    return AppConfig(
        db=DatabaseConfig(url="postgresql://localhost/sapi", pool_size=10, close_timeout_s=10),
        health=HealthConfig(check_timeout_s=2),
        redis=RedisConfig(stream_url="redis://localhost/0", state_url="redis://localhost/1"),
    )


@pytest.fixture
def db():
    client = AsyncMock(spec=DatabaseClient)
    client.fetch_one.return_value = (1,)
    return client


@pytest.fixture
def redis_stream():
    return AsyncMock(spec=RedisStreamClient)


@pytest.fixture
def redis_state():
    return AsyncMock(spec=RedisStateClient)


@pytest.fixture
async def unhandled_task_errors():
    """Exceptions asyncio reports as never retrieved ("Task exception was never retrieved").

    asyncio hands those reports to the event loop's exception handler; this replaces the
    handler with one that records them, so a test can assert none were raised.
    """
    reports: list[dict[str, Any]] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: reports.append(ctx))
    return reports


async def get_health_hk_stock_list_not_ready(config, db, redis_stream, redis_state):
    response = Response()

    body = await health.get_health(response, config, db, redis_stream, redis_state)

    assert response.status_code == 503
    assert body.status is HealthStatus.UNHEALTHY
    assert body.database is ComponentStatus.OK
    assert body.redis_stream is ComponentStatus.OK
    assert body.redis_state is ComponentStatus.OK
    assert body.hk_stock_list is HKStockListStatus.NOT_READY
    assert body.layers == LayerStatus(
        entity_analysis=ComponentStatus.ERROR,
        aggregation=ComponentStatus.ERROR,
        scoring=ComponentStatus.ERROR,
        cache=ComponentStatus.ERROR,
    )
    db.fetch_one.assert_awaited_once_with("SELECT 1")
    redis_stream.ping.assert_awaited_once()
    redis_state.ping.assert_awaited_once()


@pytest.mark.parametrize(
    ("component", "error"),
    [
        pytest.param("database", ServiceUnavailableError("DB unreachable"), id="database"),
        pytest.param("redis_stream", redis.exceptions.ConnectionError("down"), id="redis_stream"),
        pytest.param("redis_state", redis.exceptions.TimeoutError("slow"), id="redis_state"),
    ],
)
async def get_health_dependency_error(config, db, redis_stream, redis_state, component, error):
    _probe(component, db, redis_stream, redis_state).side_effect = error
    response = Response()

    body = await health.get_health(response, config, db, redis_stream, redis_state)

    assert response.status_code == 503
    assert body.status is HealthStatus.UNHEALTHY
    assert getattr(body, component) is ComponentStatus.ERROR
    for other in COMPONENTS:
        if other != component:
            assert getattr(body, other) is ComponentStatus.OK


@pytest.mark.parametrize("component", COMPONENTS)
async def get_health_dependency_timeout(config, db, redis_stream, redis_state, component):
    config.health.check_timeout_s = SHORT_TIMEOUT_S
    _probe(component, db, redis_stream, redis_state).side_effect = _hang_forever
    response = Response()

    body = await health.get_health(response, config, db, redis_stream, redis_state)

    assert response.status_code == 503
    assert body.status is HealthStatus.UNHEALTHY
    assert getattr(body, component) is ComponentStatus.ERROR
    for other in COMPONENTS:
        if other != component:
            assert getattr(body, other) is ComponentStatus.OK


async def get_health_database_timeout_cleanup_blocks(config, db, redis_stream, redis_state):
    """A paused Postgres: the timed-out query's cancellation cleanup never finishes.

    get_health must still answer once the timeout fires instead of waiting for that cleanup.
    """
    config.health.check_timeout_s = SHORT_TIMEOUT_S
    server_answers = asyncio.Event()

    async def query_on_paused_server(*_args: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await server_answers.wait()  # e.g. asyncpg's cancel request, unanswered
            raise

    db.fetch_one.side_effect = query_on_paused_server

    body = await asyncio.wait_for(
        health.get_health(Response(), config, db, redis_stream, redis_state), timeout=1
    )

    assert body.status is HealthStatus.UNHEALTHY
    assert body.database is ComponentStatus.ERROR
    assert body.redis_stream is ComponentStatus.OK
    assert body.redis_state is ComponentStatus.OK
    server_answers.set()


async def get_health_check_cancelled_itself(config, db, redis_stream, redis_state):
    db.fetch_one.side_effect = asyncio.CancelledError
    response = Response()

    body = await health.get_health(response, config, db, redis_stream, redis_state)

    assert response.status_code == 503
    assert body.status is HealthStatus.UNHEALTHY
    assert body.database is ComponentStatus.ERROR
    assert body.redis_stream is ComponentStatus.OK
    assert body.redis_state is ComponentStatus.OK


async def get_health_abandoned_check_fails_during_cleanup(
    config, db, redis_stream, redis_state, unhandled_task_errors
):
    """A timed-out check whose cleanup later fails must not surface as an unhandled error."""
    config.health.check_timeout_s = SHORT_TIMEOUT_S
    server_answers = asyncio.Event()

    async def query_failing_during_cleanup(*_args: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await server_answers.wait()
            raise ConnectionError("cancel request failed") from None

    db.fetch_one.side_effect = query_failing_during_cleanup

    body = await health.get_health(Response(), config, db, redis_stream, redis_state)
    server_answers.set()
    await asyncio.sleep(0.01)
    gc.collect()

    assert body.status is HealthStatus.UNHEALTHY
    assert body.database is ComponentStatus.ERROR
    assert body.redis_stream is ComponentStatus.OK
    assert body.redis_state is ComponentStatus.OK
    assert unhandled_task_errors == []


async def get_health_cancelled(config, db, redis_stream, redis_state, unhandled_task_errors):
    """Cancelling the request stops its running checks, without unhandled errors from them."""
    server_answers = asyncio.Event()
    cleanup_ran = asyncio.Event()

    async def query_failing_during_cleanup(*_args: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await server_answers.wait()
            cleanup_ran.set()
            raise ConnectionError("cancel request failed") from None

    db.fetch_one.side_effect = query_failing_during_cleanup
    request = asyncio.create_task(
        health.get_health(Response(), config, db, redis_stream, redis_state)
    )
    await asyncio.sleep(0.01)

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    server_answers.set()
    await asyncio.wait_for(cleanup_ran.wait(), timeout=1)
    await asyncio.sleep(0.01)
    gc.collect()

    assert unhandled_task_errors == []
