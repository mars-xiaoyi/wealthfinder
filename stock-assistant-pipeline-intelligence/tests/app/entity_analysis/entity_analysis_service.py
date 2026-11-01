from datetime import UTC, datetime
from unittest import mock
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.clients import news_client as news_client_module
from app.clients.news_client import CleanedNews, NewsClient
from app.common import hk_stock_list as hk_stock_list_module
from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.common.hk_stock_list import HKStockListCache
from app.common.stock import Stock
from app.config import EntityAnalysisConfig
from app.entity_analysis import entity_analysis_service as entity_analysis_service_module
from app.entity_analysis.entity_analysis_service import EntityAnalysisService
from app.llm import vertex_adapter as vertex_adapter_module
from app.llm.adapter import LLMAdapter, StructuredLLMResponse
from app.llm.errors import (
    LLMAuthenticationError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequestError,
    LLMSchemaViolationError,
)
from app.models.entity_event import AnalysisOutcome, EntityEvent, EventTypeEnum, SentimentEnum
from app.models.versioned_metadata import VersionedMetadata
from app.persistent import entity_event_repository as entity_event_repository_module
from app.persistent.entity_event_repository import EntityEventRepository
from app.redis import stream_client as stream_client_module
from app.redis.stream_client import RedisStreamClient
from app.skills.entity_analysis_skill import ExtractedEntity, LLMOutput

CONFIG = EntityAnalysisConfig(
    batch_size=10,
    batch_timeout_ms=5000,
    max_retry=3,
    max_input_chars=30000,
    sadi_api_url="http://sadi:8000",
    consumer_name="pod-1",
)
CLEANED_ID = UUID("00000000-0000-0000-0000-00000000000a")
TENCENT = Stock("00700", "HKEX", "騰訊控股", "TENCENT")
HSBC = Stock("00005", "HKEX", "匯豐控股", "HSBC HOLDINGS")
ARTICLE = CleanedNews(
    cleaned_id=CLEANED_ID,
    title="騰訊匯豐業績",
    body="騰訊控股（00700）及匯豐控股（00005）公佈業績。",  # noqa: RUF001
    source_url="https://example.com/a",
    source_name="MINGPAO",
    published_at=datetime(2026, 9, 8, 1, 0, tzinfo=UTC),
)


def _extracted(stock_code: str, company_name: str) -> ExtractedEntity:
    return ExtractedEntity(
        stock_code=stock_code,
        company_name=company_name,
        event_type_primary=EventTypeEnum.EARNINGS,
        event_type_secondary=None,
        sentiment_label=SentimentEnum.POSITIVE,
        sentiment_score=0.8,
        headline="業績超預期",
        entity_summary="季度收入按年增長。",
    )


def _entity_event(stock: Stock, entity_event_id: UUID | None = None) -> EntityEvent:
    return EntityEvent(
        source_id=CLEANED_ID,
        source_url=ARTICLE.source_url,
        source_name=ARTICLE.source_name,
        published_at=ARTICLE.published_at,
        analysis_outcome=VersionedMetadata(
            "v1.0.0",
            AnalysisOutcome(
                stock_code=stock.stock_code,
                event_type_primary=EventTypeEnum.EARNINGS,
                event_type_secondary=None,
                sentiment_label=SentimentEnum.POSITIVE,
                sentiment_score=0.8,
                headline="業績超預期",
                entity_summary="季度收入按年增長。",
            ),
        ),
        stock=stock,
        entity_event_id=entity_event_id,
        created_at=None if entity_event_id is None else datetime(2026, 9, 8, 2, 0, tzinfo=UTC),
    )


def _published(*entity_event_ids: UUID) -> list:
    return [
        mock.call(
            "stream:entity_event_completed",
            {"entity_event_id": str(entity_event_id), "v": "1"},
        )
        for entity_event_id in entity_event_ids
    ]


@pytest.fixture(autouse=True)
def hk_stock_list_cache(monkeypatch):
    cache = HKStockListCache({"00700": TENCENT, "00005": HSBC})
    monkeypatch.setattr(hk_stock_list_module, "_hk_stock_list_cache", cache)
    return cache


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    monkeypatch.setattr(entity_analysis_service_module, "_entity_analysis_service", None)


@pytest.fixture
def news_client(monkeypatch):
    client = AsyncMock(spec=NewsClient)
    client.fetch_cleaned_news.return_value = ARTICLE
    monkeypatch.setattr(news_client_module, "_news_client", client)
    return client


@pytest.fixture(autouse=True)
def repository(monkeypatch):
    repo = AsyncMock(spec=EntityEventRepository)
    repo.fetch_entity_event_ids_by_source_url.return_value = []
    repo.persist_entities.return_value = []
    monkeypatch.setattr(entity_event_repository_module, "_entity_event_repository", repo)
    return repo


@pytest.fixture(autouse=True)
def redis_stream(monkeypatch):
    client = AsyncMock(spec=RedisStreamClient)
    monkeypatch.setattr(stream_client_module, "_redis_stream_client", client)
    return client


