from datetime import UTC
from unittest.mock import AsyncMock

import pytest

from app.common import hk_stock_list as hk_stock_list_module
from app.common.error_categories import ErrorCategory
from app.common.hk_stock_list import HKStockListCache
from app.common.stock import Stock
from app.config import EntityAnalysisConfig
from app.llm import vertex_adapter as vertex_adapter_module
from app.llm.adapter import GenerationConfig, LLMAdapter, Message, Role, StructuredLLMResponse
from app.llm.errors import (
    LLMAuthenticationError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequestError,
    LLMSchemaViolationError,
)
from app.models.entity_event import AnalysisOutcome, EventTypeEnum, SentimentEnum
from app.skills.entity_analysis_skill import (
    EntityAnalysisInput,
    EntityAnalysisResult,
    EntityAnalysisSkill,
    EntityAnalysisSkillLog,
    ExtractedEntity,
    LLMOutput,
    UnverifiedEntity,
    VerifiedEntity,
)
from app.skills.skill_base import LlmCallMetric, LlmErrorEntry

CONFIG = EntityAnalysisConfig(
    batch_size=10,
    batch_timeout_ms=5000,
    max_input_chars=30000,
    sadi_api_url="http://sadi:8000",
    consumer_name="pod-1",
)
TENCENT = Stock("00700", "HKEX", "騰訊控股", "TENCENT")
ALIBABA = Stock("09988", "HKEX", "阿里巴巴－Ｗ", "BABA-W")
HSBC = Stock("00005", "HKEX", "匯豐控股", "HSBC HOLDINGS")
CALL_METRIC = LlmCallMetric(
    call_index=1, purpose="structured_output", input_tokens=1500, output_tokens=200, latency_ms=900
)


def _extracted(stock_code: str | None, company_name: str, **overrides: object) -> ExtractedEntity:
    fields: dict[str, object] = {
        "stock_code": stock_code,
        "company_name": company_name,
        "event_type_primary": EventTypeEnum.EARNINGS,
        "event_type_secondary": None,
        "sentiment_label": SentimentEnum.POSITIVE,
        "sentiment_score": 0.8,
        "headline": "業績超預期",
        "entity_summary": "季度收入按年增長。",
    }
    return ExtractedEntity.model_validate(fields | overrides)


def _verified(stock: Stock, **overrides: object) -> VerifiedEntity:
    return VerifiedEntity(stock, _outcome(stock.stock_code, **overrides))


def _outcome(stock_code: str, **overrides: object) -> AnalysisOutcome:
    fields: dict[str, object] = {
        "stock_code": stock_code,
        "event_type_primary": EventTypeEnum.EARNINGS,
        "event_type_secondary": None,
        "sentiment_label": SentimentEnum.POSITIVE,
        "sentiment_score": 0.8,
        "headline": "業績超預期",
        "entity_summary": "季度收入按年增長。",
    }
    return AnalysisOutcome.model_validate(fields | overrides)


def _validation_error(detail: str) -> LlmErrorEntry:
    return LlmErrorEntry(
        call_index=1, error_type="BusinessValidationError", error_detail=detail, retried=False
    )


def _expected(
    result: EntityAnalysisResult,
    entity_events: list[VerifiedEntity],
    *,
    unverified_entities: list[UnverifiedEntity] | None = None,
    llm_errors: list[LlmErrorEntry] | None = None,
    input_truncated: bool = False,
) -> EntityAnalysisResult:
    """The successful result run() should return after one call; the timestamp is the
    actual one's."""
    return EntityAnalysisResult(
        entity_events=entity_events,
        skill_log=EntityAnalysisSkillLog(
            skill_version="v1.0.0",
            verified_entities=[e.stock.stock_code for e in entity_events],
            unverified_entities=unverified_entities or [],
            input_truncated=input_truncated,
            llm_calls=[CALL_METRIC],
            total_input_tokens=1500,
            total_output_tokens=200,
            total_latency_ms=900,
            llm_errors=llm_errors or [],
            timestamp=result.skill_log.timestamp,
        ),
    )


