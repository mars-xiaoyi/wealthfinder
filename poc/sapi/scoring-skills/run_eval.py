"""Full-set measurements of BriefSummarySkill / EventLLMScoringSkill as implementation.md now specifies them, on
every Event from build_events.py. Every raw response goes to data/runs/<name>.jsonl (gitignored).

    .venv/bin/python run_eval.py prod        # every Event through both Skills
    .venv/bin/python run_eval.py repeat      # 5 runs per sampled Event, both Skills (run-to-run stability)
    .venv/bin/python run_eval.py report      # summary of whatever runs exist
"""

import asyncio
import json
import random
import re
import statistics
import sys
from collections import Counter, defaultdict

from opencc import OpenCC

import reference_scoring as rs
from reference_entity_analysis import LLMProviderError, LLMRateLimitError, make_client

RUNS = rs.HERE / "data" / "runs"
CONCURRENCY = 4
REPEATS = 5
REPEAT_SAMPLE = 20
_S2T = OpenCC("s2t")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def events() -> list[dict]:
    return json.loads(rs.EVENTS.read_text())


async def _retry(coro_fn):
    for attempt in range(4):
        try:
            return await coro_fn()
        except (LLMRateLimitError, LLMProviderError) as e:
            print(f"  attempt {attempt + 1}: {type(e).__name__}")
            await asyncio.sleep(5 * (attempt + 1))
    return None


async def run_pipeline(name: str, evs: list[dict], repeats: int = 1) -> None:
    out = RUNS / f"{name}.jsonl"
    done = Counter(json.loads(line)["event_id"] for line in out.open()) if out.exists() else Counter()
    client, sem = make_client(), asyncio.Semaphore(CONCURRENCY)

    async def one(ev: dict) -> None:
        async with sem:
            b = await _retry(lambda: rs.brief_summary(client, ev))
            if b is None:
                return
            s = await _retry(lambda: rs.event_scoring(client, ev, b.output))
            if s is None:
                return
            with out.open("a") as f:
                f.write(json.dumps({"event_id": ev["event_id"], "brief": _dump(b), "score": _dump(s)},
                                   ensure_ascii=False) + "\n")

    jobs = [ev for ev in evs for _ in range(max(0, repeats - done[ev["event_id"]]))]
    print(f"{name}: {len(jobs)} runs to go")
    await asyncio.gather(*(one(ev) for ev in jobs))


def _dump(r: rs.SkillRun) -> dict:
    return {"output": r.output.model_dump(), "fallback_reason": r.fallback_reason, "user_message": r.user_message,
            "input_tokens": r.input_tokens, "output_tokens": r.output_tokens, "latency_ms": r.latency_ms,
            "business_validation": r.business_validation,
            "input_summary": r.input_summary.__dict__ if r.input_summary else None}


