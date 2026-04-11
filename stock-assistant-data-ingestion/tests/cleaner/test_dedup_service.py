from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cleaner.dedup_service import is_duplicate


class TestIsDuplicate:
    @pytest.mark.asyncio
    async def test_returns_true_when_exists(self):
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value={"exists": True})
        assert await is_duplicate("hash", db) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_not_exists(self):
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value={"exists": False})
        assert await is_duplicate("hash", db) is False

    @pytest.mark.asyncio
    async def test_returns_false_on_none_row(self):
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value=None)
        assert await is_duplicate("hash", db) is False
