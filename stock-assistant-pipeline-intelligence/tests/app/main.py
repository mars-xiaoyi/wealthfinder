import asyncio
import dataclasses
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.exceptions
from fastapi import FastAPI

from app import layer_tasks as layer_tasks_module
from app import main as main_module
from app.common.error_categories import ErrorCategory
from app.common.errors import ParseError, ServiceUnavailableError
from app.config import (
    AggregationConfig,
    AppConfig,
    DatabaseConfig,
    EntityAnalysisConfig,
    HealthConfig,
    HttpConfig,
    LLMConfig,
    RedisConfig,
    ScoringConfig,
)

CONFIG = AppConfig(
    db=DatabaseConfig(
        url="postgresql://localhost/sapi",
        pool_size=10,
        close_timeout_s=10,
        max_retry=3,
        retry_base_wait_ms=100,
    ),
    http=HttpConfig(timeout_s=120),
    health=HealthConfig(check_timeout_s=2, layer_restart_delay_s=0),
    redis=RedisConfig(
        stream_url="redis://localhost/0",
        state_url="redis://localhost/1",
        reconnect_interval_s=5,
        stream_claim_timeout_ms=300000,
    ),
    llm=LLMConfig(
        vertex_project="wealthfinder",
        vertex_location="global",
        api_timeout_s=30,
        rate_limit_backoff_s=60,
    ),
    entity_analysis=EntityAnalysisConfig(
        batch_size=10,
        batch_timeout_ms=5000,
        max_concurrent=3,
        max_retry=3,
        max_input_chars=30000,
        sadi_api_url="http://sadi:8000",
        consumer_name="sapi-entity-analysis-1",
    ),
    aggregation=AggregationConfig(
        batch_size=10,
        batch_timeout_ms=5000,
        max_concurrent=100,
        group_idle_timeout_s=60,
        db_flush_interval_s=300,
        sliding_window_hours=4,
        event_max_timespan_hours=24,
        consumer_name="sapi-aggregation-1",
    ),
    scoring=ScoringConfig(event_type_score=[]),
)


@pytest.fixture(autouse=True)
def reset_layer_tasks(monkeypatch):
    """Start each test with no layer Tasks registered by an earlier one."""
    monkeypatch.setattr(layer_tasks_module, "_layer_tasks", None)


@pytest.fixture
def infra():
    """Patch every create_X() main.py calls; a shared parent mock records call order."""
    calls = MagicMock()
    db, redis_stream, redis_state = AsyncMock(), AsyncMock(), AsyncMock()
    http_client = MagicMock()
    http_client.aclose = AsyncMock()
    calls.attach_mock(db.close, "db_close")
    calls.attach_mock(redis_stream.close, "redis_stream_close")
    calls.attach_mock(redis_state.close, "redis_state_close")
    calls.attach_mock(http_client.aclose, "http_client_aclose")
    llm_adapter = MagicMock()
    llm_adapter.close = AsyncMock()
    calls.attach_mock(llm_adapter.close, "llm_adapter_close")
    create_db = AsyncMock(return_value=db)
    create_stream = AsyncMock(return_value=redis_stream)
    create_state = AsyncMock(return_value=redis_state)
    create_http = MagicMock(return_value=http_client)
    refresh = AsyncMock(return_value=2800)
    create_llm = MagicMock(return_value=llm_adapter)
    create_news = MagicMock()
    create_repository = MagicMock()
    create_service = MagicMock()

    def _consume_until_cancelled(name: str):
        async def start() -> None:
            getattr(calls, f"{name}_started")()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                getattr(calls, f"{name}_cancelled")()
                raise

        return start

    consumer_cls = MagicMock(return_value=MagicMock(start=_consume_until_cancelled("consumer")))
    create_store = MagicMock()
    aggregation_service = MagicMock()
    aggregation_service.close = AsyncMock()
    calls.attach_mock(aggregation_service.close, "aggregation_service_close")
    create_aggregation_service = MagicMock(return_value=aggregation_service)
    aggregation_consumer_cls = MagicMock(
        return_value=MagicMock(start=_consume_until_cancelled("aggregation_consumer"))
    )
    worker_pool_cls = MagicMock()
    calls.attach_mock(create_db, "create_db_client")
    calls.attach_mock(create_stream, "create_stream_client")
    calls.attach_mock(create_state, "create_state_client")
    calls.attach_mock(create_http, "create_http_client")
    calls.attach_mock(refresh, "refresh_hk_stock_list")
    calls.attach_mock(create_llm, "create_llm_adapter")
    calls.attach_mock(create_news, "create_news_client")
    calls.attach_mock(create_repository, "create_entity_event_repository")
    calls.attach_mock(create_service, "create_entity_analysis_service")
    calls.attach_mock(worker_pool_cls, "WorkerPool")
    calls.attach_mock(consumer_cls, "CleanedNewsConsumer")
    calls.attach_mock(create_store, "create_event_aggregation_store")
    calls.attach_mock(create_aggregation_service, "create_aggregation_service")
    calls.attach_mock(aggregation_consumer_cls, "AggregationConsumer")
    with (
        mock.patch.object(main_module, "load_config", return_value=CONFIG),
        mock.patch.object(main_module, "create_db_client", create_db),
        mock.patch.object(main_module, "create_stream_client", create_stream),
        mock.patch.object(main_module, "create_state_client", create_state),
        mock.patch.object(main_module, "create_http_client", create_http),
        mock.patch.object(main_module, "refresh_hk_stock_list", refresh),
        mock.patch.object(main_module, "create_llm_adapter", create_llm),
        mock.patch.object(main_module, "create_news_client", create_news),
        mock.patch.object(main_module, "create_entity_event_repository", create_repository),
        mock.patch.object(main_module, "create_entity_analysis_service", create_service),
        mock.patch.object(main_module, "WorkerPool", worker_pool_cls),
        mock.patch.object(main_module, "CleanedNewsConsumer", consumer_cls),
        mock.patch.object(main_module, "create_event_aggregation_store", create_store),
        mock.patch.object(main_module, "create_aggregation_service", create_aggregation_service),
        mock.patch.object(main_module, "AggregationConsumer", aggregation_consumer_cls),
    ):
        yield calls


