# Spike: Entity Analysis LLM Skill

This spike tested implementation.md §8.2's EntityAnalysisSkill against live Vertex AI, then compared candidate designs on real SADI articles. The chosen design is written into the docs: implementation.md §4.1, §7, §8, §8.2, §18 and system-design.md §8, §9.1–§9.4, §9.8, §14 (commits `b59dbe8`, `74fc31f`). The lookup_stock fuzzy-matching spike then set §4.2's name matching, which both name checks below use (`ea36df0`). This folder keeps the results behind that choice, a runnable reference of the design, and the article IDs reserved for implementation testing.

## Decision

EntityAnalysisSkill makes **one structured-output call on `gemini-3.1-flash-lite`**. The model lists every company it finds, with the code the article prints (or the HKEX code it knows) and the name as written. **Code then verifies each one** against the HK Stock List:

1. The code is on the list and the listed name agrees with the model's name.
2. Otherwise the article prints that code.
3. Otherwise a name search over the list (§4.2 `lookup_stock`).

Anything that resolves through none of these is dropped.

This replaces the original design, a function-calling loop where the model called a `lookup_stock` tool, followed by a separate Instructor structured-output call.

## Why

The main comparison used 127 real articles from the cleaned-news export, none of them seen while the designs were tuned: 60 Ming Pao, 40 HKEX, 15 AASTOCKS and 12 Yahoo HK.

- **Gold:** for 68 articles, the codes the article prints (for HKEX announcements, the issuer code). The other 59 were adjudicated by reading them; 48 of those name no listed company.
- **Classification:** agreement with `gemini-3.1-pro-preview` on codes both runs returned. Pro agrees with itself on 0.954 of event types and 0.972 of sentiments, which is the ceiling.
- **Cost:** list prices for the `global` endpoint, with thinking tokens billed as output.

| Design (127 unseen articles) | Entity F1 | Event / sentiment agreement with Pro | $ per 1k articles | p95 / max s per article | Failed calls |
|---|---|---|---|---|---|
| **One call + code-side verification, 3.1-flash-lite** | **0.962** | 0.890 / 0.908 | **1.14** | **6 / 12** | **0** |
| The same, with HKEX routed to 3.8-flash (thinking LOW) | 0.962 | 0.917 / 0.908 | 1.92 | 76 / 253 | 6 |
| One call + code-side verification, 3.8-flash LOW | 0.947 | 0.918 / 0.864 | 2.98 | 130 / 253 | 34 |
| Function-calling loop + structured call, 3.8-flash LOW | 0.930 | 0.931 / 0.931 | 8.74 | 217 / 342 | 27 |
| Function-calling loop + structured call, 3.1-pro-preview | 0.703 | — | 41.33 | 53 / 109 | 9 |

These runs used this spike's own normalization for step 1; on the same stored outputs, the fuzzy-matching spike measured that switching step 1 to §4.2's shared normalizer leaves entity F1 unchanged (0.962 here, 0.908 on the no-code set; its `results_shared_normalizer.md`). An earlier 31-article set gave the same picture, and so did a 26-article holdout: F1 0.996 and 1.000 for the chosen design, at $1.57 and $1.03 per 1k articles. §8.2 as originally written (the loop on 3.8-flash at default thinking) cost $23.09 per 1k articles.

What decided it:

- **Articles almost always print the code, and the model knows most HKEX codes.** That makes verification a deterministic check rather than a search the model has to drive, so the tool loop adds calls without adding accuracy.
- **The loop had failure modes of its own.**
  - With `function_calling_config.mode=ANY` it never terminates.
  - With `AUTO` it sometimes re-queries an already-verified code until it hits the round cap (2 of 7 small-article runs).
  - Dense market wraps take 4–6 rounds at 10–16 lookups a turn.
  - Pro's loop missed 7, 21 and 30 companies on three dense wraps.
  - 3.8-flash on LOW thinking once returned nothing for a 121-company calendar, which the single call on Lite covered 120 of 121.
- **Classification quality is the same across designs:** event agreement 0.89–0.93 and sentiment 0.86–0.93 in the table above.
- **3.8-flash was unreliable.** 7.6% of its 1,376 attempts failed (55 timeouts, 24 × 429). During a ~20-minute capacity episode, 74 of 127 single-call articles failed all 4 attempts. Flash-Lite failed 0.6% of 838 attempts, with p95 8 s.
- **Routing HKEX to 3.8-flash bought nothing measurable.** Both models agree with Pro on HKEX sentiment for 38 of 41 entities.

