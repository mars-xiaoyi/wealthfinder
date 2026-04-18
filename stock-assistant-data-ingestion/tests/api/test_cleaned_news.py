from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes.cleaned_news import router
from app.db.connection import DatabaseClient


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _create_test_app(db: DatabaseClient) -> FastAPI:
    app = FastAPI()
    app.state.db = db
    app.include_router(router, prefix="/v1")
    return app


def _make_joined_row(cleaned_id=None, raw_id=None):
    return {
        "cleaned_id": cleaned_id or uuid4(),
        "raw_id": raw_id or uuid4(),
        "title_cleaned": "Cleaned Title",
        "body_cleaned": "Cleaned body text content",
        "created_at": datetime(2026, 4, 15, tzinfo=timezone.utc),
        "published_at": datetime(2026, 4, 14, tzinfo=timezone.utc),
        "source_name": "AASTOCKS",
        "source_url": "https://example.com/article",
    }


@pytest.fixture
def mock_db():
    db = MagicMock(spec=DatabaseClient)
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_all = AsyncMock(return_value=[])
    return db


# ---------------------------------------------------------------------------
# GET /v1/cleaned_news/{cleaned_id}
# ---------------------------------------------------------------------------


class TestGetCleanedNews:
    @pytest.mark.asyncio
    async def test_returns_200_when_found(self, mock_db):
        cid = uuid4()
        mock_db.fetch_one = AsyncMock(return_value=_make_joined_row(cleaned_id=cid))
        app = _create_test_app(mock_db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(f"/v1/cleaned_news/{cid}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["cleaned_id"] == str(cid)
        assert body["title_cleaned"] == "Cleaned Title"
        assert body["source_name"] == "AASTOCKS"

    @pytest.mark.asyncio
    async def test_returns_404_when_not_found(self, mock_db):
        mock_db.fetch_one = AsyncMock(return_value=None)
        app = _create_test_app(mock_db)

        cid = uuid4()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(f"/v1/cleaned_news/{cid}")

        assert resp.status_code == 404
        body = resp.json()
        assert body["error_code"] == "COMMON-4004"

    @pytest.mark.asyncio
    async def test_invalid_uuid_returns_422(self, mock_db):
        app = _create_test_app(mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/v1/cleaned_news/not-a-uuid")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /v1/cleaned_news/batch
# ---------------------------------------------------------------------------


class TestPostCleanedNewsBatch:
    @pytest.mark.asyncio
    async def test_returns_found_records(self, mock_db):
        cid1 = uuid4()
        cid2 = uuid4()
        mock_db.fetch_all = AsyncMock(
            return_value=[_make_joined_row(cleaned_id=cid1), _make_joined_row(cleaned_id=cid2)]
        )
        app = _create_test_app(mock_db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/v1/cleaned_news/batch",
                json={"cleaned_ids": [str(cid1), str(cid2)]},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) == 2

    @pytest.mark.asyncio
    async def test_missing_ids_silently_omitted(self, mock_db):
        mock_db.fetch_all = AsyncMock(return_value=[])
        app = _create_test_app(mock_db)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/v1/cleaned_news/batch",
                json={"cleaned_ids": [str(uuid4())]},
            )

        assert resp.status_code == 200
        assert resp.json()["results"] == []

    @pytest.mark.asyncio
    async def test_empty_list_rejected(self, mock_db):
        app = _create_test_app(mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/v1/cleaned_news/batch", json={"cleaned_ids": []})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_over_50_rejected(self, mock_db):
        app = _create_test_app(mock_db)
        ids = [str(uuid4()) for _ in range(51)]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/v1/cleaned_news/batch", json={"cleaned_ids": ids})
        assert resp.status_code == 422