@pytest.fixture
def adapter(monkeypatch):
    llm_adapter = AsyncMock(spec=LLMAdapter)
    llm_adapter.generate_structured.return_value = StructuredLLMResponse(
        output=LLMOutput(
            output_items=[_extracted("00700", "騰訊控股"), _extracted("00005", "匯豐控股")]
        ),
        input_tokens=1500,
        output_tokens=200,
        latency_ms=900,
    )
    monkeypatch.setattr(vertex_adapter_module, "_llm_adapter", llm_adapter)
    return llm_adapter


async def find_entity_events_success(news_client, adapter, repository, redis_stream):
    """Every EntityEvent is announced only after the whole article has been stored."""
    persisted = [_entity_event(TENCENT, UUID(int=1)), _entity_event(HSBC, UUID(int=2))]
    repository.persist_entities.return_value = persisted
    calls = mock.MagicMock()
    calls.attach_mock(repository.persist_entities, "persist_entities")
    calls.attach_mock(redis_stream.publish, "publish")

    entity_events = await EntityAnalysisService(CONFIG).find_entity_events(CLEANED_ID)

    assert entity_events == persisted
    news_client.fetch_cleaned_news.assert_awaited_once_with(CLEANED_ID)
    repository.fetch_entity_event_ids_by_source_url.assert_awaited_once_with(ARTICLE.source_url)
    assert calls.mock_calls == [
        mock.call.persist_entities([_entity_event(TENCENT), _entity_event(HSBC)]),
        mock.call.publish(
            "stream:entity_event_completed",
            {"entity_event_id": str(UUID(int=1)), "v": "1"},
        ),
        mock.call.publish(
            "stream:entity_event_completed",
            {"entity_event_id": str(UUID(int=2)), "v": "1"},
        ),
    ]


async def find_entity_events_partly_persisted(news_client, adapter, repository, redis_stream):
    """Only the EntityEvents actually stored are returned and announced."""
    persisted = [_entity_event(HSBC, UUID(int=2))]
    repository.persist_entities.return_value = persisted

    entity_events = await EntityAnalysisService(CONFIG).find_entity_events(CLEANED_ID)

    assert entity_events == persisted
    assert redis_stream.publish.await_args_list == _published(UUID(int=2))


async def find_entity_events_no_entities(news_client, adapter, repository, redis_stream):
    adapter.generate_structured.return_value = StructuredLLMResponse(
        output=LLMOutput(output_items=[]), input_tokens=1500, output_tokens=10, latency_ms=500
    )

    assert await EntityAnalysisService(CONFIG).find_entity_events(CLEANED_ID) == []
    repository.persist_entities.assert_awaited_once_with([])
    redis_stream.publish.assert_not_awaited()


async def find_entity_events_not_found(news_client, adapter, repository, redis_stream):
    news_client.fetch_cleaned_news.return_value = None

    assert await EntityAnalysisService(CONFIG).find_entity_events(CLEANED_ID) == []
    adapter.generate_structured.assert_not_awaited()
    repository.fetch_entity_event_ids_by_source_url.assert_not_awaited()
    repository.persist_entities.assert_not_awaited()
    redis_stream.publish.assert_not_awaited()


async def find_entity_events_already_persisted(news_client, adapter, repository, redis_stream):
    """A redelivered article that is already stored is announced again, not re-analysed."""
    repository.fetch_entity_event_ids_by_source_url.return_value = [UUID(int=1), UUID(int=2)]

    assert await EntityAnalysisService(CONFIG).find_entity_events(CLEANED_ID) == []
    repository.fetch_entity_event_ids_by_source_url.assert_awaited_once_with(ARTICLE.source_url)
    adapter.generate_structured.assert_not_awaited()
    repository.persist_entities.assert_not_awaited()
    assert redis_stream.publish.await_args_list == _published(UUID(int=1), UUID(int=2))


@pytest.mark.parametrize(
    "error_cls",
    [
        pytest.param(LLMRateLimitError, id="rate_limited"),
        pytest.param(LLMProviderError, id="provider_error"),
        pytest.param(LLMRequestError, id="request_rejected"),
        pytest.param(LLMAuthenticationError, id="authentication_failed"),
        pytest.param(LLMSchemaViolationError, id="schema_violation"),
    ],
)
async def find_entity_events_failure_reason_raised(
    news_client, adapter, repository, redis_stream, error_cls
):
    """Whichever LLM error the analysis failed with is raised as itself, not a new one."""
    error = error_cls("call failed", category=ErrorCategory.LLM)
    adapter.generate_structured.side_effect = error

    with pytest.raises(error_cls) as exc_info:
        await EntityAnalysisService(CONFIG).find_entity_events(CLEANED_ID)

    assert type(exc_info.value) is error_cls
    assert exc_info.value is error
    repository.persist_entities.assert_not_awaited()
    redis_stream.publish.assert_not_awaited()


def get_entity_analysis_service_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        entity_analysis_service_module.get_entity_analysis_service()


def create_entity_analysis_service_success(news_client):
    service = entity_analysis_service_module.create_entity_analysis_service(CONFIG)

    assert entity_analysis_service_module.get_entity_analysis_service() is service
