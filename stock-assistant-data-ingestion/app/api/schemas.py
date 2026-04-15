from datetime import date, datetime
from enum import Enum
from typing import List, Optional, Union
from uuid import UUID

from pydantic import BaseModel, Field

from app.crawl.source_name import CrawlSourceName


# -- Enums --------------------------------------------------------------------

class CrawlStatus(str, Enum):
    ACCEPTED = "accepted"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ComponentStatus(str, Enum):
    OK = "ok"
    ERROR = "error"


# -- POST /v1/crawl -----------------------------------------------------------

class CrawlRequest(BaseModel):
    execution_id: str
    source_name: CrawlSourceName
    crawl_date: Optional[date] = Field(default=None, alias="date")  # ISO 8601 "YYYY-MM-DD"; only used by HKEX


class CrawlResponse(BaseModel):
    execution_id: str
    status: CrawlStatus = CrawlStatus.ACCEPTED


# -- GET /v1/health ------------------------------------------------------------

class HealthResponse(BaseModel):
    status: HealthStatus
    database: ComponentStatus
    redis: ComponentStatus


# -- GET /v1/cleaned_news/{cleaned_id} and POST /v1/cleaned_news/batch ---------

class CleanedNewsRecord(BaseModel):
    cleaned_id: UUID
    raw_id: UUID
    title_cleaned: str
    body_cleaned: str
    published_at: Optional[datetime]    # From raw_news; null if unavailable
    source_name: str                    # From raw_news
    source_url: str                     # From raw_news
    created_at: datetime                # cleaned_news.created_at


class CleanedNewsBatchRequest(BaseModel):
    cleaned_ids: List[UUID] = Field(..., min_length=1, max_length=50)


class CleanedNewsBatchResponse(BaseModel):
    results: List[CleanedNewsRecord]


# -- Error Response (used by all routes) ---------------------------------------

class ErrorResponse(BaseModel):
    error_code: str
    message: str
    detail: Union[str, dict] = {}
