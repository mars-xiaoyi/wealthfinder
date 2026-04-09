from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID


@dataclass
class CrawlErrorLog:
    error_id: UUID               # Primary key; generate with uuid.uuid4()
    execution_id: Optional[str]  # Correlation ID from POST /crawl; null if crawl triggered outside Admin context
    source_name: str             # e.g. "HKEX"
    url: str                     # Failed article URL; always present
    error_type: str              # "NETWORK" / "PARSE" / "STORAGE"
    error_code: str              # e.g. "HTTP_403", "PDF_ENCRYPTED", "TIMEOUT"
    attempt_count: int           # Total attempts made before giving up
    created_at: datetime         # UTC
