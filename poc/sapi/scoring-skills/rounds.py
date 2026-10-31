"""Variant rounds for the Scoring Skills against a gemini-3.1-pro-preview reference.

- Eval set: every multi-article Event plus a stratified sample of single-article ones, split dev/test by stock_code
  (an Event and its hand-built twin share constituents, so they must land on the same side).
- Reference score: Pro (thinking on) scores each Event from ALL its constituent summaries with §8.4's rubric
  (Parts 1-2, output constraints). Run twice: ref_a is the reference, ref_b measures Pro's own agreement (ceiling).
- Judge: Pro grades each variant's BriefSummary against all constituent summaries (faithfulness, coverage).
- Variants: see VARIANTS. Every response goes to data/rounds/<variant>.jsonl.
- Prompts are the ones the rounds ran: Parts 1/2/4 from implementation.md as of FROZEN_REV (before the spike's
  writeback), few-shots from the current implementation.md with the commentary tails the earlier variants used put
  back (SHOT_TAIL_CUTS). V4 is what implementation.md now specifies, except Edge Case Rule 3's wording.

    .venv/bin/python rounds.py ref dev          # reference scores
    .venv/bin/python rounds.py run V0 dev       # a variant (both Skills), then its judge
    .venv/bin/python rounds.py report dev V0 V1 ...
"""

import asyncio
import json
import subprocess
import random
import re
import statistics
import sys
import zlib
from dataclasses import dataclass, field

from google.genai import types
from pydantic import BaseModel

import reference_scoring as rs
import run_eval as ev
from reference_entity_analysis import (GenerationConfig, LLMProviderError, LLMRateLimitError, generate_structured,
                                       make_client)

OUT = rs.HERE / "data" / "rounds"
CONCURRENCY = 6
PRO = GenerationConfig(model="gemini-3.1-pro-preview", temperature=1.0, max_output_tokens=16384)


def pro_client():
    from google import genai
    return genai.Client(vertexai=True, project="wealthfinder", location="global",
                        http_options=types.HttpOptions(timeout=240_000))


# ── eval set ───────────────────────────────────────────────────────────────────────────────────────
def eval_set(split: str) -> list[dict]:
    evs = ev.events()
    multi = [e for e in evs if len(e["constituents"]) > 1]
    single = [e for e in evs if len(e["constituents"]) == 1]
    rnd = random.Random(3)
    ga = [e for e in single if e["event_type_primary"] == "GENERAL_ANNOUNCEMENT"]
    other = [e for e in single if e["event_type_primary"] != "GENERAL_ANNOUNCEMENT"]
    picked = multi + rnd.sample(other, min(len(other), 80)) + rnd.sample(ga, 50)
    return [e for e in picked if ("dev" if zlib.crc32(e["stock_code"].encode()) % 2 == 0 else "test") == split]


def all_summaries(e: dict) -> str:
    return "\n".join(f"- [{c['source_name']}] {c['entity_summary']}" for c in e["constituents"])


# ── prompt pieces ──────────────────────────────────────────────────────────────────────────────────
FROZEN_REV = "9fa749d"   # master just before the spike's doc writeback


def _frozen_parts(start: str, end: str) -> dict[str, str]:
    text = subprocess.run(["git", "show", f"{FROZEN_REV}:stock-assistant-pipeline-intelligence/docs/implementation.md"],
                          cwd=rs.HERE, capture_output=True, text=True, check=True).stdout
    sec = text[text.index(start):text.index(end)]
    return dict(re.findall(r"\*(Part \d)[^*]*\*:?\s*\n```\n(.*?)```", sec, re.S))


def scoring_parts() -> dict[str, str]:
    return _frozen_parts("### 8.4 ", "### 8.5 ")


def brief_parts() -> dict[str, str]:
    return _frozen_parts("### 8.3 ", "### 8.4 ")


def draft_shots(heading: str) -> list[str]:
    """The few-shots as the rounds' variants read them: §8.3's with the commentary tails put back."""
    shots = rs.few_shots(rs._section("### 8.3 ", "### 8.4 ") if heading == "## 8.3" else rs._section("### 8.4 ", "### 8.5 "))
    if heading == "## 8.3":
        for old, new in SHOT_TAIL_CUTS.items():
            shots = [s.replace(new, old) for s in shots]
    return shots


