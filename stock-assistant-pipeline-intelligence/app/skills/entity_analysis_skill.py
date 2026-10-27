import re
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from app.common.hk_stock_list import get_hk_stock_list_cache, lookup_stock, name_matches_stock
from app.common.stock import Stock
from app.config import EntityAnalysisConfig
from app.llm.adapter import GenerationConfig, Message, Role
from app.llm.errors import LLMError
from app.llm.vertex_adapter import get_llm_adapter
from app.models.entity_event import AnalysisOutcome, EventTypeEnum, SentimentEnum
from app.skills.skill_base import LlmCallMetric, LlmErrorEntry, SkillBase, SkillInput, SkillResult

ENTITY_SKILL_VERSION = "v1.0.0"

_GENERATION_CONFIG = GenerationConfig(
    model="gemini-3.1-flash-lite", temperature=0, max_output_tokens=16384
)

_HEADLINE_MAX_CHARS = 20
_ENTITY_SUMMARY_MAX_CHARS = 100
_BUSINESS_VALIDATION_ERROR = "BusinessValidationError"
_NOT_IN_HK_STOCK_LIST = "not_in_hk_stock_list"

# A stock code as the model may write it: 2-5 digits, optionally with ".HK".
_STOCK_CODE_INPUT = re.compile(r"\d{2,5}(\.HK)?", re.IGNORECASE)

_SYSTEM_PROMPT = """\
你是香港股票市場新聞分析助手。你的任務是從財經新聞文章中提取所有提及的上市公司實體，
並為每個實體分析對應的事件類型、市場情緒與事件摘要。

規則：
- 列出文章提及的每一家公司，包括只以簡稱、英文名或在行情列表中出現的公司，即使文章未標示股票代碼
- 每個實體填寫 company_name（文章中的公司名稱）及 stock_code（文章標示的代碼；未標示時填你所知的港股代碼，不知道則留空）
- 不必自行判斷是否於 HKEX 上市：系統會以 HKEX 股票主列表核實每個實體，非 HKEX 上市者會被自動剔除

對每個實體，輸出以下分析：

【event_type_primary】主要事件類型，從以下選項中選擇最符合的一個：
EARNINGS / BUYBACK / MA / REGULATORY / MANAGEMENT_CHANGE /
ANALYST_RATING / DIVIDEND / GENERAL_ANNOUNCEMENT

【event_type_secondary】次要事件類型（可為空），與 primary 不同的第二相關類型

【sentiment_label】情緒方向：POSITIVE / NEUTRAL / NEGATIVE
- POSITIVE：對該股票具明確正面影響
- NEGATIVE：對該股票具明確負面影響
- NEUTRAL：影響不明確或屬常規公告

【sentiment_score】情緒強度 [0.0, 1.0]
- 0.0-0.3：弱信號（措辭謹慎、影響有限）
- 0.4-0.6：中等信號
- 0.7-1.0：強信號（明確財務數據、重大事件）

【headline】繁體中文事件標題，≤20字，概括該實體最重要的事件

【entity_summary】針對該實體在本文中相關內容的摘要，繁體中文，≤100字
- 只包含與該實體直接相關的事件、數據、管理層表態
- 不包含其他實體的信息或與該實體無關的市場背景

範例 1：
INPUT:
標題：騰訊控股公佈第四季業績，廣告收入按年增長32%；阿里巴巴電商業務遭監管調查
內文：騰訊控股（00700.HK）第四季廣告收入錄得強勁增長，超出市場預期。
     監管機構宣佈就阿里巴巴（09988.HK）電商業務展開正式調查。

OUTPUT:
output_items: [
  {
    stock_code: "00700", company_name: "騰訊控股",
    event_type_primary: "EARNINGS", event_type_secondary: null,
    sentiment_label: "POSITIVE", sentiment_score: 0.85,
    headline: "騰訊Q4廣告收入超預期32%",
    entity_summary: "騰訊控股第四季廣告收入按年增長32%，大幅超出市場預期，顯示廣告業務強勁復甦。"
  },
  {
    stock_code: "09988", company_name: "阿里巴巴",
    event_type_primary: "REGULATORY", event_type_secondary: null,
    sentiment_label: "NEGATIVE", sentiment_score: 0.78,
    headline: "監管機構調查阿里電商業務",
    entity_summary: "監管機構宣佈就阿里巴巴電商業務展開正式調查，涉及市場競爭行為，調查範圍及時間表未明。"
  }
]

範例 2：
INPUT:
標題：碧桂園管理層表示對下半年市場「審慎樂觀」，將控制新項目推進節奏
內文：碧桂園（02007.HK）行政總裁表示，鑑於市場環境存在不確定性，
     集團將採取更為審慎的擴張策略，暫緩部分非核心城市項目。

OUTPUT:
output_items: [
  {
    stock_code: "02007", company_name: "碧桂園",
    event_type_primary: "GENERAL_ANNOUNCEMENT", event_type_secondary: null,
    sentiment_label: "NEGATIVE", sentiment_score: 0.62,
    headline: "碧桂園審慎擴張暫緩新項目",
    entity_summary: "碧桂園管理層以市場不確定性為由，宣佈採取審慎擴張策略並暫緩非核心城市項目，措辭反映隱性負面展望。"
  }
]

範例 3：
INPUT:
標題：港交所公佈例行董事會會議日期安排
內文：香港交易所（00388.HK）公佈本年度董事會例行會議時間表，與往年安排基本一致。

OUTPUT:
output_items: [
  {
    stock_code: "00388", company_name: "香港交易所",
    event_type_primary: "GENERAL_ANNOUNCEMENT", event_type_secondary: null,
    sentiment_label: "NEUTRAL", sentiment_score: 0.10,
    headline: "港交所公佈例行董事會日期",
    entity_summary: "港交所公佈本年度例行董事會會議時間表，與往年安排一致，不含任何實質業務或財務信息。"
  }
]

範例 4：
INPUT:
標題：舜宇光學季度收入增長但毛利率下滑，管理層下調全年指引
內文：舜宇光學（02382.HK）季度收入按年增長15%，惟毛利率由28%收窄至23%，
     管理層同步下調全年盈利指引。

OUTPUT:
output_items: [
  {
    stock_code: "02382", company_name: "舜宇光學",
    event_type_primary: "EARNINGS", event_type_secondary: null,
    sentiment_label: "NEGATIVE", sentiment_score: 0.58,
    headline: "舜宇光學收入增長惟毛利率收窄",
    entity_summary: "舜宇光學季度收入按年增長15%，但毛利率由28%跌至23%，管理層下調全年盈利指引，forward-looking信號主導負面判斷。"
  }
]

1. 文章未提及任何 HKEX 上市公司 → 輸出 output_items: []
2. 文章同時提及 A 股和 H 股：只提取 HKEX 上市的 H 股實體；stock_code 填 H 股的港股代碼
3. 文章提及公司但確認非 HKEX 上市（純 A 股、美股、未上市）→ 從輸出中排除，不體現於 output_items
4. event_type_secondary 只在確實存在明確次要事件時填寫，不確定時留空優於猜測
5. 同一 stock_code 在文章中對應多個事件時，只能輸出一筆記錄：最重要的事件填入 event_type_primary，次重要的填入 event_type_secondary；不得為同一 stock_code 輸出多筆記錄"""  # noqa: E501, RUF001


