import asyncio
import logging

from fastapi import APIRouter, Depends

from app.api.dependencies import get_crawl_service
from app.api.schemas import CrawlRequest, CrawlResponse, CrawlStatus
from app.crawl.crawl_service import CrawlService

logger = logging.getLogger(__name__)

router = APIRouter()


def _log_task_exception(task: asyncio.Task) -> None:
    """add_done_callback handler — logs any exception that escapes CrawlService.execute()."""
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        logger.warning("[crawl_route] background crawl task was cancelled")
        return
    if exc is not None:
        logger.error(
            "[crawl_route] background crawl task raised an unhandled exception: %s",
            exc,
            exc_info=exc,
        )


@router.post("/crawl", response_model=CrawlResponse, status_code=202)
async def post_crawl(
    request: CrawlRequest,
    crawl_service: CrawlService = Depends(get_crawl_service),
) -> CrawlResponse:
    """
    Accept a crawl request, kick off the crawl in a background asyncio Task,
    and immediately return HTTP 202.

    The crawl runs asynchronously — `CrawlService.execute()` is responsible for
    publishing `stream:crawl_completed` (SUCCESS or FAILED) when it finishes.
    Infra health is monitored externally via `GET /v1/health`; this route does
    not pre-check DB/Redis connectivity.
    """
    logger.info(
        "[crawl_route] POST /v1/crawl received: execution_id=%s source=%s date=%s",
        request.execution_id,
        request.source_name.value,
        request.crawl_date,
    )

    task = asyncio.create_task(
        crawl_service.execute(
            request.execution_id,
            request.source_name,
            request.crawl_date,
        )
    )
    task.add_done_callback(_log_task_exception)

    logger.info(
        "[crawl_route] crawl task scheduled: execution_id=%s",
        request.execution_id,
    )
    return CrawlResponse(
        execution_id=request.execution_id,
        status=CrawlStatus.ACCEPTED,
    )
