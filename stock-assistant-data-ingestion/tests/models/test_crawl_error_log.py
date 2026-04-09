from datetime import datetime, timezone
from uuid import uuid4

from app.models.crawl_error_log import CrawlErrorLog


class TestCrawlErrorLog:
    def test_create_with_execution_id(self):
        now = datetime.now(timezone.utc)
        err = CrawlErrorLog(
            error_id=uuid4(),
            execution_id="ext-exec-12345",
            source_name="HKEX",
            url="https://example.com/broken",
            error_type="NETWORK",
            error_code="HTTP_403",
            attempt_count=3,
            created_at=now,
        )
        assert err.error_type == "NETWORK"
        assert err.attempt_count == 3

    def test_create_without_execution_id(self):
        now = datetime.now(timezone.utc)
        err = CrawlErrorLog(
            error_id=uuid4(),
            execution_id=None,
            source_name="MINGPAO",
            url="https://example.com/timeout",
            error_type="NETWORK",
            error_code="TIMEOUT",
            attempt_count=1,
            created_at=now,
        )
        assert err.execution_id is None
