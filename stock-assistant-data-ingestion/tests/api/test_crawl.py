import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes.crawl import _log_task_exception, router
from app.crawl.crawl_service import CrawlService


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _create_test_app(crawl_service: CrawlService) -> FastAPI:
    app = FastAPI()
    app.state.crawl_service = crawl_service
    app.include_router(router, prefix="/v1")
    return app


@pytest.fixture
def mock_crawl_service():
    service = MagicMock(spec=CrawlService)
    service.execute = AsyncMock(return_value=None)
    return service


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestPostCrawlAccepted:
    @pytest.mark.asyncio
    async def test_returns_202_with_echo(self, mock_crawl_service):
        app = _create_test_app(mock_crawl_service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/v1/crawl",
                json={"execution_id": "exec-1", "source_name": "HKEX", "date": "2026-04-02"},
            )

        assert resp.status_code == 202
        body = resp.json()
        assert body["execution_id"] == "exec-1"
        assert body["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_schedules_background_task(self, mock_crawl_service):
        # Block execute() so we can observe the task is fired without finishing
        started = asyncio.Event()
        finish = asyncio.Event()

        async def fake_execute(execution_id, source_name, crawl_date):
            started.set()
            await finish.wait()

        mock_crawl_service.execute = AsyncMock(side_effect=fake_execute)

        app = _create_test_app(mock_crawl_service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/v1/crawl",
                json={"execution_id": "exec-2", "source_name": "MINGPAO"},
            )
            assert resp.status_code == 202
            # Background task should have started even though we already returned
            await asyncio.wait_for(started.wait(), timeout=1.0)
            finish.set()
            # Let the background task finish before the loop closes
            await asyncio.sleep(0)

        mock_crawl_service.execute.assert_called_once()
        args, _ = mock_crawl_service.execute.call_args
        assert args[0] == "exec-2"
        assert args[1].value == "MINGPAO"
        assert args[2] is None

    @pytest.mark.asyncio
    async def test_date_optional_for_non_hkex(self, mock_crawl_service):
        app = _create_test_app(mock_crawl_service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/v1/crawl",
                json={"execution_id": "exec-3", "source_name": "YAHOO_HK"},
            )
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestPostCrawlValidation:
    @pytest.mark.asyncio
    async def test_unknown_source_rejected(self, mock_crawl_service):
        app = _create_test_app(mock_crawl_service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/v1/crawl",
                json={"execution_id": "exec-x", "source_name": "BLOOMBERG"},
            )
        # Pydantic enum rejection — FastAPI default validation status
        assert resp.status_code == 422
        mock_crawl_service.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_execution_id_rejected(self, mock_crawl_service):
        app = _create_test_app(mock_crawl_service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/v1/crawl", json={"source_name": "HKEX"})
        assert resp.status_code == 422
        mock_crawl_service.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_date_format_rejected(self, mock_crawl_service):
        app = _create_test_app(mock_crawl_service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/v1/crawl",
                json={"execution_id": "exec-1", "source_name": "HKEX", "date": "not-a-date"},
            )
        assert resp.status_code == 422
        mock_crawl_service.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Background task done-callback
# ---------------------------------------------------------------------------


class TestLogTaskException:
    @pytest.mark.asyncio
    async def test_swallows_exception(self, caplog):
        async def boom():
            raise RuntimeError("boom")

        task = asyncio.create_task(boom())
        # Wait for the task to finish so .exception() is available
        with pytest.raises(RuntimeError):
            await task
        # Should not raise even though the task failed
        _log_task_exception(task)

    @pytest.mark.asyncio
    async def test_handles_successful_task(self):
        async def ok():
            return None

        task = asyncio.create_task(ok())
        await task
        _log_task_exception(task)  # no-op, no raise

    @pytest.mark.asyncio
    async def test_handles_cancelled_task(self):
        async def sleeper():
            await asyncio.sleep(10)

        task = asyncio.create_task(sleeper())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        _log_task_exception(task)  # no raise
