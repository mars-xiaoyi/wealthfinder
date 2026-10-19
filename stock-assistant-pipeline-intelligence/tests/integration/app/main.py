import time

import pytest
import redis

from app import main

pytestmark = [pytest.mark.live, pytest.mark.timeout(120)]


@pytest.mark.parametrize(
    ("component", "error"),
    [
        pytest.param("database", OSError, id="database"),
        pytest.param("redis_stream", redis.ConnectionError, id="redis_stream"),
        pytest.param("redis_state", redis.ConnectionError, id="redis_state"),
    ],
)
async def lifespan_dependency_down(infra, component, error):
    infra.for_component(component).stop()

    with pytest.raises(error):
        async with main.app.router.lifespan_context(main.app):
            pytest.fail("startup should not complete")


async def lifespan_shutdown_db_unresponsive(infra, monkeypatch):
    """Closing the pool against a paused Postgres gives up after DB_CLOSE_TIMEOUT_S."""
    monkeypatch.setenv("DB_CLOSE_TIMEOUT_S", "1")

    async with main.app.router.lifespan_context(main.app):
        infra.postgres.pause()
        started = time.monotonic()

    assert 1 <= time.monotonic() - started < 2
