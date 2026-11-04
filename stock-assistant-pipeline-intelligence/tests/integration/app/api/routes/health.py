import asyncio
import time
from collections.abc import AsyncIterator, MutableMapping
from typing import Any

import httpx
import pytest
import redis.asyncio
import redis.exceptions
import structlog.testing
from httpx import AsyncClient

from app import layer_tasks
from app.api.schemas import HealthResponse
from tests.integration.conftest import Infra

pytestmark = [pytest.mark.live, pytest.mark.timeout(120)]

COMPONENTS = ("database", "redis_stream", "redis_state")
REQUEST_TIMEOUT_S = 10

# Everything up and the real HK Stock List loaded: degraded, because the Cache layer doesn't
# exist yet.
ALL_UP_BODY = {
    "status": "degraded",
    "database": "ok",
    "redis_stream": "ok",
    "redis_state": "ok",
    "hk_stock_list": "ok",
    "layers": {
        "entity_analysis": "ok",
        "aggregation": "ok",
        "scoring": "ok",
        "cache": "error",
    },
}


async def _get_health(client: AsyncClient) -> tuple[httpx.Response, float]:
    started = time.monotonic()
    response = await asyncio.wait_for(client.get("/v1/health"), timeout=REQUEST_TIMEOUT_S)
    return response, time.monotonic() - started


def _body_with_errors(*failed: str) -> dict[str, object]:
    """A failed DB/Redis check makes the response unhealthy; the in-process list stays ok."""
    return {**ALL_UP_BODY, "status": "unhealthy", **{component: "error" for component in failed}}


@pytest.fixture
async def stream_redis(infra: Infra) -> AsyncIterator[redis.asyncio.Redis]:
    """The test's own client of the stream Redis, starting it empty."""
    client = redis.asyncio.Redis.from_url(
        f"redis://127.0.0.1:{infra.redis_stream.port}/0", decode_responses=True
    )
    await client.flushall()
    yield client
    await client.aclose()


async def _wait_consumer_reading(stream_redis: redis.asyncio.Redis) -> None:
    """Wait until every consumer has created its group, so a failure injected next hits the
    running consumers rather than their startup."""
    async with asyncio.timeout(3):
        for stream in (
            "stream:raw_news_cleaned",
            "stream:entity_event_completed",
            "stream:event_aggregated",
        ):
            while True:
                try:
                    if await stream_redis.xinfo_groups(stream):
                        break
                except redis.exceptions.ResponseError:  # the stream does not exist yet
                    pass
                await asyncio.sleep(0.05)


def _failed_check_warnings(logs: list[MutableMapping[str, Any]]) -> list[str]:
    """Components named by the health route's health_check_failed WARNING records."""
    return [
        log["component"]
        for log in logs
        if log["event"] == "health_check_failed" and log["log_level"] == "warning"
    ]


async def get_health_all_up(start_app):
    async with start_app() as client:
        response, _ = await _get_health(client)

    assert response.status_code == 200
    HealthResponse.model_validate(response.json())
    assert response.json() == ALL_UP_BODY


@pytest.mark.parametrize(
    ("component", "mode"),
    [
        pytest.param(component, mode, id=f"{component}-{mode}")
        for component in COMPONENTS
        for mode in ("stop", "pause")
    ],
)
async def get_health_dependency_down_then_recovered(
    infra, start_app, stream_redis, component, mode
):
    """A stopped container refuses connections at once; a paused one never answers, so its
    check ends at HEALTH_CHECK_TIMEOUT_S (default 2s). The Entity Analysis and Aggregation
    layers ride out the outage without crashing."""
    container = infra.for_component(component)

    async with start_app() as client:
        await _wait_consumer_reading(stream_redis)
        layer_task = layer_tasks.get_layer_tasks()["entity_analysis"]
        aggregation_task = layer_tasks.get_layer_tasks()["aggregation"]
        getattr(container, mode)()
        with structlog.testing.capture_logs() as logs:
            response, elapsed = await _get_health(client)

        assert response.status_code == 503
        assert response.json() == _body_with_errors(component)
        assert _failed_check_warnings(logs) == [component]
        if mode == "stop":
            assert elapsed < 2
        else:
            assert 2 <= elapsed < 3

        container.restore()
        response, _ = await _get_health(client)

        assert response.status_code == 200
        assert response.json() == ALL_UP_BODY
        assert layer_tasks.get_layer_tasks()["entity_analysis"] is layer_task
        assert not layer_task.done()
        assert layer_tasks.get_layer_tasks()["aggregation"] is aggregation_task
        assert not aggregation_task.done()


async def get_health_repeated_db_timeouts(infra, start_app):
    """Timed-out DB checks are abandoned, not awaited; once Postgres is back they must not
    still hold the pool's connections."""
    async with start_app(DB_POOL_SIZE="2") as client:
        infra.postgres.pause()
        for _ in range(5):
            response, _ = await _get_health(client)
            assert response.json() == _body_with_errors("database")

        infra.postgres.restore()
        response, elapsed = await _get_health(client)

    assert response.status_code == 200
    assert response.json() == ALL_UP_BODY
    assert elapsed < 2
