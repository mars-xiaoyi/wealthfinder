from datetime import datetime, timezone
from uuid import uuid4

from app.models.raw_news import RawNews


class TestRawNews:
    def test_create_with_all_fields(self):
        now = datetime.now(timezone.utc)
        raw = RawNews(
            raw_id=uuid4(),
            source_name="HKEX",
            source_url="https://example.com/article/1",
            title="Test Title",
            body="Test body content",
            published_at=now,
            created_at=now,
            raw_hash="abc123",
            extra_metadata={"stock_code": "00700"},
        )
        assert raw.source_name == "HKEX"
        assert raw.is_deleted is False
        assert raw.deleted_reason is None

    def test_defaults(self):
        now = datetime.now(timezone.utc)
        raw = RawNews(
            raw_id=uuid4(),
            source_name="MINGPAO",
            source_url="https://example.com/article/2",
            title="Title",
            body="Body",
            published_at=None,
            created_at=now,
            raw_hash="def456",
            extra_metadata=None,
        )
        assert raw.published_at is None
        assert raw.extra_metadata is None
        assert raw.is_deleted is False
        assert raw.deleted_reason is None

    def test_soft_delete_fields(self):
        now = datetime.now(timezone.utc)
        raw = RawNews(
            raw_id=uuid4(),
            source_name="AASTOCKS",
            source_url="https://example.com/article/3",
            title="Title",
            body="Body",
            published_at=None,
            created_at=now,
            raw_hash="ghi789",
            extra_metadata=None,
            is_deleted=True,
            deleted_reason="DUPLICATE_TITLE",
        )
        assert raw.is_deleted is True
        assert raw.deleted_reason == "DUPLICATE_TITLE"
