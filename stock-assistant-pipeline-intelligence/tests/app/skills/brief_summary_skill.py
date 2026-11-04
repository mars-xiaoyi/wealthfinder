from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.common.error_categories import ErrorCategory
from app.common.stock import Stock
from app.llm import vertex_adapter as vertex_adapter_module
from app.llm.adapter import GenerationConfig, LLMAdapter, Message, Role, StructuredLLMResponse
from app.llm.errors import (
    LLMAuthenticationError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequestError,
    LLMSchemaViolationError,
)
from app.models.entity_event import AnalysisOutcome, EntityEvent, EventTypeEnum, SentimentEnum
from app.models.event import BriefSummary, EntitySource, Event
from app.models.versioned_metadata import VersionedMetadata
from app.skills.brief_summary_skill import (
    BriefSummaryInput,
    BriefSummaryResult,
    BriefSummarySkill,
    BriefSummarySkillLog,
    InputSummary,
)
from app.skills.skill_base import LlmCallMetric, LlmErrorEntry

EVENT_ID = UUID("00000000-0000-0000-0000-0000000000e1")
SEEN_AT = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
TENCENT = Stock("00700", "HKEX", "騰訊控股", "TENCENT")
WEIGHTS = {"HKEX": 9.0, "MINGPAO": 6.0, "AASTOCKS": 6.0}
CALL_METRIC = LlmCallMetric(
    call_index=1, purpose="structured_output", input_tokens=800, output_tokens=120, latency_ms=700
)
SUMMARY = BriefSummary(
    summary_short="騰訊業績超預期",
    summary_full="騰訊季度收入按年增長，盈利勝預期。",  # noqa: RUF001
    key_numbers=["收入增長 12%"],
)


def _event(event_type_secondary: list[EventTypeEnum] | None = None) -> Event:
    return Event(
        event_id=EVENT_ID,
        exchange="HKEX",
        stock_code="00700",
        event_type_primary=EventTypeEnum.EARNINGS,
        first_seen_at=SEEN_AT,
        last_seen_at=SEEN_AT,
        event_type_secondary=event_type_secondary,
        source_list=[EntitySource("HKEX", "https://example.com/HKEX", SEEN_AT)],
        aggregation_updated_at=SEEN_AT,
    )


def _entity_event(
    source_name: str,
    entity_summary: str,
    *,
    hours: int = 0,
    headline: str = "騰訊公布季度業績",
    published: bool = True,
) -> EntityEvent:
    """A stored EntityEvent published hours after SEEN_AT, or stored then without a
    publication time."""
    at = SEEN_AT + timedelta(hours=hours)
    return EntityEvent(
        source_id=UUID("00000000-0000-0000-0000-00000000000c"),
        source_url=f"https://example.com/{source_name}/{hours}",
        source_name=source_name,
        published_at=at if published else None,
        analysis_outcome=VersionedMetadata(
            "v1.0.0",
            AnalysisOutcome(
                stock_code="00700",
                event_type_primary=EventTypeEnum.EARNINGS,
                event_type_secondary=None,
                sentiment_label=SentimentEnum.POSITIVE,
                sentiment_score=0.8,
                headline=headline,
                entity_summary=entity_summary,
            ),
        ),
        stock=TENCENT,
        entity_event_id=UUID(int=hours + 1),
        created_at=at,
    )


def _skill_log(
    result: BriefSummaryResult,
    input_summary: InputSummary,
    *,
    llm_calls: list[LlmCallMetric] | None = None,
    llm_errors: list[LlmErrorEntry] | None = None,
    fallback: bool = False,
    fallback_reason: str | None = None,
) -> BriefSummarySkillLog:
    """The skill log run() should return; the timestamp is the actual one's."""
    calls = [CALL_METRIC] if llm_calls is None else llm_calls
    return BriefSummarySkillLog(
        event_id=EVENT_ID,
        skill_version="v1.0.0",
        fallback=fallback,
        fallback_reason=fallback_reason,
        input_summary=input_summary,
        llm_calls=calls,
        total_input_tokens=sum(c.input_tokens for c in calls),
        total_output_tokens=sum(c.output_tokens for c in calls),
        total_latency_ms=sum(c.latency_ms for c in calls),
        llm_errors=llm_errors or [],
        timestamp=result.skill_log.timestamp,
    )


