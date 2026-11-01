"""Builds realistic aggregated Events for the Scoring Skills spike.

1. Runs the chosen EntityAnalysisSkill design (poc/sapi/llm-skill/reference_entity_analysis.py, live Vertex AI) on
   every cleaned article that a spike has already read — never the `reserved` split — and caches each article's
   verified entities in data/entity_events.jsonl.
2. Aggregates them the way implementation.md §12.4's merge_or_create() does: group key (exchange, stock_code,
   event_type_primary), SLIDING_WINDOW_HOURS=4, EVENT_MAX_TIMESPAN_HOURS=24 (PRD §12.1 defaults), articles in
   published_at order. Writes data/events.json.
3. Adds hand-built Events, since only a handful of windowed ones span several sources: every (stock_code,
   event_type_primary) group that at least two sources reported over the whole export, with no time window
   (event_id prefix "hand-", hand_built=True). These are the multi-source / Q-8 cap inputs.

    .venv/bin/python build_events.py            # resumes from the cache
"""

import asyncio
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "llm-skill"))
import reference_entity_analysis as ea   # noqa: E402

SPLIT = HERE.parent / "llm-skill" / "cleaned_news_split.json"
DATA = HERE / "data"
CACHE = DATA / "entity_events.jsonl"
EVENTS = DATA / "events.json"
CONCURRENCY = 4
SLIDING_WINDOW = timedelta(hours=4)
EVENT_MAX_TIMESPAN = timedelta(hours=24)


def load_articles() -> dict[str, dict]:
    split = json.loads(SPLIT.read_text())
    ids = set(split["verify"]) | set(split["read_by_other_spike"]) | set(split["spent"])
    assert not ids & set(split["reserved"])
    csv.field_size_limit(10**9)
    return {r["cleaned_id"]: r for r in csv.DictReader(ea.EXPORT_CSV.open(encoding="utf-8")) if r["cleaned_id"] in ids}


async def analyse_all(articles: dict[str, dict]) -> None:
    done = {json.loads(line)["cleaned_id"] for line in CACHE.open()} if CACHE.exists() else set()
    todo = [a for cid, a in articles.items() if cid not in done]
    print(f"{len(done)} cached, {len(todo)} to analyse")
    client = ea.make_client()
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(a: dict) -> None:
        async with sem:
            for attempt in range(4):
                try:
                    outcomes, log = await ea.analyse(client, a["title_cleaned"], a["body_cleaned"])
                    break
                except (ea.LLMRateLimitError, ea.LLMProviderError) as e:
                    print(f"  {a['cleaned_id']} attempt {attempt + 1}: {type(e).__name__}")
                    await asyncio.sleep(5 * (attempt + 1))
            else:
                return
            with CACHE.open("a") as f:
                f.write(json.dumps({"cleaned_id": a["cleaned_id"],
                                    "entities": [o.model_dump(mode="json") for o in outcomes],
                                    "output_tokens": log.output_tokens}, ensure_ascii=False) + "\n")

    await asyncio.gather(*(one(a) for a in todo))


def aggregate(articles: dict[str, dict]) -> list[dict]:
    rows = []
    for line in CACHE.open():
        rec = json.loads(line)
        a = articles[rec["cleaned_id"]]
        for e in rec["entities"]:
            rows.append({**e, "cleaned_id": rec["cleaned_id"], "source_name": a["source_name"],
                         "title": a["title_cleaned"], "published_at": a["published_at"],
                         "company_name": (ea.ENTRIES.get(e["stock_code"]) or {}).get("company_name_zh")})
    rows.sort(key=lambda r: r["published_at"])

    active: dict[tuple, dict] = {}
    events: list[dict] = []
    for r in rows:
        key = (r["stock_code"], r["event_type_primary"])
        t = datetime.fromisoformat(r["published_at"])
        ev = active.get(key)
        if ev is not None:
            last, first = datetime.fromisoformat(ev["last_seen_at"]), datetime.fromisoformat(ev["first_seen_at"])
            if abs(t - last) <= SLIDING_WINDOW and EVENT_MAX_TIMESPAN - (max(t, last) - min(t, first)) > timedelta(0):
                ev["last_seen_at"] = max(last, t).isoformat()
                ev["constituents"].append(r)
                continue
        ev = {"event_id": f"{key[0]}-{key[1]}-{len(events)}", "stock_code": key[0], "event_type_primary": key[1],
              "company_name": r["company_name"], "first_seen_at": t.isoformat(), "last_seen_at": t.isoformat(),
              "constituents": [r]}
        active[key] = ev
        events.append(ev)

    for ev in events:   # §3.3: up to 2 secondary types, frequency-ranked
        counts: dict[str, int] = defaultdict(int)
        for c in ev["constituents"]:
            if c["event_type_secondary"] and c["event_type_secondary"] != ev["event_type_primary"]:
                counts[c["event_type_secondary"]] += 1
        ev["event_type_secondary"] = sorted(counts, key=lambda k: -counts[k])[:2] or None
        ev["sources"] = sorted({c["source_name"] for c in ev["constituents"]})
    return events


def hand_built(events: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for ev in events:
        groups[(ev["stock_code"], ev["event_type_primary"])].extend(ev["constituents"])
    out = []
    for (code, etype), cs in groups.items():
        if len({c["source_name"] for c in cs}) < 2:
            continue
        cs = sorted(cs, key=lambda c: c["published_at"])
        counts: dict[str, int] = defaultdict(int)
        for c in cs:
            if c["event_type_secondary"] and c["event_type_secondary"] != etype:
                counts[c["event_type_secondary"]] += 1
        out.append({"event_id": f"hand-{code}-{etype}", "hand_built": True, "stock_code": code,
                    "event_type_primary": etype, "company_name": cs[0]["company_name"],
                    "first_seen_at": cs[0]["published_at"], "last_seen_at": cs[-1]["published_at"],
                    "constituents": cs, "event_type_secondary": sorted(counts, key=lambda k: -counts[k])[:2] or None,
                    "sources": sorted({c["source_name"] for c in cs})})
    return out


def main() -> None:
    DATA.mkdir(exist_ok=True)
    articles = load_articles()
    asyncio.run(analyse_all(articles))
    events = aggregate(articles)
    events += hand_built(events)
    EVENTS.write_text(json.dumps(events, ensure_ascii=False, indent=1))
    by_size: dict[int, int] = defaultdict(int)
    for ev in events:
        by_size[len(ev["constituents"])] += 1
    multi_source = sum(len(ev["sources"]) > 1 for ev in events)
    print(f"{len(events)} events ({sum(bool(e.get('hand_built')) for e in events)} hand-built); constituents per event {dict(sorted(by_size.items()))}; multi-source {multi_source}")


if __name__ == "__main__":
    main()
