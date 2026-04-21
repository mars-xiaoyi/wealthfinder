import pytest
from httpx import ASGITransport, AsyncClient

from app.api.main import create_app


@pytest.fixture
def app():
    return create_app()


class TestCreateApp:
    def test_title_and_version(self, app):
        assert app.title == "SADI"
        assert app.version == "1.0.0"


class TestValidationErrorHandler:
    @pytest.mark.asyncio
    async def test_returns_common_4001(self, app):
        # POST /v1/crawl with missing required fields triggers RequestValidationError
        app.state.crawl_service = None  # not reached
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/v1/crawl", json={})

        assert resp.status_code == 400
        body = resp.json()
        assert body["error_code"] == "COMMON-4001"
        assert "detail" not in body


class TestJsonDecodeErrorHandler:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_400(self, app):
        # FastAPI wraps JSONDecodeError into RequestValidationError before it reaches
        # custom exception handlers, so malformed JSON also triggers COMMON-4001.
        # The dedicated json.JSONDecodeError handler is a safety net for non-Pydantic paths.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/v1/crawl",
                content=b"not json",
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 400
        body = resp.json()
        assert body["error_code"] in ("COMMON-4000", "COMMON-4001")


class TestRouterRegistration:
    @pytest.mark.asyncio
    async def test_health_route_registered(self, app):
        # Health route requires app.state.db and app.state.stream_client, but we just
        # verify the route exists (405 or similar, not 404)
        routes = [r.path for r in app.routes]
        assert "/v1/health" in routes

    @pytest.mark.asyncio
    async def test_crawl_route_registered(self, app):
        routes = [r.path for r in app.routes]
        assert "/v1/crawl" in routes

    @pytest.mark.asyncio
    async def test_cleaned_news_routes_registered(self, app):
        routes = [r.path for r in app.routes]
        assert "/v1/cleaned_news/{cleaned_id}" in routes
        assert "/v1/cleaned_news/batch" in routes