def _call_names(calls: MagicMock) -> list[str]:
    return [name for name, _args, _kwargs in calls.mock_calls]


STARTUP_CALLS = [
    "create_db_client",
    "create_stream_client",
    "create_state_client",
    "create_http_client",
    "create_llm_adapter",
    "create_news_client",
    "create_entity_event_repository",
    "create_event_aggregation_store",
    "refresh_hk_stock_list",
    "WorkerPool",
    "create_entity_analysis_service",
    "CleanedNewsConsumer",
    "create_aggregation_service",
    "AggregationConsumer",
    "consumer_started",
    "aggregation_consumer_started",
]
SHUTDOWN_CALLS = [
    "consumer_cancelled",
    "aggregation_consumer_cancelled",
    "aggregation_service_close",
    "llm_adapter_close",
    "http_client_aclose",
    "redis_state_close",
    "redis_stream_close",
    "db_close",
]


async def lifespan(infra):
    async with main_module.lifespan(FastAPI()):
        await asyncio.sleep(0)  # let the consumer Tasks start
        assert _call_names(infra) == STARTUP_CALLS
        infra.create_db_client.assert_awaited_once_with(CONFIG.db)
        infra.create_stream_client.assert_awaited_once_with(
            CONFIG.redis.stream_url, CONFIG.redis.reconnect_interval_s
        )
        infra.create_state_client.assert_awaited_once_with(CONFIG.redis.state_url)
        infra.create_http_client.assert_called_once_with(CONFIG.http.timeout_s)
        infra.create_llm_adapter.assert_called_once_with(CONFIG.llm)
        infra.create_news_client.assert_called_once_with(
            infra.create_http_client.return_value, CONFIG.entity_analysis.sadi_api_url
        )
        infra.create_entity_event_repository.assert_called_once_with()
        infra.refresh_hk_stock_list.assert_awaited_once_with()
        infra.create_entity_analysis_service.assert_called_once_with(CONFIG.entity_analysis)
        infra.WorkerPool.assert_called_once_with("entity_analysis", 3, 60)
        infra.CleanedNewsConsumer.assert_called_once_with(
            infra.WorkerPool.return_value, CONFIG.entity_analysis, CONFIG.redis
        )
        infra.create_event_aggregation_store.assert_called_once_with()
        infra.create_aggregation_service.assert_called_once_with(CONFIG.aggregation)
        infra.AggregationConsumer.assert_called_once_with(CONFIG.aggregation, CONFIG.redis)

        layer_tasks = layer_tasks_module.get_layer_tasks()
        assert set(layer_tasks) == {"entity_analysis", "aggregation"}
        assert not layer_tasks["entity_analysis"].done()
        assert not layer_tasks["aggregation"].done()

    assert _call_names(infra) == STARTUP_CALLS + SHUTDOWN_CALLS
    infra.db_close.assert_awaited_once_with(CONFIG.db.close_timeout_s)
    assert layer_tasks["entity_analysis"].cancelled()
    assert layer_tasks["aggregation"].cancelled()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            ServiceUnavailableError("HKEXnews unreachable", category=ErrorCategory.STOCK_LIST),
            id="unreachable",
        ),
        pytest.param(
            ParseError("shape mismatch", category=ErrorCategory.STOCK_LIST),
            id="unparseable",
        ),
    ],
)
async def lifespan_hk_stock_list_load_failed(infra, error):
    """Unlike the infra connects, a failed HK Stock List load must not abort startup."""
    infra.refresh_hk_stock_list.side_effect = error

    async with main_module.lifespan(FastAPI()):
        await asyncio.sleep(0)  # let the consumer Tasks start
        assert _call_names(infra) == STARTUP_CALLS

    assert _call_names(infra) == STARTUP_CALLS + SHUTDOWN_CALLS


