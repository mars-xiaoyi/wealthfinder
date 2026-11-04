from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ConfigDict

from app.llm.adapter import GenerationConfig, Message, Role
from app.llm.errors import LLMError, LLMSchemaViolationError
from app.llm.vertex_adapter import get_llm_adapter
from app.logger import get_logger
from app.models.entity_event import EntityEvent
from app.models.event import BriefSummary, Event
from app.skills.skill_base import LlmCallMetric, LlmErrorEntry, SkillBase, SkillInput, SkillResult

logger = get_logger(__name__)

BRIEF_SKILL_VERSION = "v1.0.0"

_GENERATION_CONFIG = GenerationConfig(
    model="gemini-3.1-flash-lite", temperature=0, max_output_tokens=1024
)

_SUMMARY_SHORT_MAX_CHARS = 30
_SUMMARY_FULL_MAX_CHARS = 150
_KEY_NUMBERS_MAX_ITEMS = 3
_ARTICLES_PER_SOURCE = 3
_BUSINESS_VALIDATION_ERROR = "BusinessValidationError"
_SCHEMA_VIOLATION = "schema_violation"

_SYSTEM_PROMPT = """\
你是香港股票市場新聞摘要專家。你的任務是為已聚合的股票事件生成結構化摘要，
供專業投資者在晨報中快速閱讀。

所有輸出必須使用繁體中文，無論來源文章語言為何。
摘要必須基於輸入內容，不得添加輸入中未提及的信息或數據。

【summary_short】≤30繁體中文字；包含核心事件+最重要數據（如有）；不得為純描述性語句
【summary_full】≤150繁體中文字；涵蓋事件背景、核心數據，以及來源所述的影響或展望；整合多來源主要觀點；不得加入來源未有的推論或評價（如「反映…」「顯示…」）
【key_numbers】最多3個；格式：數字+單位+方向（如適用）；例："廣告收入+32% YoY"
              只提取與 event_type_primary 直接相關的數據；無明確數字時輸出 []

範例 1：
INPUT:
【實體信息】
股票代碼：00027
公司名稱：銀河娛樂
主要事件類型：EARNINGS
次要事件類型：DIVIDEND

【各文章實體摘要】
- [HKEX] 銀河娛樂公佈中期業績，淨收益按年增長9%至221億港元，經調整EBITDA升18%至68.4億港元，宣派中期股息每股0.5港元。
- [AASTOCKS] 銀娛中期經調整EBITDA增長18%，勝市場預期，管理層指中場業務需求強勁。
- [MINGPAO] 銀娛宣佈20億港元股份回購計劃，並維持全年資本開支預算。

OUTPUT:
summary_short: "銀娛中期EBITDA增18%勝預期"
summary_full: "銀河娛樂中期淨收益按年增長9%至221億港元，經調整EBITDA升18%至68.4億港元，勝市場預期，宣派中期股息每股0.5港元，另宣佈20億港元股份回購。管理層指中場業務需求強勁。"
key_numbers: ["經調整EBITDA 68.4億港元 +18% YoY", "淨收益221億港元 +9% YoY", "中期股息每股0.5港元"]

範例 2：
INPUT:
【實體信息】
股票代碼：02333
公司名稱：長城汽車
主要事件類型：REGULATORY
次要事件類型：無

【各文章實體摘要】
- [HKEX] 長城汽車公告接獲國家市場監管總局通知，就其部分車型的排放數據申報展開調查，公司表示將全面配合。
- [YAHOO_HK] 監管機構調查長城汽車排放數據申報，公司未披露涉及車型數目及調查時間表。

OUTPUT:
summary_short: "長城汽車排放數據申報遭監管調查"
summary_full: "長城汽車接獲國家市場監管總局通知，就部分車型的排放數據申報展開調查，公司表示將全面配合。涉及車型數目及調查時間表均未披露。"
key_numbers: []

範例 3：
INPUT:
【實體信息】
股票代碼：00291
公司名稱：華潤啤酒
主要事件類型：EARNINGS
次要事件類型：無

【各文章實體摘要】
- [HKEX] 華潤啤酒公佈中期業績，收入按年增長4%至245億元人民幣，惟毛利率由43.4%收窄至41.2%。
- [AASTOCKS] 華潤啤酒管理層將全年銷量指引由低單位數增長下調至持平，指內地餐飲消費轉弱。
- [MINGPAO] 華潤啤酒次高端及以上產品銷量按年升9%，喜力品牌銷量增長逾30%。

OUTPUT:
summary_short: "華潤啤酒收入增4%惟下調銷量指引"
summary_full: "華潤啤酒中期收入按年增長4%至245億元人民幣，次高端及以上產品銷量升9%，喜力品牌增長逾30%；惟毛利率由43.4%收窄至41.2%，管理層以內地餐飲消費轉弱為由，將全年銷量指引由低單位數增長下調至持平。"
key_numbers: ["收入245億元人民幣 +4% YoY", "毛利率43.4%→41.2%", "次高端及以上銷量 +9% YoY"]

1. 輸入內容無明確數字 → key_numbers 輸出 []，不得估算或捏造
2. 多來源報導角度存在明顯差異 → 在 summary_full 中整合主要觀點，不偏向單一來源
3. 實體摘要含英文內容（如英文公司名、產品名）→ 所有輸出仍使用繁體中文，數字和專有名詞保持原格式
4. 數字照錄輸入原文：不得自行加總、平均或換算；不同幣種的金額不得相加
5. 同一指標有多個不同數值（如盤中及全日股價變動）→ 採用標明「全日」或最新的數值，不得混用"""  # noqa: E501, RUF001