def select_capped(constituents: list[dict], char_cap: int | None) -> list[dict]:
    """§8.3 selection as the rounds ran it: the documented order, then the optional total-character cap."""
    picked, total = [], 0
    for c in rs.select_summaries(constituents)[0]:
        if char_cap is not None and total + len(c["entity_summary"]) > char_cap:
            continue
        picked.append(c)
        total += len(c["entity_summary"])
    return picked


def join_shots(shots: list[str]) -> str:
    return "\n\n".join(f"範例 {i}：\n{s}" for i, s in enumerate(shots, 1))


BRIEF_RULE_NUMBERS = "4. 數字照錄輸入原文：不得自行加總、平均或換算；不同幣種的金額不得相加"
SCORING_RULE_NEW_INFO = ("4. 只評估本事件帶來的新信息：公司既有狀態（如長期停牌、已進入清盤程序）本身不構成評分依據；"
                         "停牌公司的例行公告仍按公告內容評分")


# Round 2: summary_full may carry only impact/outlook the sources state; duplicate figures resolved; few-shot tails cut.
SUMMARY_FULL_SPEC_OLD = "【summary_full】≤150繁體中文字；涵蓋事件背景、核心數據、潛在影響；整合多來源主要觀點"
SUMMARY_FULL_SPEC_NEW = ("【summary_full】≤150繁體中文字；涵蓋事件背景、核心數據，以及來源所述的影響或展望；整合多來源主要觀點；"
                         "不得加入來源未有的推論或評價（如「反映…」「顯示…」）")
BRIEF_RULE_DUPLICATES = "5. 同一指標有多個不同數值（如盤中及全日股價變動）→ 採用標明「全日」或最新的數值，不得混用"
BRIEF_RULE_TIMING = ("5. 時段照錄原文：只有輸入標明「全日」「半日」「盤中」的數值才可寫上該時段；未標明時段的數值照錄原文措辭"
                     "（如「升11.09%」），不得自行加上時段；同一指標有多個數值時，優先採用標明「全日」者")
SHOT_TAIL_CUTS = {
    "管理層指中場業務需求強勁，業績及股東回報均屬正面。": "管理層指中場業務需求強勁。",
    "涉及車型數目及調查時間表均未披露，結果及潛在處罰存在不確定性。": "涉及車型數目及調查時間表均未披露。",
    "將全年銷量指引由低單位數增長下調至持平，高端化增長與前景轉弱信號並存。": "將全年銷量指引由低單位數增長下調至持平。",
}


def brief_shots(no_inference: bool) -> list[str]:
    shots = draft_shots("## 8.3")
    if no_inference:
        for old, new in SHOT_TAIL_CUTS.items():
            shots = [s.replace(old, new) for s in shots]
    return shots


def brief_prompt(numbers_rule: bool, no_inference: bool = False) -> str:
    p = brief_parts()
    part2 = p["Part 2"].strip()
    rules = p["Part 4"].strip() + ("\n" + BRIEF_RULE_NUMBERS if numbers_rule else "")
    if no_inference:
        assert SUMMARY_FULL_SPEC_OLD in part2
        part2 = part2.replace(SUMMARY_FULL_SPEC_OLD, SUMMARY_FULL_SPEC_NEW)
        rules += "\n" + (BRIEF_RULE_TIMING if no_inference == "timing" else BRIEF_RULE_DUPLICATES)
    return "\n\n".join([p["Part 1"].strip(), part2, join_shots(brief_shots(no_inference)), rules])


def scoring_prompt(new_info_rule: bool, shots: list[str] | None = None) -> str:
    p = scoring_parts()
    part2 = p["Part 2"].strip() + ("\n" + SCORING_RULE_NEW_INFO if new_info_rule else "")
    shots = draft_shots("## 8.4") if shots is None else shots
    return "\n\n".join(x for x in [p["Part 1"].strip(), part2, join_shots(shots), p["Part 4"].strip()] if x)


ANALYST_SHOT = """INPUT:
【實體信息】
股票代碼：00669
公司名稱：創科實業
主要事件類型：ANALYST_RATING
次要事件類型：無

【事件摘要（來自BriefSummarySkill）】
summary_full：摩根士丹利將創科實業評級由「中性」上調至「增持」，目標價由95港元升至118港元，指北美工具需求回暖及新產品周期將帶動盈利增長。
key_numbers：目標價95港元→118港元

OUTPUT:
stock_impact_score: 3
adjustment_reason: "大行升評並上調目標價約24%，具明確前瞻正面信號，符合分析師升評的明確正面水平。"
"""


def scoring_shots(kind: str) -> list[str]:
    return {"draft": draft_shots("## 8.4"), "none": [], "plus_analyst": draft_shots("## 8.4") + [ANALYST_SHOT.strip()]}[kind]


