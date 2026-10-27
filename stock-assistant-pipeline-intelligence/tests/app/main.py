import asyncio
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.exceptions
from fastapi import FastAPI

from app import main as main_module
from app.common.error_categories import ErrorCategory
from app.common.errors import ParseError, ServiceUnavailableError
from app.config import (
    AppConfig,
    DatabaseConfig,
    EntityAnalysisConfig,
    HealthConfig,
    HttpConfig,
    LLMConfig,
    RedisConfig,
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
    health=HealthConfig(check_timeout_s=2),
    redis=RedisConfig(
        stream_url="redis://localhost/0", state_url="redis://localhost/1", reconnect_interval_s=5
    ),
    llm=LLMConfig(vertex_project="wealthfinder", vertex_location="global", api_timeout_s=30),
    entity_analysis=EntityAnalysisConfig(
        batch_size=10,
        batch_timeout_ms=5000,
        max_input_chars=30000,
        sadi_api_url="http://sadi:8000",
        consumer_name="sapi-entity-analysis-1",
    ),
)


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

    async def _consume_until_cancelled() -> None:
        calls.consumer_started()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            calls.consumer_cancelled()
            raise

    consumer_cls = MagicMock(return_value=MagicMock(start=_consume_until_cancelled))
    calls.attach_mock(create_db, "create_db_client")
    calls.attach_mock(create_stream, "create_stream_client")
    calls.attach_mock(create_state, "create_state_client")
    calls.attach_mock(create_http, "create_http_client")
    calls.attach_mock(refresh, "refresh_hk_stock_list")
    calls.attach_mock(create_llm, "create_llm_adapter")
    calls.attach_mock(create_news, "create_news_client")
    calls.attach_mock(create_repository, "create_entity_event_repository")
    calls.attach_mock(create_service, "create_entity_analysis_service")
    calls.attach_mock(consumer_cls, "CleanedNewsConsumer")
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
        mock.patch.object(main_module, "CleanedNewsConsumer", consumer_cls),
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
    "refresh_hk_stock_list",
    "create_entity_analysis_service",
    "CleanedNewsConsumer",
    "consumer_started",
]
SHUTDOWN_CALLS = [
    "consumer_cancelled",
    "llm_adapter_close",
    "http_client_aclose",
    "redis_state_close",
    "redis_stream_close",
    "db_close",
]


async def lifespan(infra):
    async with main_module.lifespan(FastAPI()):
        await asyncio.sleep(0)  # let the consumer Task start
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
        infra.CleanedNewsConsumer.assert_called_once_with(CONFIG.entity_analysis, CONFIG.redis)

    assert _call_names(infra) == STARTUP_CALLS + SHUTDOWN_CALLS
    infra.db_close.assert_awaited_once_with(CONFIG.db.close_timeout_s)


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
        await asyncio.sleep(0)  # let the consumer Task start
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