@pytest.fixture
def adapter(monkeypatch):
    llm_adapter = AsyncMock(spec=LLMAdapter)
    monkeypatch.setattr(vertex_adapter_module, "_llm_adapter", llm_adapter)
    return llm_adapter


def _answer(adapter: AsyncMock, summary: BriefSummary) -> None:
    adapter.generate_structured.return_value = StructuredLLMResponse(
        output=summary, input_tokens=800, output_tokens=120, latency_ms=700
    )


async def _run(entity_events: list[EntityEvent], event: Event | None = None) -> BriefSummaryResult:
    return await BriefSummarySkill(
        BriefSummaryInput(event or _event(), entity_events, WEIGHTS)
    ).run()


async def run_success(adapter):
    _answer(adapter, SUMMARY.model_copy())

    result = await _run(
        [_entity_event("MINGPAO", "騰訊盈利勝預期。"), _entity_event("HKEX", "騰訊公布業績。")],
        _event([EventTypeEnum.DIVIDEND, EventTypeEnum.BUYBACK]),
    )

    assert result == BriefSummaryResult(
        output=SUMMARY,
        skill_log=_skill_log(result, InputSummary(2, 15, truncated_by_source_cap=False)),
    )
    assert result.skill_log.timestamp.tzinfo is UTC
    adapter.generate_structured.assert_awaited_once()
    messages, output_schema, generation_config = adapter.generate_structured.await_args.args
    assert [m.role for m in messages] == [Role.SYSTEM, Role.USER]
    assert messages[1] == Message(
        Role.USER,
        "【實體信息】\n"
        "股票代碼：00700\n"  # noqa: RUF001
        "公司名稱：騰訊控股\n"  # noqa: RUF001
        "主要事件類型：EARNINGS\n"  # noqa: RUF001
        "次要事件類型：DIVIDEND、BUYBACK\n"  # noqa: RUF001
        "\n"
        "【各文章實體摘要】\n"
        "- [HKEX] 騰訊公布業績。\n"
        "- [MINGPAO] 騰訊盈利勝預期。",
    )
    assert output_schema is BriefSummary
    assert generation_config == GenerationConfig(
        model="gemini-3.1-flash-lite", temperature=0, max_output_tokens=1024
    )


@pytest.mark.parametrize(
    "event_type_secondary",
    [pytest.param(None, id="none"), pytest.param([], id="empty")],
)
async def run_no_secondary_type(adapter, event_type_secondary):
    _answer(adapter, SUMMARY.model_copy())

    result = await _run([_entity_event("HKEX", "騰訊公布業績。")], _event(event_type_secondary))

    assert result == BriefSummaryResult(
        output=SUMMARY,
        skill_log=_skill_log(result, InputSummary(1, 7, truncated_by_source_cap=False)),
    )
    messages = adapter.generate_structured.await_args.args[0]
    assert messages[1] == Message(
        Role.USER,
        "【實體信息】\n"
        "股票代碼：00700\n"  # noqa: RUF001
        "公司名稱：騰訊控股\n"  # noqa: RUF001
        "主要事件類型：EARNINGS\n"  # noqa: RUF001
        "次要事件類型：無\n"  # noqa: RUF001
        "\n"
        "【各文章實體摘要】\n"
        "- [HKEX] 騰訊公布業績。",
    )