def shots_as_summaries(shots: list[str]) -> list[str]:
    """§8.4's examples with the Brief block replaced by the article-summary block a summaries-input variant reads."""
    out = []
    for s in shots:
        m = re.search(r"【事件摘要（來自BriefSummarySkill）】\nsummary_full：(.*?)\nkey_numbers：.*?\n", s, re.S)
        out.append(s.replace(m.group(0), f"【各文章實體摘要】\n- [HKEX] {m.group(1)}\n"))
    return out


# ── combined single call ───────────────────────────────────────────────────────────────────────────
class BriefAndScore(BaseModel):
    summary_short: str
    summary_full: str
    key_numbers: list[str]
    stock_impact_score: float
    adjustment_reason: str


COMBINED_SCORES = [   # stock_impact_score / adjustment_reason added to §8.3's three examples
    ('4', "EBITDA增長18%並勝市場預期，同時宣派股息及20億港元回購，財務數據與股東回報方向一致，屬明確正面，接近重大正面水平。"),
    ('-3', "正式監管調查屬明確負面事件，涉及排放數據申報的合規風險，調查範圍及處罰未明，但未見存亡風險，評為明確負面。"),
    ('-1', "收入及高端化增長屬正面，但毛利率收窄且下調全年銷量指引，前瞻信號主導，負面略為抵銷增長，整體輕微負面。"),
]


def combined_prompt(numbers_rule: bool, new_info_rule: bool) -> str:
    b, s = brief_parts(), scoring_parts()
    shots = [shot + f'\nstock_impact_score: {sc}\nadjustment_reason: "{reason}"'
             for shot, (sc, reason) in zip(draft_shots("## 8.3"), COMBINED_SCORES)]
    part2 = s["Part 2"].strip() + ("\n" + SCORING_RULE_NEW_INFO if new_info_rule else "")
    rules = b["Part 4"].strip() + ("\n" + BRIEF_RULE_NUMBERS if numbers_rule else "")
    role = ("你是香港股票市場新聞分析專家。你的任務是為已聚合的股票事件生成結構化摘要，供專業投資者在晨報中快速閱讀，"
            "並評估該事件對該上市公司股價的潛在影響方向與幅度。\n\n"
            "所有輸出必須使用繁體中文，無論來源文章語言為何。\n摘要必須基於輸入內容，不得添加輸入中未提及的信息或數據。\n"
            "影響評估必須直接基於事件內容對股價的實際影響進行分析。")
    return "\n\n".join([role, b["Part 2"].strip(), part2, s["Part 4"].strip(), join_shots(shots), rules])


# ── variants ───────────────────────────────────────────────────────────────────────────────────────
@dataclass
class Variant:
    char_cap: int | None = 200
    brief_temp: float = 0.1
    numbers_rule: bool = False
    new_info_rule: bool = False
    scoring_input: str = "brief"          # "brief" (documented) | "summaries" | "both"
    combined: bool = False
    no_inference: bool | str = False      # True, or "timing" for V9's timing rule in place of the duplicates rule
    shots: str = "draft"                  # "draft" | "none" | "plus_analyst"
    scoring_model: str = "gemini-3.1-flash-lite"
    brief_model: str = "gemini-3.1-flash-lite"
    extra: dict = field(default_factory=dict)


VARIANTS: dict[str, Variant] = {
    "V0": Variant(),                                                                   # as documented
    "V1": Variant(char_cap=None, brief_temp=0.0, numbers_rule=True, new_info_rule=True),  # proposed fixes
    "V2": Variant(char_cap=None, brief_temp=0.0, numbers_rule=True, new_info_rule=True, scoring_input="summaries"),
    "V3": Variant(char_cap=None, brief_temp=0.0, numbers_rule=True, new_info_rule=True, combined=True),
    "V4": Variant(char_cap=None, brief_temp=0.0, numbers_rule=True, new_info_rule=True, no_inference=True),
    "V5": Variant(char_cap=None, brief_temp=0.0, numbers_rule=True, new_info_rule=True, no_inference=True, shots="none"),
    "V6": Variant(char_cap=None, brief_temp=0.0, numbers_rule=True, new_info_rule=True, no_inference=True,
                  shots="plus_analyst"),
    "V7": Variant(char_cap=None, brief_temp=0.0, numbers_rule=True, new_info_rule=True, no_inference=True,
                  scoring_model="gemini-3.8-flash"),
    "V9": Variant(char_cap=None, brief_temp=0.0, numbers_rule=True, new_info_rule=True, no_inference="timing"),
    "V4b": Variant(char_cap=None, brief_temp=0.0, numbers_rule=True, new_info_rule=True, no_inference=True),
    "V8": Variant(char_cap=None, brief_temp=0.0, numbers_rule=True, new_info_rule=False, no_inference=True),
}