@dataclass
class BriefSummaryInput(SkillInput):
    event: Event
    constituent_entity_events: list[EntityEvent]
    source_weights: dict[str, float]  # authority weight per source_name


@dataclass
class InputSummary:
    entity_summary_count: int
    entity_summary_total_chars: int
    truncated_by_source_cap: bool  # a source had more articles than the per-source cap


@dataclass
class BriefSummarySkillLog:
    event_id: UUID
    skill_version: str
    fallback: bool
    fallback_reason: str | None
    input_summary: InputSummary
    llm_calls: list[LlmCallMetric]
    total_input_tokens: int
    total_output_tokens: int
    total_latency_ms: int
    llm_errors: list[LlmErrorEntry]
    timestamp: datetime


class BriefSummaryResult(SkillResult):
    model_config = ConfigDict(arbitrary_types_allowed=True)  # LLMError is not a pydantic type

    output: BriefSummary | None  # None only when failure_reason is set
    skill_log: BriefSummarySkillLog
    failure_reason: LLMError | None = None


class BriefSummarySkill(SkillBase[BriefSummaryInput, BriefSummaryResult]):
    """Summarizes an Event from its constituent EntityEvents, in Traditional Chinese."""

    def __init__(self, input: BriefSummaryInput) -> None:
        super().__init__(input)
        self._adapter = get_llm_adapter()
        articles = self._select_articles()
        self._input_summary = InputSummary(
            entity_summary_count=len(articles),
            entity_summary_total_chars=sum(
                len(article.analysis_outcome.output.entity_summary) for article in articles
            ),
            truncated_by_source_cap=len(articles) < len(input.constituent_entity_events),
        )
        self._messages = self._build_messages(articles)

    async def llm_process(self) -> BriefSummaryResult:
        """Return the Event's summary, or an extractive one when the model's answer doesn't fit
        the schema. Raises an LLMError."""
        try:
            response = await self._adapter.generate_structured(
                self._messages, BriefSummary, _GENERATION_CONFIG
            )
        except LLMSchemaViolationError as err:
            self._llm_errors.append(
                LlmErrorEntry(
                    call_index=len(self._llm_calls) + 1,
                    error_type=type(err).__name__,
                    error_detail=err.dev_message,
                    retried=False,
                )
            )
            logger.warning("brief_summary_fallback", event_id=str(self._input.event.event_id))
            return BriefSummaryResult(
                output=self._get_fallback_output(),
                skill_log=self._create_skill_log(True, _SCHEMA_VIOLATION),
            )
        self._llm_calls.append(
            LlmCallMetric(
                call_index=1,
                purpose="structured_output",
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                latency_ms=response.latency_ms,
            )
        )
        output = response.output
        self._enforce_constraints(output)
        return BriefSummaryResult(output=output, skill_log=self._create_skill_log(False, None))

    def build_fail_result(self, err: LLMError) -> BriefSummaryResult:
        return BriefSummaryResult(
            output=None, skill_log=self._create_skill_log(False, None), failure_reason=err
        )

    def _select_articles(self) -> list[EntityEvent]:
        """Each source's latest few constituents, the most authoritative source first."""
        by_source: dict[str, list[EntityEvent]] = {}
        for entity_event in self._input.constituent_entity_events:
            by_source.setdefault(entity_event.source_name, []).append(entity_event)
        selected: list[EntityEvent] = []
        for entity_events in by_source.values():
            entity_events.sort(key=_get_published_at, reverse=True)
            selected.extend(entity_events[:_ARTICLES_PER_SOURCE])
        selected.sort(key=self._get_rank, reverse=True)
        return selected

    def _build_messages(self, articles: list[EntityEvent]) -> list[Message]:
        event = self._input.event
        secondary = "、".join(event.event_type_secondary or []) or "無"
        company_name = self._input.constituent_entity_events[0].stock.company_name_zh
        summaries = "\n".join(
            f"- [{article.source_name}] {article.analysis_outcome.output.entity_summary}"
            for article in articles
        )
        user_message = (
            "【實體信息】\n"
            f"股票代碼：{event.stock_code}\n"  # noqa: RUF001
            f"公司名稱：{company_name}\n"  # noqa: RUF001
            f"主要事件類型：{event.event_type_primary}\n"  # noqa: RUF001
            f"次要事件類型：{secondary}\n"  # noqa: RUF001
            "\n"
            "【各文章實體摘要】\n"
            f"{summaries}"
        )
        return [Message(Role.SYSTEM, _SYSTEM_PROMPT), Message(Role.USER, user_message)]

    def _get_rank(self, entity_event: EntityEvent) -> tuple[float, datetime]:
        """The EntityEvent's source weight, then its publication time; higher ranks first."""
        weight = self._input.source_weights.get(entity_event.source_name, 0.0)
        return weight, _get_published_at(entity_event)

    def _get_fallback_output(self) -> BriefSummary:
        """A summary made of the top-ranked constituent's headline, without numbers."""
        top = max(self._input.constituent_entity_events, key=self._get_rank)
        headline = top.analysis_outcome.output.headline
        return BriefSummary(summary_short=headline, summary_full=headline, key_numbers=[])

    def _enforce_constraints(self, output: BriefSummary) -> None:
        """Truncate the fields that break a business constraint, recording each fix."""
        if len(output.summary_short) > _SUMMARY_SHORT_MAX_CHARS:
            output.summary_short = output.summary_short[:_SUMMARY_SHORT_MAX_CHARS]
            self._append_validation_error("summary_short truncated")
        if len(output.summary_full) > _SUMMARY_FULL_MAX_CHARS:
            output.summary_full = output.summary_full[:_SUMMARY_FULL_MAX_CHARS]
            self._append_validation_error("summary_full truncated")
        if len(output.key_numbers) > _KEY_NUMBERS_MAX_ITEMS:
            output.key_numbers = output.key_numbers[:_KEY_NUMBERS_MAX_ITEMS]
            self._append_validation_error("key_numbers truncated")

    def _append_validation_error(self, detail: str) -> None:
        self._llm_errors.append(
            LlmErrorEntry(
                call_index=1,
                error_type=_BUSINESS_VALIDATION_ERROR,
                error_detail=detail,
                retried=False,
            )
        )

    def _create_skill_log(
        self, fallback: bool, fallback_reason: str | None
    ) -> BriefSummarySkillLog:
        return BriefSummarySkillLog(
            event_id=self._input.event.event_id,
            skill_version=BRIEF_SKILL_VERSION,
            fallback=fallback,
            fallback_reason=fallback_reason,
            input_summary=self._input_summary,
            llm_calls=self._llm_calls,
            total_input_tokens=sum(c.input_tokens for c in self._llm_calls),
            total_output_tokens=sum(c.output_tokens for c in self._llm_calls),
            total_latency_ms=sum(c.latency_ms for c in self._llm_calls),
            llm_errors=self._llm_errors,
            timestamp=datetime.now(UTC),
        )


def _get_published_at(entity_event: EntityEvent) -> datetime:
    published_at = entity_event.get_published_at()
    # Set on every stored EntityEvent.
    assert published_at is not None
    return published_at