@pytest.fixture(autouse=True)
def hk_stock_list_cache(monkeypatch):
    monkeypatch.setattr(
        hk_stock_list_module,
        "_hk_stock_list_cache",
        HKStockListCache({s.stock_code: s for s in (TENCENT, ALIBABA, HSBC)}),
    )


@pytest.fixture
def adapter(monkeypatch):
    llm_adapter = AsyncMock(spec=LLMAdapter)
    monkeypatch.setattr(vertex_adapter_module, "_llm_adapter", llm_adapter)
    return llm_adapter


def _answer(adapter: AsyncMock, *extracted: ExtractedEntity) -> None:
    adapter.generate_structured.return_value = StructuredLLMResponse(
        output=LLMOutput(output_items=list(extracted)),
        input_tokens=1500,
        output_tokens=200,
        latency_ms=900,
    )


async def _run(title: str, body: str, config: EntityAnalysisConfig = CONFIG):
    return await EntityAnalysisSkill(EntityAnalysisInput(title, body), config).run()


async def run_success(adapter):
    _answer(adapter, _extracted("00700", "騰訊控股"), _extracted("9988.HK", "阿里巴巴"))

    result = await _run("騰訊阿里業績", "騰訊控股及阿里巴巴公佈業績。")

    assert result == _expected(result, [_verified(TENCENT), _verified(ALIBABA)])
    assert result.skill_log.timestamp.tzinfo is UTC
    adapter.generate_structured.assert_awaited_once()
    messages, output_schema, generation_config = adapter.generate_structured.await_args.args
    assert [m.role for m in messages] == [Role.SYSTEM, Role.USER]
    assert messages[1] == Message(
        Role.USER,
        "標題：騰訊阿里業績\n內文：騰訊控股及阿里巴巴公佈業績。",  # noqa: RUF001
    )
    assert output_schema is LLMOutput
    assert generation_config == GenerationConfig(
        model="gemini-3.1-flash-lite", temperature=0, max_output_tokens=16384
    )


@pytest.mark.parametrize(
    ("extracted", "body", "stock"),
    [
        pytest.param(_extracted("00700", "騰訊"), "騰訊業績。", TENCENT, id="code_and_name"),
        pytest.param(
            _extracted("700", "TENCENT"), "Tencent results.", TENCENT, id="short_code_and_name"
        ),
        pytest.param(
            _extracted("00700", "新公司"), "新公司(0700)業績。", TENCENT, id="code_in_parens"
        ),
        pytest.param(
            _extracted("00700", "新公司"),
            "新公司（00700）業績。",  # noqa: RUF001
            TENCENT,
            id="code_in_full_width_parens",
        ),
        pytest.param(
            _extracted("00005", "新公司"),
            "新公司(005)業績。",
            HSBC,
            id="three_digit_code_in_parens",
        ),
        pytest.param(
            _extracted("00700", "新公司"), "新公司00700.HK業績。", TENCENT, id="code_with_hk"
        ),
        pytest.param(
            _extracted("00700", "新公司"), "Stock Code: 700 業績。", TENCENT, id="stock_code_label"
        ),
        pytest.param(
            _extracted("00700", "新公司"),
            "股份代號：700 業績。",  # noqa: RUF001
            TENCENT,
            id="zh_stock_code_label",
        ),
        pytest.param(_extracted(None, "匯豐"), "匯豐業績。", HSBC, id="no_code_name_found"),
        pytest.param(_extracted("A1234", "匯豐"), "匯豐業績。", HSBC, id="malformed_code"),
        pytest.param(
            _extracted("00700", "匯豐"), "匯豐業績。", HSBC, id="code_disagrees_name_found"
        ),
        pytest.param(_extracted("01234", "匯豐"), "匯豐業績。", HSBC, id="unlisted_code"),
    ],
)
async def run_verifies_entity(adapter, extracted, body, stock):
    """A listed code is kept when the name agrees or the article prints it; otherwise the
    company name is searched."""
    _answer(adapter, extracted)

    result = await _run("標題", body)

    assert result == _expected(result, [_verified(stock)])


