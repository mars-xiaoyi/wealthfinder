"""Reference implementation of the chosen EntityAnalysisSkill design, for the VertexAIAdapter / EntityAnalysisSkill
tickets to read and run against live Vertex AI. Throwaway code: one file, no app/ imports.

- generate_structured(): implementation.md §7.3 — native structured output, explicit timeout, no SDK retry,
  token accounting with thinking tokens, provider-error mapping.
- EntityAnalysis: implementation.md §8.2 — the system prompt is read from the current implementation.md, one call,
  code-side verification of every listed company, business-constraint validation, dedup.

Both name checks reuse the lookup_stock fuzzy-matching spike's code, imported at runtime (fuzzy_import.py): its
name forms for names_agree() (step 1) and its S8 search for lookup_stock() (step 3) — §4.2 in the service.

    .venv/bin/python reference_entity_analysis.py --title "..." --body "..."
    .venv/bin/python reference_entity_analysis.py --cleaned-id <uuid>      # article from poc/sapi/cleaned_news_export.csv
"""

import argparse
import asyncio
import csv
import json
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

HERE = Path(__file__).resolve().parent
IMPL_MD = HERE.parents[2] / "stock-assistant-pipeline-intelligence" / "docs" / "implementation.md"
EXPORT_CSV = HERE.parent / "cleaned_news_export.csv"

PROJECT = "wealthfinder"
LOCATION = "global"
API_TIMEOUT_S = 30                 # LLM_API_TIMEOUT_S
MAX_INPUT_CHARS = 30_000           # ENTITY_ANALYSIS_MAX_INPUT_CHARS

T = TypeVar("T", bound=BaseModel)


# ── Adapter (§7) ───────────────────────────────────────────────────────────────────────────────────
class LLMError(Exception): ...
class LLMRateLimitError(LLMError): ...
class LLMProviderError(LLMError): ...
class LLMAuthenticationError(LLMError): ...
class LLMSchemaViolationError(LLMError): ...


@dataclass(frozen=True)
class GenerationConfig:
    model: str
    temperature: float
    max_output_tokens: int


@dataclass
class StructuredLLMResponse:
    output: BaseModel
    input_tokens: int
    output_tokens: int
    latency_ms: int


def make_client() -> genai.Client:
    # No default timeout in the SDK, so set one; leave retry_options unset (one attempt per call).
    return genai.Client(vertexai=True, project=PROJECT, location=LOCATION,
                        http_options=types.HttpOptions(timeout=API_TIMEOUT_S * 1000))


async def generate_structured(client: genai.Client, system: str, user: str, schema: type[T],
                              cfg: GenerationConfig) -> StructuredLLMResponse:
    config = types.GenerateContentConfig(
        system_instruction=system, temperature=cfg.temperature, max_output_tokens=cfg.max_output_tokens,
        response_mime_type="application/json", response_json_schema=schema.model_json_schema())
    t0 = time.monotonic()
    try:
        r = await client.aio.models.generate_content(model=cfg.model, contents=user, config=config)
    except errors.APIError as e:
        if e.code == 429:
            raise LLMRateLimitError(str(e)) from e
        if e.code in (401, 403):
            raise LLMAuthenticationError(str(e)) from e
        raise LLMProviderError(str(e)) from e          # incl. the SDK's own timeout, reported as 504
    except Exception as e:                              # network errors
        raise LLMProviderError(f"{type(e).__name__}: {e}") from e
    latency_ms = int((time.monotonic() - t0) * 1000)
    try:
        output = schema.model_validate_json(r.text or "")
    except ValidationError as e:                        # e.g. JSON cut off at max_output_tokens
        raise LLMSchemaViolationError(str(e)[:300]) from e
    u = r.usage_metadata
    return StructuredLLMResponse(output=output, input_tokens=u.prompt_token_count or 0,
                                 output_tokens=(u.candidates_token_count or 0) + (u.thoughts_token_count or 0),
                                 latency_ms=latency_ms)


# ── Skill schemas (§8.2) ───────────────────────────────────────────────────────────────────────────
class EventTypeEnum(str, Enum):
    EARNINGS = "EARNINGS"
    BUYBACK = "BUYBACK"
    MA = "MA"
    REGULATORY = "REGULATORY"
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    ANALYST_RATING = "ANALYST_RATING"
    DIVIDEND = "DIVIDEND"
    GENERAL_ANNOUNCEMENT = "GENERAL_ANNOUNCEMENT"