@dataclass
class EntityAnalysisInput(SkillInput):
    title: str
    body: str


class ExtractedEntity(BaseModel):
    """One company the model found in the article, before verification."""

    stock_code: str | None  # the code the article prints, else the HKEX code the model knows
    company_name: str  # as written in the article
    event_type_primary: EventTypeEnum
    event_type_secondary: EventTypeEnum | None
    sentiment_label: SentimentEnum
    sentiment_score: float
    headline: str
    entity_summary: str


class LLMOutput(BaseModel):
    output_items: list[ExtractedEntity]


@dataclass
class UnverifiedEntity:
    stock_code: str | None
    company_name: str
    reason: str


@dataclass
class EntityAnalysisSkillLog:
    skill_version: str
    verified_entities: list[str]  # verified stock codes
    unverified_entities: list[UnverifiedEntity]
    input_truncated: bool  # the body was cut at max_input_chars
    llm_calls: list[LlmCallMetric]
    total_input_tokens: int
    total_output_tokens: int
    total_latency_ms: int
    llm_errors: list[LlmErrorEntry]
    timestamp: datetime


@dataclass
class VerifiedEntity:
    """One verified company: its analysis and the listed stock it was verified against."""

    stock: Stock
    outcome: AnalysisOutcome


class EntityAnalysisResult(SkillResult):
    model_config = ConfigDict(arbitrary_types_allowed=True)  # LLMError is not a pydantic type

    entity_events: list[VerifiedEntity] | None  # None only when failure_reason is set
    skill_log: EntityAnalysisSkillLog
    failure_reason: LLMError | None = None


