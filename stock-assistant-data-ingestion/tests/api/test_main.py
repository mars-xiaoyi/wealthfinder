import pytest
import redis.exceptions
from httpx import ASGITransport, AsyncClient

from app.api.main import create_app
from app.common.error_codes import CommonErrorCode
from app.common.exceptions import ServiceUnavailableException


@pytest.fixture
def app():
    return create_app()


class TestCreateApp:
    def test_title_and_version(self, app):
        assert app.title == "SADI"
        assert app.version == "1.0.0"


class TestServiceUnavailableViaGenericHandler:
    """
    A DB-unreachable condition surfaces as ServiceUnavailableException (raised
    inside DatabaseClient — see app/db/connection.py) and is routed through the
    generic sadi_exception_handler via _HTTP_STATUS_MAP. There is no dedicated
    asyncpg-specific handler: app/api/main.py must never import asyncpg.
    """

    @pytest.mark.asyncio
    async def test_returns_503(self, app):
        @app.get("/__raises_service_unavailable")
        async def _raise():
            raise ServiceUnavailableException("Database is unavailable")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/__raises_service_unavailable")

        assert resp.status_code == 503
        body = resp.json()
        assert body["error_code"] == "COMMON-5001"
        # exc.detail is a safe, fixed message set at the raise site (never the
        # raw exception string — see app/db/connection.py) — safe to surface.
        assert body["detail"] == "Database is unavailable"


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
        # docs/api.md §1.4: detail.errors is a list of {field, issue}, one per
        # missing/invalid field — CrawlRequest requires execution_id and source_name.
        fields = {e["field"] for e in body["detail"]["errors"]}
        assert fields == {"execution_id", "source_name"}
        assert all("issue" in e and e["issue"] for e in body["detail"]["errors"])


class TestMalformedJsonViaValidationHandler:
    """
    There is no dedicated json.JSONDecodeError handler (see docs/spikes.md §3.3):
    FastAPI catches it internally while parsing the request body and re-raises it
    as RequestValidationError, tagged with type "json_invalid", before it ever
    reaches the ASGI exception-handling layer. validation_error_handler detects
    that tag and returns COMMON-4000 (malformed request) rather than COMMON-4001
    (field validation failed) — docs/api.md §1.3 treats them as distinct errors.
    """

    @pytest.mark.asyncio
    async def test_malformed_json_returns_common_4000(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/v1/crawl",
                content=b"not json",
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 400
        body = resp.json()
        assert body["error_code"] == "COMMON-4000"
        assert body["detail"] == CommonErrorCode.MALFORMED_REQUEST.dev_message


class TestRedisConnectionErrorHandler:
    @pytest.mark.asyncio
    async def test_returns_503_with_reason(self, app):
        @app.get("/__raises_redis_connection_error")
        async def _raise():
            raise redis.exceptions.ConnectionError("redis down")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/__raises_redis_connection_error")

        assert resp.status_code == 503
        body = resp.json()
        assert body["error_code"] == "COMMON-5001"
        # A fixed, safe reason — never the raw redis exception string (which
        # could carry hostnames/ports).
        assert body["detail"] == "Redis is unavailable"


class TestGeneralExceptionHandler:
    @pytest.mark.asyncio
    async def test_returns_500_with_generic_reason(self, app):
        @app.get("/__raises_unexpected")
        async def _raise():
            raise RuntimeError("boom")

        # A handler registered on the base Exception class goes through Starlette's
        # ServerErrorMiddleware, which re-raises after building the response (so
        # servers/test tools can still see the traceback) — raise_app_exceptions=False
        # tells the transport to return that response instead of re-raising it here.
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/__raises_unexpected")

        assert resp.status_code == 500
        body = resp.json()
        assert body["error_code"] == "COMMON-5000"
        # Generic on purpose — we don't know what failed here, so we don't
        # echo str(exc) (unlike the two handlers above, which know exactly
        # which dependency is unavailable and can say so safely).
        assert body["detail"] == "An unexpected internal error occurred"


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
