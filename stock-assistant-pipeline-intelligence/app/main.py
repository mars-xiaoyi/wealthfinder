import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from app.api.main import create_app
from app.clients.news_client import create_news_client
from app.common.hk_stock_list import refresh_hk_stock_list
from app.common.http_client import create_http_client
from app.config import load_config
from app.db.connection import create_db_client
from app.entity_analysis.cleaned_news_consumer import CleanedNewsConsumer
from app.logger import configure_logging, get_logger
from app.redis.state_client import create_state_client
from app.redis.stream_client import create_stream_client

configure_logging()
logger = get_logger(__name__)


async def _cancel_and_wait(task: asyncio.Task[None]) -> None:
    """Cancel task and wait until it has stopped."""
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


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
        create_news_client(http_client, config.entity_analysis.sadi_api_url)

        # Eager, blocking HK Stock List load. Unlike the infra above, a failure must not abort
        # startup: refresh_hk_stock_list() leaves an empty fallback cache, and GET /v1/health
        # reports not_ready until a later sync succeeds.
        try:
            await refresh_hk_stock_list()
        except Exception:
            logger.critical("hk_stock_list_startup_load_failed", exc_info=True)

        cleaned_news_consumer = CleanedNewsConsumer(config.entity_analysis, config.redis)
        consumer_task = asyncio.create_task(cleaned_news_consumer.start())
        # Registered last, so on shutdown the consumer stops before the clients it uses close.
        resources.push_async_callback(_cancel_and_wait, consumer_task)

        logger.info("startup_complete")

        yield

        logger.info("shutdown_started")


app = create_app()
app.router.lifespan_context = lifespan
