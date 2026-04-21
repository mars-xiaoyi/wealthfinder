import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.main import create_app
from app.cleaner.cleaning_service import CleaningService
from app.cleaner.stream_handler import StreamHandler
from app.config import load_config
from app.crawl.crawl_service import CrawlService
from app.crawl.fetchers.page_crawler import PageCrawler
from app.db.connection import create_db_client
from app.redis.stream_client import create_stream_client

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    config = load_config()
    logger.info("[main] config loaded")

    db = await create_db_client(config.db)
    logger.info("[main] database client ready")

    stream_client = await create_stream_client(config.redis_url)
    logger.info("[main] redis stream client ready")

    http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=config.crawl.request_timeout_s,
    )
    page_crawler = PageCrawler(http_client, config.crawl)
    crawl_service = CrawlService(db, stream_client, page_crawler, config.crawl)

    app.state.db = db
    app.state.stream_client = stream_client
    app.state.crawl_service = crawl_service
    app.state.config = config

    stream_handler = StreamHandler(stream_client)
    await stream_handler.ensure_consumer_group()
    cleaning_service = CleaningService(db, stream_handler, config.clean)
    cleaning_task = asyncio.create_task(cleaning_service.start())
    logger.info("[main] cleaning service started")

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    cleaning_task.cancel()
    try:
        await cleaning_task
    except asyncio.CancelledError:
        pass
    logger.info("[main] cleaning service stopped")

    await http_client.aclose()
    await stream_client.close()
    await db.close()
    logger.info("[main] all resources closed")


app = create_app()
app.router.lifespan_context = lifespan
