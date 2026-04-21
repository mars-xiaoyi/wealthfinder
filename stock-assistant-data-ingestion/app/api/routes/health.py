import logging

from fastapi import APIRouter, Depends, Response

from app.api.dependencies import get_db, get_stream_client
from app.api.schemas import ComponentStatus, HealthResponse, HealthStatus
from app.db.connection import DatabaseClient
from app.redis.stream_client import StreamClient

logger = logging.getLogger(__name__)

router = APIRouter()


async def _ping_db(db: DatabaseClient) -> ComponentStatus:
    try:
        await db.fetch_one("SELECT 1")
        return ComponentStatus.OK
    except Exception:
        logger.warning("Health check: database ping failed", exc_info=True)
        return ComponentStatus.ERROR


async def _ping_redis(stream_client: StreamClient) -> ComponentStatus:
    try:
        await stream_client.ping()
        return ComponentStatus.OK
    except Exception:
        logger.warning("Health check: redis ping failed", exc_info=True)
        return ComponentStatus.ERROR


@router.get("/health", response_model=HealthResponse)
async def get_health(
    response: Response,
    db: DatabaseClient = Depends(get_db),
    stream_client: StreamClient = Depends(get_stream_client),
) -> HealthResponse:
    db_status = await _ping_db(db)
    redis_status = await _ping_redis(stream_client)

    if db_status == ComponentStatus.OK and redis_status == ComponentStatus.OK:
        status = HealthStatus.HEALTHY
    else:
        status = HealthStatus.UNHEALTHY
        response.status_code = 503

    return HealthResponse(status=status, database=db_status, redis=redis_status)
