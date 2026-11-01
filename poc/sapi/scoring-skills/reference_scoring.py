"""Reference implementation of BriefSummarySkill (implementation.md §8.3) and EventLLMScoringSkill (§8.4), for the
two Scoring Skill tickets to read and run against live Vertex AI. Throwaway code: one file, no app/ imports.

generate_structured() (§7.3) is the Entity Analysis spike's (poc/sapi/llm-skill/reference_entity_analysis.py).
Both system prompts are read from the current implementation.md, so this always runs the documented prompts.

    .venv/bin/python reference_scoring.py --event-id <id>        # an Event from data/events.json (build_events.py)
"""

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "llm-skill"))
from reference_entity_analysis import (IMPL_MD, GenerationConfig, LLMSchemaViolationError,   # noqa: E402
                                       generate_structured, make_client)

EVENTS = HERE / "data" / "events.json"

# admin-tad.md §4.2 seed data
SOURCE_WEIGHTS = {"HKEX": 9.0, "MINGPAO": 6.0, "AASTOCKS": 6.0, "YAHOO_HK": 6.0}

BRIEF_CONFIG = GenerationConfig(model="gemini-3.1-flash-lite", temperature=0, max_output_tokens=1024)
SCORING_CONFIG = GenerationConfig(model="gemini-3.1-flash-lite", temperature=0, max_output_tokens=512)

PER_SOURCE_CAP = 3


# ── Prompts, read from implementation.md ───────────────────────────────────────────────────────────
def _section(start: str, end: str) -> str:
    text = IMPL_MD.read_text()
    sec = text[text.index(start):text.index(end)]
    return sec[sec.index("#### System Prompt"):sec.index("#### Output Schemas")]


_SHOT = re.compile(r"\*\*Few-shot Example \d[^*]*\*\*\s*\n```\n(.*?)```", re.S)


def few_shots(sec: str) -> list[str]:
    return [s.strip() for s in _SHOT.findall(sec)]


def _system_prompt(sec: str) -> str:
    """Parts 1, 2, 3 (few-shots, each as `範例 N：` + its block) and 4, blank line between each (§8.2)."""
    fences = dict(re.findall(r"\*(Part \d)[^*]*\*:?\s*\n```\n(.*?)```", sec, re.S))
    shots = few_shots(sec)
    few_shot = "\n\n".join(f"範例 {i}：\n{s}" for i, s in enumerate(shots, 1))
    return "\n\n".join([fences["Part 1"].strip(), fences["Part 2"].strip(), few_shot, fences["Part 4"].strip()])


def brief_system_prompt() -> str:
    return _system_prompt(_section("### 8.3 ", "### 8.4 "))


def scoring_system_prompt() -> str:
    return _system_prompt(_section("### 8.4 ", "### 8.5 "))


def entity_block(ev: dict) -> str:
    secondary = "、".join(ev["event_type_secondary"] or []) or "無"
    return (f"【實體信息】\n股票代碼：{ev['stock_code']}\n公司名稱：{ev['company_name']}\n"
            f"主要事件類型：{ev['event_type_primary']}\n次要事件類型：{secondary}")


# ── BriefSummarySkill (§8.3) ───────────────────────────────────────────────────────────────────────
class BriefSummary(BaseModel):
    summary_short: str
    summary_full: str
    key_numbers: list[str]


@dataclass
class InputSummary:
    entity_summary_count: int
    entity_summary_total_chars: int
    truncated_by_source_cap: bool


def select_summaries(constituents: list[dict]) -> tuple[list[dict], InputSummary]:
    """§8.3 Input construction: per-source cap, then authority order (latest first within a weight)."""
    by_source: dict[str, list[dict]] = {}
    for c in constituents:
        by_source.setdefault(c["source_name"], []).append(c)
    retained, source_cut = [], False
    for items in by_source.values():
        items = sorted(items, key=lambda c: c["published_at"], reverse=True)
        source_cut |= len(items) > PER_SOURCE_CAP
        retained += items[:PER_SOURCE_CAP]
    retained.sort(key=lambda c: c["published_at"], reverse=True)
    retained.sort(key=lambda c: SOURCE_WEIGHTS.get(c["source_name"], 0.0), reverse=True)
    return retained, InputSummary(len(retained), sum(len(c["entity_summary"]) for c in retained), source_cut)


