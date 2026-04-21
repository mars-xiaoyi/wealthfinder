import logging
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.dependencies import get_db
from app.api.schemas import (
    CleanedNewsBatchRequest,
    CleanedNewsBatchResponse,
    CleanedNewsRecord,
)
from app.common.exceptions import NotFoundException
from app.db.connection import DatabaseClient

logger = logging.getLogger(__name__)

router = APIRouter()

_SINGLE_QUERY = """
    SELECT c.cleaned_id, c.raw_id, c.title_cleaned, c.body_cleaned, c.created_at,
           r.published_at, r.source_name, r.source_url
    FROM cleaned_news c
    JOIN raw_news r ON c.raw_id = r.raw_id
    WHERE c.cleaned_id = $1
"""

_BATCH_QUERY = """
    SELECT c.cleaned_id, c.raw_id, c.title_cleaned, c.body_cleaned, c.created_at,
           r.published_at, r.source_name, r.source_url
    FROM cleaned_news c
    JOIN raw_news r ON c.raw_id = r.raw_id
    WHERE c.cleaned_id = ANY($1)
"""


def _row_to_record(row) -> CleanedNewsRecord:
    return CleanedNewsRecord(
        cleaned_id=row["cleaned_id"],
        raw_id=row["raw_id"],
        title_cleaned=row["title_cleaned"],
        body_cleaned=row["body_cleaned"],
        published_at=row["published_at"],
        source_name=row["source_name"],
        source_url=row["source_url"],
        created_at=row["created_at"],
    )


@router.get("/cleaned_news/{cleaned_id}", response_model=CleanedNewsRecord)
async def get_cleaned_news(
    cleaned_id: UUID,
    db: DatabaseClient = Depends(get_db),
) -> CleanedNewsRecord:
    logger.info("[cleaned_news] GET /v1/cleaned_news/%s", cleaned_id)
    row = await db.fetch_one(_SINGLE_QUERY, cleaned_id)
    if row is None:
        raise NotFoundException(f"cleaned_id {cleaned_id} not found")
    return _row_to_record(row)


@router.post("/cleaned_news/batch", response_model=CleanedNewsBatchResponse)
async def post_cleaned_news_batch(
    request: CleanedNewsBatchRequest,
    db: DatabaseClient = Depends(get_db),
) -> CleanedNewsBatchResponse:
    logger.info(
        "[cleaned_news] POST /v1/cleaned_news/batch: %d ids", len(request.cleaned_ids)
    )
    rows = await db.fetch_all(_BATCH_QUERY, request.cleaned_ids)
    results = [_row_to_record(row) for row in rows]
    return CleanedNewsBatchResponse(results=results)
