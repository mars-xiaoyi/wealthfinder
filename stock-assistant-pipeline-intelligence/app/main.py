from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from app.api.main import create_app
from app.common.hk_stock_list import refresh_hk_stock_list
from app.common.http_client import create_http_client
from app.config import load_config
from app.db.connection import create_db_client
from app.logger import configure_logging, get_logger
from app.redis.state_client import create_state_client
from app.redis.stream_client import create_stream_client

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # DB and Redis are mandatory infra: any failure here aborts startup. The exit stack
    # closes whatever was already opened, in reverse order, on shutdown or failed startup.
    async with AsyncExitStack() as resources:
        config = load_config()

        db = await create_db_client(config.db)
        resources.push_async_callback(db.close, config.db.close_timeout_s)
        redis_stream = await create_stream_client(config.redis.stream_url)
        resources.push_async_callback(redis_stream.close)
        redis_state = await create_state_client(config.redis.state_url)
        resources.push_async_callback(redis_state.close)
        http_client = create_http_client(config.http.timeout_s)
        resources.push_async_callback(http_client.aclose)

        # Eager, blocking HK Stock List load. Unlike the infra above, a failure must not abort
        # startup: refresh_hk_stock_list() leaves an empty fallback cache, and GET /v1/health
        # reports not_ready until a later sync succeeds.
        try:
            await refresh_hk_stock_list()
        except Exception:
            logger.critical("hk_stock_list_startup_load_failed", exc_info=True)

        logger.info("startup_complete")

        yield

        logger.info("shutdown_started")


app = create_app()
app.router.lifespan_context = lifespan
