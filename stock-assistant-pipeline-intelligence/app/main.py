from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from app.api.main import create_app
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
        logger.info("startup_complete")

        yield

        logger.info("shutdown_started")


app = create_app()
app.router.lifespan_context = lifespan
