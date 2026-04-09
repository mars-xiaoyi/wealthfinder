from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass
class CleanedNews:
    cleaned_id: UUID        # Primary key; generate with uuid.uuid4() on insert
    raw_id: UUID            # FK -> raw_news.raw_id
    title_cleaned: str      # Normalised title (NFKC + whitespace stripped)
    body_cleaned: str       # Normalised plain text body
    created_at: datetime    # UTC; set to now() on insert