The trade-offs we accepted:

- **Articles that name companies without printing any code.** They rely on the model's own code knowledge plus the name search. With every code stripped from the test set (a worst case), F1 is 0.913 against 0.984 for the tool loop. About 6% of real articles name a listed company with no code anywhere in them, mostly sector roundups.
- **Lite under-calls some negatives in formal HKEX announcements.** For example, it called a placing at a 19.5% discount NEUTRAL.
- **Lite's summaries sometimes run over 100 characters:** 0–11% depending on the set. §8.2's business validation truncates them.

## Findings the implementation relies on

| Finding | Where it went |
|---|---|
| `google-genai==2.33.0`. Native structured output (`response_mime_type="application/json"`, `response_json_schema`) enforces fields and types, so Instructor isn't needed. Instructor 1.17's re-ask in `GENAI_STRUCTURED_OUTPUTS` mode sends the error as a model turn, which Gemini rejects with `400 Requests ending with a model turn are not supported`. | §7.1, §7.3 |
| The SDK has **no default timeout**: calls hung for ~260 s before one was set. With `HttpOptions(timeout=ms)`, a hang comes back as `504 DEADLINE_EXCEEDED`. Without `retry_options` the SDK makes exactly one attempt. | §7.3 |
| Tokens: input is `usage_metadata.prompt_token_count`. Output is `candidates_token_count` plus `thoughts_token_count`; thinking tokens are reported separately (`None` when the model doesn't think), billed as output, and counted against `max_output_tokens`. | §7.1, §7.3 |
| Current Gemini models only answer on the `global` endpoint, not `asia-east1`/`asia-east2`. Responses report `traffic_type=ON_DEMAND`, and 429s arrived at ≤ 4 concurrent calls: a shared quota pool, with no fixed RPM to tune `ENTITY_ANALYSIS_MAX_CONCURRENT` against. | §4.1, Q-9 |
| Flash-Lite latency on EntityAnalysis calls: p95 8 s, max 18 s, so `LLM_API_TIMEOUT_S=30` holds. The largest output was 13,917 tokens (a 121-company calendar), with every other article under 4,400, so max tokens is 16,384. | §4.1, §8 |
| One cleaned HKEX announcement is 2,069,932 characters. The body is capped at 30,000 characters before the call. | §4.1, §8.2, design §9.3 |
| Explicit context caching needs ≥ 4,096 tokens and the prompt is ~1,800, so caching isn't a cost lever. | — |

## Code

- **`reference_entity_analysis.py`:** the chosen design in one runnable file. It covers `generate_structured()` (§7.3: native structured output, timeout, token accounting, error mapping) and the Skill (§8.2: one call, `verify_mention()`, business-constraint validation, dedup). It reads the system prompt from the current implementation.md §8.2, so it always runs the documented prompt. Run on 6 already-used articles (AASTOCKS, HKEX, Ming Pao, Yahoo HK), it found every gold code.
- **`fuzzy_import.py`:** imports the lookup_stock fuzzy-matching spike's name matching from `poc/sapi/fuzzy-match/` at runtime: its name forms (step 1, `names_agree()`), its S8 search (step 3) and its HK Stock List snapshot. None of its code is copied here; in the service both are §4.2's `app/common/stock_name_match.py`.

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -r requirements.txt
gcloud auth application-default login
.venv/bin/python reference_entity_analysis.py --title "..." --body "..."
.venv/bin/python reference_entity_analysis.py --cleaned-id <uuid>   # reads poc/sapi/cleaned_news_export.csv (gitignored)
```

## Test data for implementation

`cleaned_news_split.json` splits `poc/sapi/cleaned_news_export.csv` (729 articles published 2026-09-01..13) by `cleaned_id`. **`reserved`** holds 288 articles published 09-08..13 that no spike has read: 128 HKEX, 97 Ming Pao, 40 AASTOCKS and 23 Yahoo HK. Use those for implementation testing. `verify` (127, this spike's test set), `read_by_other_spike` (312, published 09-01..07) and `spent` (2) have already been used. The file holds IDs only; article text is never committed.