async def run_selects_articles(adapter):
    """At most 3 articles per source, the latest kept; by source weight, then latest first;
    an unknown source weighs 0. Articles without a publication time are dated by when they
    were stored."""
    _answer(adapter, SUMMARY.model_copy())

    result = await _run(
        [
            _entity_event("UNKNOWN", "未知1", hours=9),
            _entity_event("MINGPAO", "明報1", hours=1),
            _entity_event("AASTOCKS", "阿思達克1", hours=2),
            _entity_event("HKEX", "交易所1", hours=1),
            _entity_event("HKEX", "交易所2", hours=2),
            _entity_event("HKEX", "交易所3", hours=3, published=False),
            _entity_event("HKEX", "交易所4", hours=4),
        ]
    )

    assert result == BriefSummaryResult(
        output=SUMMARY,
        skill_log=_skill_log(result, InputSummary(6, 23, truncated_by_source_cap=True)),
    )
    messages = adapter.generate_structured.await_args.args[0]
    assert messages[1] == Message(
        Role.USER,
        "【實體信息】\n"
        "股票代碼：00700\n"  # noqa: RUF001
        "公司名稱：騰訊控股\n"  # noqa: RUF001
        "主要事件類型：EARNINGS\n"  # noqa: RUF001
        "次要事件類型：無\n"  # noqa: RUF001
        "\n"
        "【各文章實體摘要】\n"
        "- [HKEX] 交易所4\n"
        "- [HKEX] 交易所3\n"
        "- [HKEX] 交易所2\n"
        "- [AASTOCKS] 阿思達克1\n"
        "- [MINGPAO] 明報1\n"
        "- [UNKNOWN] 未知1",
    )


async def run_enforces_constraints(adapter):
    """Over-long fields are truncated and recorded, never failed."""
    _answer(
        adapter,
        BriefSummary(
            summary_short="一" * 31, summary_full="二" * 151, key_numbers=["1", "2", "3", "4"]
        ),
    )

    result = await _run([_entity_event("HKEX", "騰訊公布業績。")])

    assert result == BriefSummaryResult(
        output=BriefSummary(
            summary_short="一" * 30, summary_full="二" * 150, key_numbers=["1", "2", "3"]
        ),
        skill_log=_skill_log(
            result,
            InputSummary(1, 7, truncated_by_source_cap=False),
            llm_errors=[
                LlmErrorEntry(1, "BusinessValidationError", "summary_short truncated", False),
                LlmErrorEntry(1, "BusinessValidationError", "summary_full truncated", False),
                LlmErrorEntry(1, "BusinessValidationError", "key_numbers truncated", False),
            ],
        ),
    )


async def run_schema_violation_fallback(adapter):
    """The fallback is the headline of the highest-weight source's latest article."""
    error = LLMSchemaViolationError("bad json", category=ErrorCategory.LLM)
    adapter.generate_structured.side_effect = error

    result = await _run(
        [
            _entity_event("UNKNOWN", "未知", hours=5, headline="未知標題"),
            _entity_event("MINGPAO", "明報", hours=4, headline="明報標題"),
            _entity_event("HKEX", "交易所", hours=1, headline="較早標題"),
            _entity_event("HKEX", "交易所", hours=2, headline="最新標題"),
        ]
    )

    assert result == BriefSummaryResult(
        output=BriefSummary(summary_short="最新標題", summary_full="最新標題", key_numbers=[]),
        skill_log=_skill_log(
            result,
            InputSummary(4, 10, truncated_by_source_cap=False),
            llm_calls=[],
            llm_errors=[
                LlmErrorEntry(1, "LLMSchemaViolationError", error.dev_message, False),
            ],
            fallback=True,
            fallback_reason="schema_violation",
        ),
    )
    assert result.failure_reason is None


@pytest.mark.parametrize(
    "error_cls",
    [
        pytest.param(LLMRateLimitError, id="rate_limited"),
        pytest.param(LLMProviderError, id="provider_error"),
        pytest.param(LLMRequestError, id="request_rejected"),
        pytest.param(LLMAuthenticationError, id="authentication_failure"),
    ],
)
async def run_llm_failure(adapter, error_cls):
    """A failed call is returned as a failed result carrying that error, not raised."""
    error = error_cls("call failed", category=ErrorCategory.LLM)
    adapter.generate_structured.side_effect = error

    result = await _run([_entity_event("HKEX", "騰訊公布業績。")])

    assert result == BriefSummaryResult(
        output=None,
        skill_log=_skill_log(
            result,
            InputSummary(1, 7, truncated_by_source_cap=False),
            llm_calls=[],
            llm_errors=[LlmErrorEntry(1, error_cls.__name__, error.dev_message, False)],
        ),
        failure_reason=error,
    )
    assert result.failure_reason is error
