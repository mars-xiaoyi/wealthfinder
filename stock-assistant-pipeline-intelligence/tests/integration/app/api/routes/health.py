import asyncio
import time
from collections.abc import MutableMapping
from typing import Any

import httpx
import pytest
import structlog.testing
from httpx import AsyncClient

from app.api.schemas import HealthResponse

pytestmark = [pytest.mark.live, pytest.mark.timeout(120)]

COMPONENTS = ("database", "redis_stream", "redis_state")
REQUEST_TIMEOUT_S = 10

ALL_UP_BODY = {
    "status": "unhealthy",
    "database": "ok",
    "redis_stream": "ok",
    "redis_state": "ok",
    "hk_stock_list": "not_ready",
    "layers": {
        "entity_analysis": "error",
        "aggregation": "error",
        "scoring": "error",
        "cache": "error",
    },
}


async def _get_health(client: AsyncClient) -> tuple[httpx.Response, float]:
    started = time.monotonic()
    response = await asyncio.wait_for(client.get("/v1/health"), timeout=REQUEST_TIMEOUT_S)
    return response, time.monotonic() - started


def _body_with_errors(*failed: str) -> dict[str, object]:
    return {**ALL_UP_BODY, **{component: "error" for component in failed}}


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

    assert response.status_code == 503
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
async def get_health_dependency_down_then_recovered(infra, start_app, component, mode):
    """A stopped container refuses connections at once; a paused one never answers, so its
    check ends at HEALTH_CHECK_TIMEOUT_S (default 2s)."""
    container = infra.for_component(component)

    async with start_app() as client:
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

        assert response.status_code == 503
        assert response.json() == ALL_UP_BODY


async def get_health_all_dependencies_unresponsive(infra, start_app):
    """The three checks run concurrently, so the response takes one timeout, not three."""
    async with start_app() as client:
        for container in infra.all():
            container.pause()
        with structlog.testing.capture_logs() as logs:
            response, elapsed = await _get_health(client)

        assert response.status_code == 503
        assert response.json() == _body_with_errors(*COMPONENTS)
        assert 2 <= elapsed < 3
        assert sorted(_failed_check_warnings(logs)) == sorted(COMPONENTS)

        infra.restore_all()
        response, _ = await _get_health(client)

        assert response.status_code == 503
        assert response.json() == ALL_UP_BODY


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

    assert response.json() == ALL_UP_BODY
    assert elapsed < 2


async def get_health_check_timeout_from_config(infra, start_app):
    async with start_app(HEALTH_CHECK_TIMEOUT_S="1") as client:
        infra.postgres.pause()
        response, elapsed = await _get_health(client)

    assert response.json() == _body_with_errors("database")
    assert 1 <= elapsed < 2