async def lifespan_startup_failure(infra):
    infra.create_state_client.side_effect = redis.exceptions.ConnectionError("down")

    with pytest.raises(redis.exceptions.ConnectionError):
        async with main_module.lifespan(FastAPI()):
            pytest.fail("startup should not complete")

    assert _call_names(infra) == [
        "create_db_client",
        "create_stream_client",
        "create_state_client",
        "redis_stream_close",
        "db_close",
    ]


def _set_restart_delay(infra: MagicMock, delay_s: int) -> None:
    health = dataclasses.replace(CONFIG.health, layer_restart_delay_s=delay_s)
    main_module.load_config.return_value = dataclasses.replace(CONFIG, health=health)


async def lifespan_layer_task_crashed(infra):
    """A crashed layer is restarted on the same consumer instance once the restart delay has
    passed; until then the crashed Task stays registered."""
    _set_restart_delay(infra, 1)
    loop = asyncio.get_running_loop()
    start_times: list[float] = []
    restarted_event = asyncio.Event()

    async def crash_once() -> None:
        start_times.append(loop.time())
        infra.consumer_started()
        if len(start_times) == 1:
            raise RuntimeError("consumer loop bug")
        restarted_event.set()
        await asyncio.Event().wait()

    infra.CleanedNewsConsumer.return_value.start = crash_once

    async with main_module.lifespan(FastAPI()):
        await asyncio.sleep(0.01)  # let the first Task crash
        crashed = layer_tasks_module.get_layer_tasks()["entity_analysis"]
        assert crashed.done()
        assert isinstance(crashed.exception(), RuntimeError)
        assert _call_names(infra) == STARTUP_CALLS

        await asyncio.wait_for(restarted_event.wait(), timeout=3)

        assert start_times[1] - start_times[0] >= 0.99
        restarted = layer_tasks_module.get_layer_tasks()["entity_analysis"]
        assert restarted is not crashed
        assert not restarted.done()
        assert _call_names(infra) == [*STARTUP_CALLS, "consumer_started"]
        infra.CleanedNewsConsumer.assert_called_once()

    assert restarted.cancelled()
    assert _call_names(infra) == [*STARTUP_CALLS, "consumer_started", *SHUTDOWN_CALLS[1:]]


async def lifespan_layer_task_cancelled(infra):
    """A cancelled layer Task is not restarted."""
    async with main_module.lifespan(FastAPI()):
        await asyncio.sleep(0)  # let the consumer Task start
        cancelled = layer_tasks_module.get_layer_tasks()["entity_analysis"]
        cancelled.cancel()
        await asyncio.sleep(0.05)  # past the restart delay

        assert layer_tasks_module.get_layer_tasks()["entity_analysis"] is cancelled
        assert cancelled.cancelled()
        assert _call_names(infra) == [*STARTUP_CALLS, "consumer_cancelled"]

    assert _call_names(infra) == STARTUP_CALLS + SHUTDOWN_CALLS


async def lifespan_layer_task_returned(infra):
    """A layer Task that ends without an error is not restarted."""

    async def return_at_once() -> None:
        infra.consumer_started()

    infra.CleanedNewsConsumer.return_value.start = return_at_once

    async with main_module.lifespan(FastAPI()):
        await asyncio.sleep(0.05)  # past the restart delay
        returned = layer_tasks_module.get_layer_tasks()["entity_analysis"]

        assert returned.done()
        assert returned.exception() is None
        assert _call_names(infra) == STARTUP_CALLS

    assert _call_names(infra) == STARTUP_CALLS + SHUTDOWN_CALLS[1:]


async def lifespan_shutdown_during_restart_delay(infra):
    """A crash just before shutdown is not restarted once shutdown has begun."""
    _set_restart_delay(infra, 1)

    async def crash() -> None:
        infra.consumer_started()
        raise RuntimeError("consumer loop bug")

    infra.CleanedNewsConsumer.return_value.start = crash

    async with main_module.lifespan(FastAPI()):
        await asyncio.sleep(0.01)  # let the Task crash
        crashed = layer_tasks_module.get_layer_tasks()["entity_analysis"]

    await asyncio.sleep(1.1)  # past the restart delay

    assert layer_tasks_module.get_layer_tasks()["entity_analysis"] is crashed
    assert _call_names(infra) == STARTUP_CALLS + SHUTDOWN_CALLS[1:]


async def lifespan_layer_task_failed_during_shutdown(infra):
    """A layer Task that fails while shutdown cancels it is not restarted, and shutdown still
    closes every client."""

    async def fail_on_cancel() -> None:
        infra.consumer_started()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            infra.consumer_cancelled()
            raise RuntimeError("final flush failed") from None

    infra.CleanedNewsConsumer.return_value.start = fail_on_cancel

    async with main_module.lifespan(FastAPI()):
        await asyncio.sleep(0)  # let the consumer Task start
        failed = layer_tasks_module.get_layer_tasks()["entity_analysis"]

    await asyncio.sleep(0.05)  # past the restart delay

    assert isinstance(failed.exception(), RuntimeError)
    assert layer_tasks_module.get_layer_tasks()["entity_analysis"] is failed
    assert _call_names(infra) == STARTUP_CALLS + SHUTDOWN_CALLS
