from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.cleaner.cleaning_service import CleaningService
from app.cleaner.stream_handler import StreamHandler
from app.config import CleanConfig
from app.db.connection import DatabaseClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_config():
    return CleanConfig(
        worker_concurrency=2,
        body_min_length=50,
        stream_claim_timeout_ms=30000,
    )


@pytest.fixture
def mock_db():
    db = MagicMock(spec=DatabaseClient)
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_all = AsyncMock(return_value=[])
    db.execute = AsyncMock()
    return db


@pytest.fixture
def mock_stream_handler():
    handler = MagicMock(spec=StreamHandler)
    handler.ensure_consumer_group = AsyncMock()
    handler.read_messages = AsyncMock(return_value=[])
    handler.reclaim_pending = AsyncMock(return_value=[])
    handler.ack = AsyncMock()
    handler.publish_cleaned = AsyncMock()
    return handler


@pytest.fixture
def service(mock_db, mock_stream_handler, clean_config):
    return CleaningService(mock_db, mock_stream_handler, clean_config)


def _make_raw_row(
    raw_id=None,
    title="Some Title",
    body="A" * 100,
    published_at=None,
    source_name="AASTOCKS",
    source_url="https://example.com/1",
    raw_hash="abc123",
    is_deleted=False,
    deleted_reason=None,
):
    """Create a dict simulating an asyncpg Record for raw_news."""
    return {
        "raw_id": raw_id or uuid4(),
        "source_name": source_name,
        "source_url": source_url,
        "title": title,
        "body": body,
        "published_at": published_at,
        "created_at": datetime.now(timezone.utc),
        "raw_hash": raw_hash,
        "extra_metadata": None,
        "is_deleted": is_deleted,
        "deleted_reason": deleted_reason,
    }


# ---------------------------------------------------------------------------
# process_record — happy path
# ---------------------------------------------------------------------------


class TestProcessRecordSuccess:
    @pytest.mark.asyncio
    async def test_inserts_cleaned_news_and_publishes(self, service, mock_db, mock_stream_handler):
        raw_id = uuid4()
        raw_row = _make_raw_row(raw_id=raw_id, title="Test Title", body="B" * 100)

        # fetch_one call order: _fetch_raw_news → _already_cleaned (not found)
        mock_db.fetch_one = AsyncMock(side_effect=[raw_row, None])

        await service.process_record(raw_id)

        mock_db.execute.assert_called_once()
        insert_call = mock_db.execute.call_args
        assert "INSERT INTO cleaned_news" in insert_call.args[0]
        assert insert_call.args[2] == raw_id

        mock_stream_handler.publish_cleaned.assert_called_once()


# ---------------------------------------------------------------------------
# process_record — idempotency
# ---------------------------------------------------------------------------


class TestProcessRecordIdempotency:
    @pytest.mark.asyncio
    async def test_skips_already_cleaned(self, service, mock_db, mock_stream_handler):
        raw_id = uuid4()
        raw_row = _make_raw_row(raw_id=raw_id)

        # _fetch_raw_news returns row, _already_cleaned returns a row → skip
        mock_db.fetch_one = AsyncMock(side_effect=[raw_row, {"1": 1}])

        await service.process_record(raw_id)

        mock_db.execute.assert_not_called()
        mock_stream_handler.publish_cleaned.assert_not_called()


# ---------------------------------------------------------------------------
# process_record — rejections
# ---------------------------------------------------------------------------


class TestProcessRecordEmptyField:
    @pytest.mark.asyncio
    async def test_empty_title_marks_deleted(self, service, mock_db, mock_stream_handler):
        raw_id = uuid4()
        raw_row = _make_raw_row(raw_id=raw_id, title="", body="B" * 100)
        mock_db.fetch_one = AsyncMock(return_value=raw_row)

        await service.process_record(raw_id)

        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args.args
        assert "UPDATE raw_news SET is_deleted" in call_args[0]
        assert call_args[1] == "EMPTY_FIELD"
        mock_stream_handler.publish_cleaned.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_body_marks_deleted(self, service, mock_db, mock_stream_handler):
        raw_id = uuid4()
        raw_row = _make_raw_row(raw_id=raw_id, title="Title", body="")
        mock_db.fetch_one = AsyncMock(return_value=raw_row)

        await service.process_record(raw_id)

        call_args = mock_db.execute.call_args.args
        assert call_args[1] == "EMPTY_FIELD"