async def _retry(fn):
    for attempt in range(5):
        try:
            return await fn()
        except (LLMRateLimitError, LLMProviderError) as e:
            print(f"  attempt {attempt + 1}: {type(e).__name__} {str(e)[:80]}")
            await asyncio.sleep(8 * (attempt + 1))
    return None


async def run_variant(v: Variant, client, e: dict) -> dict | None:
    picked = select_capped(e["constituents"], v.char_cap)
    brief_user = rs.brief_user_message(e, picked)
    if v.combined:
        cfg = GenerationConfig(model=v.brief_model, temperature=v.brief_temp, max_output_tokens=4096)
        r = await _retry(lambda: generate_structured(client, combined_prompt(v.numbers_rule, v.new_info_rule),
                                                     brief_user, BriefAndScore, cfg))
        if r is None:
            return None
        o = r.output
        return {"brief": {"summary_short": o.summary_short, "summary_full": o.summary_full, "key_numbers": o.key_numbers},
                "score": {"stock_impact_score": o.stock_impact_score, "adjustment_reason": o.adjustment_reason},
                "brief_user": brief_user, "tokens_in": r.input_tokens, "tokens_out": r.output_tokens,
                "latency_ms": r.latency_ms, "calls": 1}
    bcfg = GenerationConfig(model=v.brief_model, temperature=v.brief_temp, max_output_tokens=4096)
    rb = await _retry(lambda: generate_structured(client, brief_prompt(v.numbers_rule, v.no_inference), brief_user, rs.BriefSummary, bcfg))
    if rb is None:
        return None
    brief = rb.output
    shots = scoring_shots(v.shots)
    if v.scoring_input == "brief":
        score_user = rs.scoring_user_message(e, brief)
    elif v.scoring_input == "summaries":
        score_user = f"{rs.entity_block(e)}\n\n【各文章實體摘要】\n" + "\n".join(
            f"- [{c['source_name']}] {c['entity_summary']}" for c in picked)
        shots = shots_as_summaries(shots)
    scfg = GenerationConfig(model=v.scoring_model, temperature=0, max_output_tokens=2048)
    rsc = await _retry(lambda: generate_structured(client, scoring_prompt(v.new_info_rule, shots), score_user,
                                                   rs.LLMScore, scfg))
    if rsc is None:
        return None
    return {"brief": brief.model_dump(), "score": rsc.output.model_dump(), "brief_user": brief_user,
            "score_user": score_user, "tokens_in": rb.input_tokens + rsc.input_tokens,
            "tokens_out": rb.output_tokens + rsc.output_tokens, "latency_ms": rb.latency_ms + rsc.latency_ms, "calls": 2}


# ── reference and judge ────────────────────────────────────────────────────────────────────────────
def reference_prompt() -> str:
    p = scoring_parts()
    return "\n\n".join([p["Part 1"].strip(), p["Part 2"].strip(), p["Part 4"].strip()])


class Judgement(BaseModel):
    unsupported: list[str]        # claims or numbers in the summary the input does not support (incl. wrong arithmetic)
    missed_facts: list[str]       # price-relevant facts in the input that summary_full leaves out
    short_has_core: bool          # summary_short states the core event (and its most important number, if any)
    key_numbers_ok: bool          # every key_number is in the input, relevant to the event, correctly stated
    quality: int                  # 1-5 overall usefulness to a professional investor


JUDGE_PROMPT = """You grade a Traditional-Chinese Morning Brief summary of one Hong Kong stock event for professional investors.
INPUT is every source-article summary the event aggregates. OUTPUT is the generated summary (summary_short <=30 chars,
summary_full <=150 chars, key_numbers <=3, each tied to the event type).

Return:
- unsupported: each claim or number in OUTPUT that INPUT does not support. Count wrong arithmetic (e.g. adding amounts in
  different currencies), changed figures and invented context. Rounding or an equivalent restatement (1.3倍 = +130%) is fine.
- missed_facts: facts in INPUT a professional investor would need for the price impact that summary_full leaves out.
  Ignore routine filler. Remember summary_full is limited to 150 characters: list only omissions a better summary of that
  length would have covered.
- short_has_core: summary_short names the core event and, if INPUT has a key figure, the most important one.
- key_numbers_ok: every key_number is supported, relevant to the event and correctly labelled; [] is correct only if
  INPUT has no figure relevant to the event.
- quality: 1-5, overall usefulness to a professional investor (5 = nothing to improve)."""