def brief_user_message(ev: dict, picked: list[dict]) -> str:
    lines = "\n".join(f"- [{c['source_name']}] {c['entity_summary']}" for c in picked)
    return f"{entity_block(ev)}\n\n【各文章實體摘要】\n{lines}"


@dataclass
class SkillRun:
    output: BaseModel
    fallback: bool
    fallback_reason: str | None
    user_message: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    business_validation: list[str] = field(default_factory=list)
    input_summary: InputSummary | None = None


def brief_fallback(ev: dict) -> BriefSummary:
    best = max(ev["constituents"], key=lambda c: (SOURCE_WEIGHTS.get(c["source_name"], 0.0), c["published_at"]))
    return BriefSummary(summary_short=best["headline"], summary_full=best["headline"], key_numbers=[])


async def brief_summary(client, ev: dict, config: GenerationConfig = BRIEF_CONFIG) -> SkillRun:
    picked, input_summary = select_summaries(ev["constituents"])
    user = brief_user_message(ev, picked)
    try:
        res = await generate_structured(client, brief_system_prompt(), user, BriefSummary, config)
    except LLMSchemaViolationError:
        return SkillRun(brief_fallback(ev), True, "schema_violation", user, input_summary=input_summary)
    out, notes = res.output, []
    if len(out.summary_short) > 30:
        out.summary_short, _ = out.summary_short[:30], notes.append("summary_short truncated")
    if len(out.summary_full) > 150:
        out.summary_full, _ = out.summary_full[:150], notes.append("summary_full truncated")
    if len(out.key_numbers) > 3:
        out.key_numbers, _ = out.key_numbers[:3], notes.append("key_numbers trimmed")
    return SkillRun(out, False, None, user, res.input_tokens, res.output_tokens, res.latency_ms, notes, input_summary)


# ── EventLLMScoringSkill (§8.4) ────────────────────────────────────────────────────────────────────
class LLMScore(BaseModel):
    stock_impact_score: float
    adjustment_reason: str


def scoring_user_message(ev: dict, brief: BriefSummary) -> str:
    numbers = "、".join(brief.key_numbers) or "無"   # §8.4 Input construction
    return (f"{entity_block(ev)}\n\n【事件摘要（來自BriefSummarySkill）】\n"
            f"summary_full：{brief.summary_full}\nkey_numbers：{numbers}")


async def event_scoring(client, ev: dict, brief: BriefSummary,
                        config: GenerationConfig = SCORING_CONFIG) -> SkillRun:
    user = scoring_user_message(ev, brief)
    try:
        res = await generate_structured(client, scoring_system_prompt(), user, LLMScore, config)
    except LLMSchemaViolationError:
        return SkillRun(LLMScore(stock_impact_score=0, adjustment_reason=""), True, "schema_violation", user)
    out, notes = res.output, []
    if not -5.0 <= out.stock_impact_score <= 5.0:
        out.stock_impact_score, _ = min(max(out.stock_impact_score, -5.0), 5.0), notes.append("score clipped")
    if len(out.adjustment_reason) > 80:
        out.adjustment_reason, _ = out.adjustment_reason[:80], notes.append("adjustment_reason truncated")
    if not out.adjustment_reason:
        return SkillRun(LLMScore(stock_impact_score=0, adjustment_reason=""), True, "empty_adjustment_reason", user,
                        res.input_tokens, res.output_tokens, res.latency_ms, notes)
    return SkillRun(out, False, None, user, res.input_tokens, res.output_tokens, res.latency_ms, notes)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event-id", required=True)
    a = ap.parse_args()
    ev = next(e for e in json.loads(EVENTS.read_text()) if e["event_id"] == a.event_id)

    async def run() -> None:
        client = make_client()
        brief = await brief_summary(client, ev)
        score = await event_scoring(client, ev, brief.output)
        for name, r in (("BriefSummarySkill", brief), ("EventLLMScoringSkill", score)):
            print(f"── {name} input\n{r.user_message}\n── output (fallback={r.fallback_reason})")
            print(json.dumps(r.output.model_dump(), ensure_ascii=False, indent=1))
            print(f"tokens in/out {r.input_tokens}/{r.output_tokens}, {r.latency_ms} ms, {r.business_validation}\n")

    asyncio.run(run())


if __name__ == "__main__":
    main()