class TestProcessRecordBodyTooShort:
    @pytest.mark.asyncio
    async def test_short_body_marks_deleted(self, service, mock_db, mock_stream_handler):
        raw_id = uuid4()
        raw_row = _make_raw_row(raw_id=raw_id, body="short")  # len=5 < 50
        mock_db.fetch_one = AsyncMock(return_value=raw_row)

        await service.process_record(raw_id)

        call_args = mock_db.execute.call_args.args
        assert call_args[1] == "BODY_TOO_SHORT"
        mock_stream_handler.publish_cleaned.assert_not_called()


# ---------------------------------------------------------------------------
# process_record — raw_news not found
# ---------------------------------------------------------------------------


class TestProcessRecordNotFound:
    @pytest.mark.asyncio
    async def test_missing_raw_news_skips(self, service, mock_db, mock_stream_handler):
        raw_id = uuid4()
        mock_db.fetch_one = AsyncMock(return_value=None)

        await service.process_record(raw_id)

        mock_db.execute.assert_not_called()
        mock_stream_handler.publish_cleaned.assert_not_called()


# ---------------------------------------------------------------------------
# process_record — DB failure propagates
# ---------------------------------------------------------------------------


class TestProcessRecordDbFailure:
    @pytest.mark.asyncio
    async def test_insert_failure_raises(self, service, mock_db, mock_stream_handler):
        raw_id = uuid4()
        raw_row = _make_raw_row(raw_id=raw_id, body="B" * 100)

        mock_db.fetch_one = AsyncMock(side_effect=[raw_row, None])
        mock_db.execute = AsyncMock(side_effect=RuntimeError("DB down"))

        with pytest.raises(RuntimeError, match="DB down"):
            await service.process_record(raw_id)

        mock_stream_handler.publish_cleaned.assert_not_called()


# ---------------------------------------------------------------------------
# _already_cleaned
# ---------------------------------------------------------------------------


class TestAlreadyCleaned:
    @pytest.mark.asyncio
    async def test_returns_true_when_exists(self, service, mock_db):
        mock_db.fetch_one = AsyncMock(return_value={"1": 1})
        assert await service._already_cleaned(uuid4()) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_not_exists(self, service, mock_db):
        mock_db.fetch_one = AsyncMock(return_value=None)
        assert await service._already_cleaned(uuid4()) is False


# ---------------------------------------------------------------------------
# _mark_deleted
# ---------------------------------------------------------------------------


class TestMarkDeleted:
    @pytest.mark.asyncio
    async def test_updates_raw_news(self, service, mock_db):
        raw_id = uuid4()
        await service._mark_deleted(raw_id, "BODY_TOO_SHORT")

        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args.args
        assert "UPDATE raw_news" in call_args[0]
        assert call_args[1] == "BODY_TOO_SHORT"
        assert call_args[2] == raw_id


# ---------------------------------------------------------------------------
# _fetch_raw_news
# ---------------------------------------------------------------------------


class TestFetchRawNews:
    @pytest.mark.asyncio
    async def test_returns_raw_news_dataclass(self, service, mock_db):
        raw_id = uuid4()
        mock_db.fetch_one = AsyncMock(return_value=_make_raw_row(raw_id=raw_id))

        result = await service._fetch_raw_news(raw_id)
        assert result is not None
        assert result.raw_id == raw_id

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, service, mock_db):
        mock_db.fetch_one = AsyncMock(return_value=None)
        result = await service._fetch_raw_news(uuid4())
        assert result is None