async def reference(split: str, name: str) -> None:
    evs = eval_set(split)
    out = OUT / f"{name}.jsonl"
    done = {json.loads(x)["event_id"] for x in out.open()} if out.exists() else set()
    client, sem = pro_client(), asyncio.Semaphore(CONCURRENCY)

    async def one(e):
        async with sem:
            user = f"{rs.entity_block(e)}\n\n【各文章實體摘要】\n{all_summaries(e)}"
            r = await _retry(lambda: generate_structured(client, reference_prompt(), user, rs.LLMScore, PRO))
            if r:
                with out.open("a") as f:
                    f.write(json.dumps({"event_id": e["event_id"], **r.output.model_dump()}, ensure_ascii=False) + "\n")

    await asyncio.gather(*(one(e) for e in evs if e["event_id"] not in done))


async def run(name: str, split: str) -> None:
    v = VARIANTS[name]
    evs = eval_set(split)
    out = OUT / f"{name}.jsonl"
    done = {json.loads(x)["event_id"] for x in out.open()} if out.exists() else set()
    client, pro, sem = make_client(), pro_client(), asyncio.Semaphore(CONCURRENCY)

    async def one(e):
        async with sem:
            r = await run_variant(v, client, e)
            if r is None:
                return
            b = r["brief"]
            user = (f"INPUT:\n{rs.entity_block(e)}\n\n{all_summaries(e)}\n\nOUTPUT:\nsummary_short: {b['summary_short']}\n"
                    f"summary_full: {b['summary_full']}\nkey_numbers: {json.dumps(b['key_numbers'], ensure_ascii=False)}")
            j = await _retry(lambda: generate_structured(pro, JUDGE_PROMPT, user, Judgement, PRO))
            r["judge"] = j.output.model_dump() if j else None
            with out.open("a") as f:
                f.write(json.dumps({"event_id": e["event_id"], **r}, ensure_ascii=False) + "\n")

    await asyncio.gather(*(one(e) for e in evs if e["event_id"] not in done))


async def rejudge(name: str, split: str) -> None:
    """Judges an existing variant's Briefs again into <name>_j2, to measure the judge's own noise."""
    rows = load(name)
    evs = {e["event_id"]: e for e in eval_set(split)}
    out = OUT / f"{name}_j2.jsonl"
    done = set(load(f"{name}_j2"))
    pro, sem = pro_client(), asyncio.Semaphore(CONCURRENCY)

    async def one(k, r):
        async with sem:
            e, b = evs[k], r["brief"]
            user = (f"INPUT:\n{rs.entity_block(e)}\n\n{all_summaries(e)}\n\nOUTPUT:\nsummary_short: {b['summary_short']}\n"
                    f"summary_full: {b['summary_full']}\nkey_numbers: {json.dumps(b['key_numbers'], ensure_ascii=False)}")
            j = await _retry(lambda: generate_structured(pro, JUDGE_PROMPT, user, Judgement, PRO))
            if j:
                with out.open("a") as f:
                    f.write(json.dumps({**r, "judge": j.output.model_dump()}, ensure_ascii=False) + "\n")

    await asyncio.gather(*(one(k, r) for k, r in rows.items() if k in evs and k not in done))


# ── report ─────────────────────────────────────────────────────────────────────────────────────────
def load(name: str) -> dict[str, dict]:
    p = OUT / f"{name}.jsonl"
    return {json.loads(x)["event_id"]: json.loads(x) for x in p.open()} if p.exists() else {}


def sign(x: float) -> int:
    return (x > 0) - (x < 0)


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float:
    return statistics.correlation(_ranks(a), _ranks(b))


def score_metrics(pred: dict[str, float], ref: dict[str, float]) -> str:
    ids = [i for i in pred if i in ref]
    mae = statistics.mean(abs(pred[i] - ref[i]) for i in ids)
    sign_agree = statistics.mean(sign(pred[i]) == sign(ref[i]) for i in ids)
    within1 = statistics.mean(abs(pred[i] - ref[i]) <= 1 for i in ids)
    opposite = sum(sign(pred[i]) * sign(ref[i]) < 0 for i in ids)
    rho = spearman([pred[i] for i in ids], [ref[i] for i in ids])
    return f"n={len(ids)} spearman {rho:.3f}  MAE {mae:.2f}  sign-agree {sign_agree:.3f}  within-1 {within1:.3f}  opposite-sign {opposite}"


