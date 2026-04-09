from datetime import datetime, timezone
from uuid import uuid4

from app.models.cleaned_news import CleanedNews


class TestCleanedNews:
    def test_create(self):
        now = datetime.now(timezone.utc)
        cleaned = CleanedNews(
            cleaned_id=uuid4(),
            raw_id=uuid4(),
            title_cleaned="Normalised title",
            body_cleaned="Normalised body",
            created_at=now,
        )
        assert cleaned.title_cleaned == "Normalised title"
        assert cleaned.body_cleaned == "Normalised body"
