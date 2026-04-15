from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.schemas import (
    CleanedNewsBatchRequest,
    CleanedNewsBatchResponse,
    CleanedNewsRecord,
    ComponentStatus,
    CrawlRequest,
    CrawlResponse,
    CrawlStatus,
    ErrorResponse,
    HealthResponse,
    HealthStatus,
)
from app.crawl.source_name import CrawlSourceName


class TestCrawlSourceName:
    def test_all_members(self):
        assert set(CrawlSourceName) == {
            CrawlSourceName.HKEX,
            CrawlSourceName.MINGPAO,
            CrawlSourceName.AASTOCKS,
            CrawlSourceName.YAHOO_HK,
        }

    def test_string_values(self):
        assert CrawlSourceName.HKEX.value == "HKEX"
        assert CrawlSourceName.YAHOO_HK.value == "YAHOO_HK"


class TestCrawlRequest:
    def test_valid_without_date(self):
        req = CrawlRequest.model_validate(
            {"execution_id": "exec-001", "source_name": "HKEX"}
        )
        assert req.execution_id == "exec-001"
        assert req.source_name == CrawlSourceName.HKEX
        assert req.crawl_date is None

    def test_valid_with_iso8601_date_via_alias(self):
        """JSON input uses 'date' as the wire name (alias)."""
        req = CrawlRequest.model_validate(
            {"execution_id": "exec-002", "source_name": "HKEX", "date": "2026-04-06"}
        )
        assert req.crawl_date == date(2026, 4, 6)

    def test_valid_with_date_object(self):
        d = date(2026, 1, 15)
        req = CrawlRequest.model_validate(
            {"execution_id": "exec-003", "source_name": "HKEX", "date": d}
        )
        assert req.crawl_date == d

    def test_invalid_source_name_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            CrawlRequest.model_validate(
                {"execution_id": "exec-004", "source_name": "UNKNOWN"}
            )
        assert "source_name" in str(exc_info.value)

    def test_invalid_date_format_rejected(self):
        with pytest.raises(ValidationError):
            CrawlRequest.model_validate(
                {"execution_id": "exec-005", "source_name": "HKEX", "date": "20260406"}
            )

    def test_invalid_date_value_rejected(self):
        with pytest.raises(ValidationError):
            CrawlRequest.model_validate(
                {"execution_id": "exec-006", "source_name": "HKEX", "date": "2026-13-01"}
            )


class TestCrawlResponse:
    def test_default_status(self):
        resp = CrawlResponse(execution_id="exec-001")
        assert resp.status == CrawlStatus.ACCEPTED

    def test_serialisation(self):
        resp = CrawlResponse(execution_id="exec-001")
        data = resp.model_dump()
        assert data["status"] == "accepted"
        assert data["execution_id"] == "exec-001"


class TestHealthResponse:
    def test_healthy(self):
        resp = HealthResponse(status="healthy", database="ok", redis="ok")
        assert resp.status == HealthStatus.HEALTHY
        assert resp.database == ComponentStatus.OK
        assert resp.redis == ComponentStatus.OK

    def test_unhealthy(self):
        resp = HealthResponse(status="unhealthy", database="error", redis="ok")
        assert resp.status == HealthStatus.UNHEALTHY
        assert resp.database == ComponentStatus.ERROR

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            HealthResponse(status="unknown", database="ok", redis="ok")

    def test_invalid_component_status_rejected(self):
        with pytest.raises(ValidationError):
            HealthResponse(status="healthy", database="bad", redis="ok")

    def test_serialisation(self):
        resp = HealthResponse(status="healthy", database="ok", redis="ok")
        data = resp.model_dump()
        assert data["status"] == "healthy"
        assert data["database"] == "ok"


class TestCleanedNewsRecord:
    def test_create(self):
        now = datetime.now(timezone.utc)
        record = CleanedNewsRecord(
            cleaned_id=uuid4(),
            raw_id=uuid4(),
            title_cleaned="Title",
            body_cleaned="Body",
            published_at=now,
            source_name="HKEX",
            source_url="https://example.com",
            created_at=now,
        )
        assert record.source_name == "HKEX"

    def test_null_published_at(self):
        now = datetime.now(timezone.utc)
        record = CleanedNewsRecord(
            cleaned_id=uuid4(),
            raw_id=uuid4(),
            title_cleaned="Title",
            body_cleaned="Body",
            published_at=None,
            source_name="MINGPAO",
            source_url="https://example.com",
            created_at=now,
        )
        assert record.published_at is None


class TestCleanedNewsBatchRequest:
    def test_valid(self):
        ids = [uuid4() for _ in range(3)]
        req = CleanedNewsBatchRequest(cleaned_ids=ids)
        assert len(req.cleaned_ids) == 3

    def test_empty_list_rejected(self):
        with pytest.raises(ValidationError):
            CleanedNewsBatchRequest(cleaned_ids=[])

    def test_exceeds_max_rejected(self):
        ids = [uuid4() for _ in range(51)]
        with pytest.raises(ValidationError):
            CleanedNewsBatchRequest(cleaned_ids=ids)

    def test_max_boundary_accepted(self):
        ids = [uuid4() for _ in range(50)]
        req = CleanedNewsBatchRequest(cleaned_ids=ids)
        assert len(req.cleaned_ids) == 50


class TestCleanedNewsBatchResponse:
    def test_empty_results(self):
        resp = CleanedNewsBatchResponse(results=[])
        assert resp.results == []


class TestErrorResponse:
    def test_default_detail(self):
        resp = ErrorResponse(error_code="COMMON-4001", message="Validation failed")
        assert resp.detail == {}

    def test_with_dict_detail(self):
        resp = ErrorResponse(
            error_code="COMMON-4001",
            message="Validation failed",
            detail={"errors": [{"field": "source_name", "issue": "invalid"}]},
        )
        assert len(resp.detail["errors"]) == 1

    def test_with_string_detail(self):
        resp = ErrorResponse(
            error_code="COMMON-5000",
            message="Internal server error",
            detail="Unexpected null in crawler response",
        )
        assert resp.detail == "Unexpected null in crawler response"