def repeat_sample(evs: list[dict]) -> list[dict]:
    multi = [e for e in evs if len(e["constituents"]) > 1]
    single = [e for e in evs if len(e["constituents"]) == 1]
    rnd = random.Random(7)
    return rnd.sample(multi, min(len(multi), REPEAT_SAMPLE // 2)) + rnd.sample(single, REPEAT_SAMPLE // 2)


# ── report ─────────────────────────────────────────────────────────────────────────────────────────
def load(name: str) -> list[dict]:
    p = RUNS / f"{name}.jsonl"
    return [json.loads(line) for line in p.open()] if p.exists() else []


def nums(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(text)}


def brief_text(out: dict) -> str:
    return " ".join([out["summary_short"], out["summary_full"], *out["key_numbers"]])


def summaries_part(user_message: str) -> str:
    return user_message.split("【各文章實體摘要】")[1]


def simplified_chars(text: str, source: str) -> str:
    """Characters OpenCC would convert to Traditional that the (Traditional) input doesn't itself use — OpenCC
    also converts HK forms like 恒/涌/伙, which the input already uses and are not errors."""
    return "".join(a for a, b in zip(text, _S2T.convert(text)) if a != b and a not in source)


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def report() -> None:
    evs = {e["event_id"]: e for e in events()}
    prod = load("prod")
    print(f"=== prod: {len(prod)} Events ===")
    print("constituents per Event:", dict(sorted(Counter(len(evs[r["event_id"]]["constituents"]) for r in prod).items())))
    isum = [r["brief"]["input_summary"] for r in prod]
    print(f"source cap cut: {sum(i['truncated_by_source_cap'] for i in isum)}; "
          f"input chars max {max(i['entity_summary_total_chars'] for i in isum)}")

    for skill, limit in (("brief", rs.BRIEF_CONFIG.max_output_tokens), ("score", rs.SCORING_CONFIG.max_output_tokens)):
        rows = [r[skill] for r in prod]
        ot, lat, it = ([x[k] for x in rows] for k in ("output_tokens", "latency_ms", "input_tokens"))
        print(f"{skill}: output_tokens p50 {pct(ot, .5)} p95 {pct(ot, .95)} max {max(ot)} (limit {limit}); "
              f"input_tokens max {max(it)}; latency p50 {pct(lat, .5)} p95 {pct(lat, .95)} max {max(lat)} ms; "
              f"fallbacks {dict(Counter(x['fallback_reason'] for x in rows if x['fallback_reason']))}; "
              f"business validation {dict(Counter(n for x in rows for n in x['business_validation']))}")

    b = [r["brief"] for r in prod]
    print("summary_short len p50/max", pct([len(x["output"]["summary_short"]) for x in b], .5),
          max(len(x["output"]["summary_short"]) for x in b),
          "| summary_full len p50/max", pct([len(x["output"]["summary_full"]) for x in b], .5),
          max(len(x["output"]["summary_full"]) for x in b),
          "| key_numbers count", dict(sorted(Counter(len(x["output"]["key_numbers"]) for x in b).items())))
    no_num = [x for x in b if not nums(summaries_part(x["user_message"]))]
    print(f"inputs without any number: {len(no_num)}, of which key_numbers non-empty: "
          f"{sum(bool(x['output']['key_numbers']) for x in no_num)}")
    inv = [(r["event_id"], n) for r in prod
           for n in [nums(brief_text(r["brief"]["output"])) - nums(summaries_part(r["brief"]["user_message"]))] if n]
    print(f"brief outputs with a number not literally in the input (hand-check): {len(inv)}", inv[:20])
    simp = [(r["event_id"], s) for r in prod
            for s in [simplified_chars(brief_text(r["brief"]["output"]) + r["score"]["output"]["adjustment_reason"],
                                       r["brief"]["user_message"])] if s]
    print(f"outputs with simplified-only chars (hand-check): {len(simp)}", simp[:10])

    scores = [r["score"]["output"]["stock_impact_score"] for r in prod]
    print("stock_impact_score distribution:", dict(sorted(Counter(scores).items())))
    by_type = defaultdict(list)
    for r in prod:
        by_type[evs[r["event_id"]]["event_type_primary"]].append(r["score"]["output"]["stock_impact_score"])
    for t, v in sorted(by_type.items()):
        print(f"  {t:22s} n={len(v):3d} mean {statistics.mean(v):+.2f} min {min(v):+} max {max(v):+}")

    rows = load("repeat")
    if rows:
        g = defaultdict(list)
        for r in rows:
            g[r["event_id"]].append(r)
        print(f"\n=== repeat: {len(g)} Events x {REPEATS} ===")
        for field_, get in (("summary_short", lambda r: r["brief"]["output"]["summary_short"]),
                            ("summary_full", lambda r: r["brief"]["output"]["summary_full"]),
                            ("key_numbers", lambda r: tuple(r["brief"]["output"]["key_numbers"])),
                            ("stock_impact_score", lambda r: r["score"]["output"]["stock_impact_score"])):
            distinct = [len({get(r) for r in rs_}) for rs_ in g.values()]
            print(f"  {field_:18s} Events with >1 distinct value: {sum(d > 1 for d in distinct)}")
        spread = [max(v) - min(v) for v in ([r["score"]["output"]["stock_impact_score"] for r in rs_] for rs_ in g.values())]
        print(f"  stock_impact_score max-min per Event: {dict(sorted(Counter(spread).items()))}")


def main() -> None:
    cmd = sys.argv[1]
    RUNS.mkdir(parents=True, exist_ok=True)
    if cmd == "prod":
        asyncio.run(run_pipeline("prod", events()))
    elif cmd == "repeat":
        asyncio.run(run_pipeline("repeat", repeat_sample(events()), repeats=REPEATS))
    report()


if __name__ == "__main__":
    main()
