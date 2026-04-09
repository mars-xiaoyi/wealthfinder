from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI

from app.api.routes.health import router
from app.db.connection import DatabaseClient
from app.redis.stream_client import StreamClient


def _create_test_app(db: DatabaseClient, stream_client: StreamClient) -> FastAPI:
    app = FastAPI()
    app.state.db = db
    app.state.stream_client = stream_client
    app.include_router(router, prefix="/v1")
    return app


@pytest.fixture
def mock_db():
    db = AsyncMock(spec=DatabaseClient)
    db.fetch_one = AsyncMock(return_value=(1,))
    return db


@pytest.fixture
def mock_stream_client():
    client = AsyncMock(spec=StreamClient)
    client._client = AsyncMock()
    client._client.ping = AsyncMock(return_value=True)
    return client


@pytest.mark.asyncio
async def test_health_all_ok(mock_db, mock_stream_client):
    app = _create_test_app(mock_db, mock_stream_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/v1/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["database"] == "ok"
    assert body["redis"] == "ok"


@pytest.mark.asyncio
async def test_health_db_down(mock_db, mock_stream_client):
    mock_db.fetch_one.side_effect = ConnectionError("db unreachable")
    app = _create_test_app(mock_db, mock_stream_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/v1/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["database"] == "error"
    assert body["redis"] == "ok"


@pytest.mark.asyncio
async def test_health_redis_down(mock_db, mock_stream_client):
    mock_stream_client._client.ping.side_effect = ConnectionError("redis unreachable")
    app = _create_test_app(mock_db, mock_stream_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/v1/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["database"] == "ok"
    assert body["redis"] == "error"


@pytest.mark.asyncio
async def test_health_both_down(mock_db, mock_stream_client):
    mock_db.fetch_one.side_effect = ConnectionError("db unreachable")
    mock_stream_client._client.ping.side_effect = ConnectionError("redis unreachable")
    app = _create_test_app(mock_db, mock_stream_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/v1/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["database"] == "error"
    assert body["redis"] == "error"