def bootstrap(split: str, a: str, b: str, n: int = 2000) -> None:
    """Paired bootstrap of Spearman/sign-agreement differences b - a against ref_a: 95% interval."""
    ids = {e["event_id"] for e in eval_set(split)}
    ref = {k: v["stock_impact_score"] for k, v in load("ref_a").items() if k in ids}
    pa = {k: v["score"]["stock_impact_score"] for k, v in load(a).items()}
    pb = {k: v["score"]["stock_impact_score"] for k, v in load(b).items()}
    common = [k for k in ref if k in pa and k in pb]
    rnd = random.Random(0)
    d_rho, d_sign = [], []
    for _ in range(n):
        s = [rnd.choice(common) for _ in common]
        r = [ref[k] for k in s]
        d_rho.append(spearman([pb[k] for k in s], r) - spearman([pa[k] for k in s], r))
        d_sign.append(statistics.mean(sign(pb[k]) == sign(ref[k]) for k in s)
                      - statistics.mean(sign(pa[k]) == sign(ref[k]) for k in s))
    q = lambda xs: (sorted(xs)[int(.025 * n)], sorted(xs)[int(.975 * n)])
    print(f"  {b} - {a} (n={len(common)}): spearman {statistics.mean(d_rho):+.3f} {q(d_rho)[0]:+.3f}..{q(d_rho)[1]:+.3f}"
          f"   sign-agree {statistics.mean(d_sign):+.3f} {q(d_sign)[0]:+.3f}..{q(d_sign)[1]:+.3f}")


def report(split: str, names: list[str]) -> None:
    ids = {e["event_id"] for e in eval_set(split)}
    multi = {e["event_id"] for e in eval_set(split) if len(e["constituents"]) > 1}
    ref_a = {k: v["stock_impact_score"] for k, v in load("ref_a").items() if k in ids}
    ref_b = {k: v["stock_impact_score"] for k, v in load("ref_b").items() if k in ids}
    print(f"[{split}] {len(ids)} Events ({len(multi)} multi-article)")
    if ref_b:
        print(f"  Pro vs Pro (ceiling)  {score_metrics(ref_b, ref_a)}")
    for name in names:
        rows = {k: v for k, v in load(name).items() if k in ids}
        if not rows:
            continue
        pred = {k: v["score"]["stock_impact_score"] for k, v in rows.items()}
        js = [v["judge"] for v in rows.values() if v.get("judge")]
        jm = [v["judge"] for k, v in rows.items() if v.get("judge") and k in multi]
        print(f"  {name:4s} score: {score_metrics(pred, ref_a)}")
        print(f"       brief: unsupported-any {statistics.mean(bool(j['unsupported']) for j in js):.3f}  "
              f"missed/Event {statistics.mean(len(j['missed_facts']) for j in js):.2f} (multi {statistics.mean(len(j['missed_facts']) for j in jm):.2f})  "
              f"short-core {statistics.mean(j['short_has_core'] for j in js):.3f}  "
              f"key_numbers-ok {statistics.mean(j['key_numbers_ok'] for j in js):.3f}  "
              f"quality {statistics.mean(j['quality'] for j in js):.2f} (multi {statistics.mean(j['quality'] for j in jm):.2f})")
        print(f"       cost: tokens in {statistics.mean(v['tokens_in'] for v in rows.values()):.0f} / out "
              f"{statistics.mean(v['tokens_out'] for v in rows.values()):.0f} per Event, "
              f"latency p95 {ev.pct([v['latency_ms'] for v in rows.values()], .95)} ms, calls {rows[next(iter(rows))]['calls']}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cmd = sys.argv[1]
    if cmd == "ref":
        asyncio.run(reference(sys.argv[2], "ref_a"))
        asyncio.run(reference(sys.argv[2], "ref_b"))
    elif cmd == "run":
        asyncio.run(run(sys.argv[2], sys.argv[3]))
    elif cmd == "rejudge":
        asyncio.run(rejudge(sys.argv[2], sys.argv[3]))
    elif cmd == "boot":
        for b in sys.argv[4:]:
            bootstrap(sys.argv[2], sys.argv[3], b)
    elif cmd == "report":
        report(sys.argv[2], sys.argv[3:])


if __name__ == "__main__":
    main()