class SentimentEnum(str, Enum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"


class EntityMention(BaseModel):
    stock_code: str | None
    company_name: str
    event_type_primary: EventTypeEnum
    event_type_secondary: EventTypeEnum | None
    sentiment_label: SentimentEnum
    sentiment_score: float
    headline: str
    entity_summary: str


class LLMOutput(BaseModel):
    output_items: list[EntityMention]


class AnalysisOutcome(BaseModel):
    stock_code: str
    event_type_primary: EventTypeEnum
    event_type_secondary: EventTypeEnum | None
    sentiment_label: SentimentEnum
    sentiment_score: float
    headline: str
    entity_summary: str


GENERATION_CONFIG = GenerationConfig(model="gemini-3.1-flash-lite", temperature=0, max_output_tokens=16384)


def system_prompt() -> str:
    """§8.2's Parts 1, 2, 3 (few-shots) and 4, read from the current implementation.md."""
    text = IMPL_MD.read_text()
    sec = text[text.index("### 8.2 "):text.index("### 8.3 ")]
    sec = sec[sec.index("#### System Prompt"):sec.index("#### Output Schemas")]
    fences = dict(re.findall(r"\*(Part \d)[^*]*\*:?\s*\n```\n(.*?)```", sec, re.S))
    shots = re.findall(r"\*\*Few-shot Example \d[^*]*\*\*\s*\n```\n(.*?)```", sec, re.S)
    few_shot = "\n\n".join(f"範例 {i}：\n{s.strip()}" for i, s in enumerate(shots, 1))
    return "\n\n".join([fences["Part 1"].strip(), fences["Part 2"].strip(), few_shot, fences["Part 4"].strip()])


# ── Code-side verification (§8.2 _verify_mention) ──────────────────────────────────────────────────
from fuzzy_import import ENTRIES, S8, name_forms   # noqa: E402

_CODE_INPUT = re.compile(r"\d{2,5}(\.HK)?", re.IGNORECASE)


def _subseq(a: str, b: str) -> bool:
    it = iter(b)
    return bool(a) and all(ch in it for ch in a)


def names_agree(name: str, entry: dict) -> bool:
    """§4.2: some form q of the mention's name and some form n of a listed name agree — q is an ordered
    subsequence of n, or n is one of q and covers at least 60% of it."""
    q_forms = name_forms(name)
    for cand in (entry["company_name_zh"], entry["company_name_en"]):
        for n in (name_forms(cand) if cand else []):
            if any(_subseq(q, n) or (len(n) >= 0.6 * len(q) and _subseq(n, q)) for q in q_forms):
                return True
    return False


def code_printed(code: str, text: str) -> bool:
    n = int(code)
    return bool(re.search(rf"[（(]\s*0*{n}(?:\.HK)?\s*[)）]|(?<!\d)0*{n}\.HK|(?i:stock\s*codes?)\s*[:：]?\s*0*{n}(?!\d)"
                          rf"|股份代號\s*[:：]?\s*0*{n}(?!\d)", text))


def normalize_code(raw: str | None) -> str | None:
    text = (raw or "").strip()
    return text.upper().removesuffix(".HK").zfill(5) if _CODE_INPUT.fullmatch(text) else None


def lookup_stock(name: str) -> dict | None:
    code = S8(name)
    return ENTRIES.get(code) if code else None


def verify_mention(m: EntityMention, article_text: str) -> dict | None:
    code = normalize_code(m.stock_code)
    if code in ENTRIES:
        entry = ENTRIES[code]
        if names_agree(m.company_name, entry) or code_printed(code, article_text):
            return entry
    return lookup_stock(m.company_name)


# ── Skill (§8.2 llm_process) ───────────────────────────────────────────────────────────────────────
@dataclass
class SkillLog:
    verified_entities: list[str] = field(default_factory=list)
    dropped_mentions: list[dict] = field(default_factory=list)
    input_truncated: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    business_validation: list[str] = field(default_factory=list)


async def analyse(client: genai.Client, title: str, body: str) -> tuple[list[AnalysisOutcome], SkillLog]:
    log = SkillLog()
    cut = body[:MAX_INPUT_CHARS]
    log.input_truncated = len(cut) < len(body)
    res = await generate_structured(client, system_prompt(), f"標題：{title}\n內文：{cut}", LLMOutput, GENERATION_CONFIG)
    log.input_tokens, log.output_tokens, log.latency_ms = res.input_tokens, res.output_tokens, res.latency_ms

    outcomes: list[AnalysisOutcome] = []
    for m in res.output.output_items:
        entry = verify_mention(m, title + cut)
        if entry is None:
            log.dropped_mentions.append({"stock_code": m.stock_code, "company_name": m.company_name,
                                         "reason": "not_in_hk_stock_list"})
            continue
        o = AnalysisOutcome(stock_code=entry["stock_code"], **m.model_dump(exclude={"stock_code", "company_name"}))
        if not 0.0 <= o.sentiment_score <= 1.0:
            o.sentiment_score = min(max(o.sentiment_score, 0.0), 1.0)
            log.business_validation.append(f"{o.stock_code} sentiment_score clipped")
        if len(o.headline) > 20:
            o.headline = o.headline[:20]
            log.business_validation.append(f"{o.stock_code} headline truncated")
        if len(o.entity_summary) > 100:
            o.entity_summary = o.entity_summary[:100]
            log.business_validation.append(f"{o.stock_code} entity_summary truncated")
        if o.event_type_secondary == o.event_type_primary:
            o.event_type_secondary = None
            log.business_validation.append(f"{o.stock_code} event_type_secondary cleared")
        outcomes.append(o)

    deduped, seen = [], set()
    for o in outcomes:
        if o.stock_code in seen:
            log.business_validation.append(f"{o.stock_code} duplicate dropped")
            continue
        seen.add(o.stock_code)
        deduped.append(o)
    log.verified_entities = [o.stock_code for o in deduped]
    return deduped, log


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title")
    ap.add_argument("--body")
    ap.add_argument("--cleaned-id")
    a = ap.parse_args()
    title, body = a.title, a.body
    if a.cleaned_id:
        csv.field_size_limit(10**9)
        row = next(r for r in csv.DictReader(EXPORT_CSV.open(encoding="utf-8")) if r["cleaned_id"] == a.cleaned_id)
        title, body = row["title_cleaned"], row["body_cleaned"]
    outcomes, log = asyncio.run(analyse(make_client(), title, body))
    print(json.dumps({"entities": [o.model_dump(mode="json") for o in outcomes], "skill_log": asdict(log)},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