class EntityAnalysisSkill(SkillBase[EntityAnalysisInput, EntityAnalysisResult]):
    """Finds the HKEX-listed companies an article mentions and classifies each one's event."""

    def __init__(self, input: EntityAnalysisInput, config: EntityAnalysisConfig) -> None:
        super().__init__(input)
        self._adapter = get_llm_adapter()
        self._hk_stock_list_cache = get_hk_stock_list_cache()
        self._config = config
        self._verified_entities: list[str] = []
        self._unverified_entities: list[UnverifiedEntity] = []
        self._input_truncated = False

    async def llm_process(self) -> EntityAnalysisResult:
        """Return the HKEX-listed companies the article mentions, each with its event.
        Raises an LLMError if the LLM call fails."""
        body = self._input.body[: self._config.max_input_chars]
        self._input_truncated = len(body) < len(self._input.body)
        messages = [
            Message(Role.SYSTEM, _SYSTEM_PROMPT),
            Message(Role.USER, f"標題：{self._input.title}\n內文：{body}"),  # noqa: RUF001
        ]
        response = await self._adapter.generate_structured(messages, LLMOutput, _GENERATION_CONFIG)
        self._llm_calls.append(
            LlmCallMetric(
                call_index=1,
                purpose="structured_output",
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                latency_ms=response.latency_ms,
            )
        )

        article_text = self._input.title + body
        entities: list[VerifiedEntity] = []
        for extracted in response.output.output_items:
            stock = self._verify_entity(extracted, article_text)
            if stock is None:
                self._unverified_entities.append(
                    UnverifiedEntity(
                        extracted.stock_code, extracted.company_name, _NOT_IN_HK_STOCK_LIST
                    )
                )
                continue
            outcome = AnalysisOutcome(
                stock_code=stock.stock_code,
                **extracted.model_dump(exclude={"stock_code", "company_name"}),
            )
            self._enforce_constraints(outcome)
            if outcome.stock_code in self._verified_entities:
                # The model is told not to repeat a code, but two entities can verify to one.
                self._append_validation_error(f"{outcome.stock_code} duplicate dropped")
                continue
            self._verified_entities.append(outcome.stock_code)
            entities.append(VerifiedEntity(stock, outcome))

        return EntityAnalysisResult(entity_events=entities, skill_log=self._create_skill_log())

    def build_fail_result(self, err: LLMError) -> EntityAnalysisResult:
        return EntityAnalysisResult(
            entity_events=None, skill_log=self._create_skill_log(), failure_reason=err
        )

    def _verify_entity(self, extracted: ExtractedEntity, article_text: str) -> Stock | None:
        """Resolve an extracted entity to a listed stock: its code when the name agrees or the
        article prints that code, else a search by its name."""
        code = _parse_stock_code(extracted.stock_code)
        stock = self._hk_stock_list_cache.entries.get(code) if code else None
        if stock is not None and (
            name_matches_stock(extracted.company_name, stock)
            or _code_printed(stock.stock_code, article_text)
        ):
            return stock
        return lookup_stock(extracted.company_name)

    def _enforce_constraints(self, outcome: AnalysisOutcome) -> None:
        """Clip or truncate the fields that break a business constraint, recording each fix."""
        code = outcome.stock_code
        if not 0.0 <= outcome.sentiment_score <= 1.0:
            outcome.sentiment_score = min(max(outcome.sentiment_score, 0.0), 1.0)
            self._append_validation_error(f"{code} sentiment_score clipped")
        if len(outcome.headline) > _HEADLINE_MAX_CHARS:
            outcome.headline = outcome.headline[:_HEADLINE_MAX_CHARS]
            self._append_validation_error(f"{code} headline truncated")
        if len(outcome.entity_summary) > _ENTITY_SUMMARY_MAX_CHARS:
            outcome.entity_summary = outcome.entity_summary[:_ENTITY_SUMMARY_MAX_CHARS]
            self._append_validation_error(f"{code} entity_summary truncated")
        if outcome.event_type_secondary == outcome.event_type_primary:
            outcome.event_type_secondary = None
            self._append_validation_error(f"{code} event_type_secondary cleared")

    def _append_validation_error(self, detail: str) -> None:
        self._llm_errors.append(
            LlmErrorEntry(
                call_index=1,
                error_type=_BUSINESS_VALIDATION_ERROR,
                error_detail=detail,
                retried=False,
            )
        )

    def _create_skill_log(self) -> EntityAnalysisSkillLog:
        return EntityAnalysisSkillLog(
            skill_version=ENTITY_SKILL_VERSION,
            verified_entities=self._verified_entities,
            unverified_entities=self._unverified_entities,
            input_truncated=self._input_truncated,
            llm_calls=self._llm_calls,
            total_input_tokens=sum(c.input_tokens for c in self._llm_calls),
            total_output_tokens=sum(c.output_tokens for c in self._llm_calls),
            total_latency_ms=sum(c.latency_ms for c in self._llm_calls),
            llm_errors=self._llm_errors,
            timestamp=datetime.now(UTC),
        )


def _parse_stock_code(raw: str | None) -> str | None:
    """A model-written stock code in the HK Stock List's 5-digit form, or None if it isn't one."""
    text = (raw or "").strip()
    if not _STOCK_CODE_INPUT.fullmatch(text):
        return None
    return text.upper().removesuffix(".HK").zfill(5)


def _code_printed(code: str, article_text: str) -> bool:
    """Whether the article prints code in a code-shaped form such as (0700), 00700.HK or
    "Stock Code: 700", leading zeros optional. A bare number never counts, and a bracketed one
    only with at least 3 digits, so a list marker such as (1) doesn't."""
    number = int(code)
    pattern = (
        rf"[（(]\s*(?=\d{{3}})0*{number}(?:\.HK)?\s*[)）]"  # noqa: RUF001
        rf"|(?<!\d)0*{number}\.HK"
        rf"|(?i:stock\s*codes?)\s*[:：]?\s*0*{number}(?!\d)"  # noqa: RUF001
        rf"|股份代號\s*[:：]?\s*0*{number}(?!\d)"  # noqa: RUF001
    )
    return re.search(pattern, article_text) is not None