@pytest.mark.parametrize(
    ("extracted", "body"),
    [
        pytest.param(_extracted("00700", "新公司"), "新公司業績，700 萬元。", id="bare_number"),  # noqa: RUF001
        pytest.param(_extracted("00005", "新公司"), "要點：(5) 新公司業績。", id="list_marker"),  # noqa: RUF001
        pytest.param(_extracted(None, "蘋果公司"), "蘋果公司業績。", id="not_listed"),
    ],
)
async def run_drops_unverified_entity(adapter, extracted, body):
    _answer(adapter, extracted)

    result = await _run("標題", body)

    assert result == _expected(
        result,
        [],
        unverified_entities=[
            UnverifiedEntity(extracted.stock_code, extracted.company_name, "not_in_hk_stock_list")
        ],
    )


async def run_no_entities(adapter):
    _answer(adapter)

    result = await _run("標題", "內文")

    assert result == _expected(result, [])


async def run_enforces_constraints(adapter):
    """Out-of-range fields are fixed and recorded, never failed."""
    _answer(
        adapter,
        _extracted(
            "00700",
            "騰訊控股",
            sentiment_score=1.4,
            headline="一" * 25,
            entity_summary="二" * 120,
            event_type_secondary=EventTypeEnum.EARNINGS,
        ),
        _extracted("00005", "匯豐控股", sentiment_score=-0.2),
    )

    result = await _run("標題", "內文")

    assert result == _expected(
        result,
        [
            _verified(TENCENT, sentiment_score=1.0, headline="一" * 20, entity_summary="二" * 100),
            _verified(HSBC, sentiment_score=0.0),
        ],
        llm_errors=[
            _validation_error("00700 sentiment_score clipped"),
            _validation_error("00700 headline truncated"),
            _validation_error("00700 entity_summary truncated"),
            _validation_error("00700 event_type_secondary cleared"),
            _validation_error("00005 sentiment_score clipped"),
        ],
    )


async def run_duplicate_code(adapter):
    """Two extracted verifying to the same code keep the first."""
    _answer(
        adapter,
        _extracted("00700", "騰訊控股", headline="第一"),
        _extracted(None, "騰訊", headline="第二"),
    )

    result = await _run("標題", "內文")

    assert result == _expected(
        result,
        [_verified(TENCENT, headline="第一")],
        llm_errors=[_validation_error("00700 duplicate dropped")],
    )


async def run_input_truncated(adapter):
    """The body is cut before the call, and a code printed after the cut doesn't count."""
    _answer(adapter, _extracted("00700", "新公司"))
    config = EntityAnalysisConfig(
        batch_size=10,
        batch_timeout_ms=5000,
        max_input_chars=6,
        sadi_api_url="http://sadi:8000",
        consumer_name="pod-1",
    )

    result = await _run("標題", "新公司業績。(0700)", config)

    messages = adapter.generate_structured.await_args.args[0]
    assert messages[1] == Message(Role.USER, "標題：標題\n內文：新公司業績。")  # noqa: RUF001
    assert result == _expected(
        result,
        [],
        unverified_entities=[UnverifiedEntity("00700", "新公司", "not_in_hk_stock_list")],
        input_truncated=True,
    )


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
async def run_llm_failure(adapter, error_cls):
    """A failed call is returned as a failed result carrying that error, not raised."""
    error = error_cls("call failed", category=ErrorCategory.LLM)
    adapter.generate_structured.side_effect = error

    result = await _run("標題", "內文")

    assert result == EntityAnalysisResult(
        entity_events=None,
        skill_log=EntityAnalysisSkillLog(
            skill_version="v1.0.0",
            verified_entities=[],
            unverified_entities=[],
            input_truncated=False,
            llm_calls=[],
            total_input_tokens=0,
            total_output_tokens=0,
            total_latency_ms=0,
            llm_errors=[
                LlmErrorEntry(
                    call_index=1,
                    error_type=error_cls.__name__,
                    error_detail=error.dev_message,
                    retried=False,
                )
            ],
            timestamp=result.skill_log.timestamp,
        ),
        failure_reason=error,
    )
    assert result.failure_reason is error
