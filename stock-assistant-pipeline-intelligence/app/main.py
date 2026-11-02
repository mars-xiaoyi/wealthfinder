import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from app.api.main import create_app
from app.clients.news_client import create_news_client
from app.common.hk_stock_list import refresh_hk_stock_list
from app.common.http_client import create_http_client
from app.config import load_config
from app.db.connection import create_db_client
from app.entity_analysis.cleaned_news_consumer import CleanedNewsConsumer
from app.entity_analysis.entity_analysis_service import create_entity_analysis_service
from app.entity_analysis.worker_pool import WorkerPool
from app.layer_tasks import Layer, register_layer_tasks
from app.llm.vertex_adapter import create_llm_adapter
from app.logger import configure_logging, get_logger
from app.persistent.entity_event_repository import create_entity_event_repository
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
        redis_stream = await create_stream_client(
            config.redis.stream_url, config.redis.reconnect_interval_s
        )
        resources.push_async_callback(redis_stream.close)
        redis_state = await create_state_client(config.redis.state_url)
        resources.push_async_callback(redis_state.close)
        http_client = create_http_client(config.http.timeout_s)
        resources.push_async_callback(http_client.aclose)
        llm_adapter = create_llm_adapter(config.llm)
        resources.push_async_callback(llm_adapter.close)
        create_news_client(http_client, config.entity_analysis.sadi_api_url)
        create_entity_event_repository()

        # Eager, blocking HK Stock List load. Unlike the infra above, a failure must not abort
        # startup: refresh_hk_stock_list() leaves an empty fallback cache, and GET /v1/health
        # reports not_ready until a later sync succeeds.
        try:
            await refresh_hk_stock_list()
        except Exception:
            logger.critical("hk_stock_list_startup_load_failed", exc_info=True)

        entity_analysis_worker_pool = WorkerPool(
            Layer.ENTITY_ANALYSIS,
            config.entity_analysis.max_concurrent,
            config.llm.rate_limit_backoff_s,
        )
        create_entity_analysis_service(config.entity_analysis)
        cleaned_news_consumer = CleanedNewsConsumer(
            entity_analysis_worker_pool, config.entity_analysis, config.redis
        )

        layer_tasks: dict[Layer, asyncio.Task[None]] = {}
        shutting_down = False
        loop = asyncio.get_running_loop()

        # A layer's start() runs its loops in one asyncio.TaskGroup, so once its Task is done
        # every loop has stopped and a restart can't run beside a leftover one.
        def _make_monitored(
            layer: Layer, start: Callable[[], Coroutine[object, object, None]]
        ) -> asyncio.Task[None]:
            """Run start() as a Task that is restarted, after a delay, whenever it crashes."""

            def _restart() -> None:
                # The restart is skipped if shutdown began during the delay.
                if not shutting_down:
                    layer_tasks[layer] = _make_monitored(layer, start)

            def _on_done(task: asyncio.Task[None]) -> None:
                exc = None if task.cancelled() else task.exception()
                if shutting_down:
                    # A restart now would outlive the clients shutdown is about to close.
                    if exc is not None:
                        logger.error("layer_task_failed_during_shutdown", layer=layer, exc_info=exc)
                    return
                if exc is None:
                    # Stopped without an error while the service runs: not restarted, but the
                    # layer is down until the service restarts.
                    logger.error("layer_task_stopped", layer=layer, cancelled=task.cancelled())
                    return
                restart_delay_s = config.health.layer_restart_delay_s
                logger.critical(
                    "layer_task_crashed", layer=layer, restart_in_s=restart_delay_s, exc_info=exc
                )
                loop.call_later(restart_delay_s, _restart)

            task = asyncio.create_task(start())
            task.add_done_callback(_on_done)
            return task

        layer_tasks[Layer.ENTITY_ANALYSIS] = _make_monitored(
            Layer.ENTITY_ANALYSIS, cleaned_news_consumer.start
        )
        register_layer_tasks(layer_tasks)

        async def _cancel_layer_tasks() -> None:
            nonlocal shutting_down
            shutting_down = True
            for task in layer_tasks.values():
                task.cancel()
            await asyncio.gather(*layer_tasks.values(), return_exceptions=True)

        # Registered last, so on shutdown the layers stop before the clients they use close.
        resources.push_async_callback(_cancel_layer_tasks)

        logger.info("startup_complete")

        yield

        logger.info("shutdown_started")


app = create_app()
app.router.lifespan_context = lifespan
