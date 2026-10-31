# Spike: Scoring LLM Skills

This spike ran BriefSummarySkill (implementation.md §8.3) and EventLLMScoringSkill (§8.4) against live Vertex AI on aggregated Events built from real SADI articles, wrote their few-shot examples, and compared prompt and design variants against a `gemini-3.1-pro-preview` reference. The chosen design is written into the docs: implementation.md §8, §8.3, §8.4, §18 and system-design.md §9.1, §9.5, §9.6, §14 (Q-8 resolved, I-3 closed for both Skills). This folder keeps the results behind those choices and a runnable reference of the documented Skills.

## Decision

Both Skills stay on `gemini-3.1-flash-lite`, one call each, EventLLMScoringSkill reading BriefSummarySkill's output as designed. What changed:

- **BriefSummarySkill's `summary_full` may only carry impact or outlook the sources state.** The original spec asked it to cover 潛在影響 while also forbidding anything the input doesn't contain; the model resolved that by adding its own reading ("反映市場沽壓較為明顯"). Two new rules keep numbers as the input states them and pick the 全日/latest figure when one measure has several values.
- **No total-character cap on BriefSummarySkill's input (Q-8).** The per-source cap (3 per source) is the only bound. The 200-character cap cut 16 of the 574 Events; in 7 of them the dropped summaries held figures the kept ones didn't (e.g. 中海外's 8月銷售額 −12.8%, whose score went from −1 to −3 once it was in). Without it the largest input grew from 1,536 to 1,726 tokens.
- **BriefSummarySkill runs at temperature 0.** Over 20 Events × 5 runs at 0.1, `summary_full` changed in 16 Events and the score in 4 (by up to 2), because the scorer reads `summary_full`; at 0, 6 and 1 (both on the pre-writeback prompt; the documented prompt's own run is in the full-set table below). Max tokens are 1024 (Brief) and 512 (Scoring).
- **Edge Case Rule 3 (English source) is reworded.** BriefSummarySkill never sees article text, only `entity_summary`, which EntityAnalysisSkill writes in Traditional Chinese for every source (196 of the 603 entity events come from English-titled articles, and all 196 summaries are in Chinese); what reaches it in English is names (Medcaptain Medical Technology, Workato). The rule now covers that.
- **EventLLMScoringSkill scores only what the event adds** (principle 4): a company's existing state, such as a long suspension, is not new information.
- Few-shot examples: 3 for §8.3, 5 for §8.4, in the categories the docs named.

## How the variants were compared

- **Events.** The chosen EntityAnalysisSkill design (`../llm-skill/reference_entity_analysis.py`) ran on the 441 articles earlier spikes had already read (never the `reserved` split), and the results were aggregated with §12.4's merge rule (group key stock + event type, `SLIDING_WINDOW_HOURS=4`, `EVENT_MAX_TIMESPAN_HOURS=24`, PRD defaults): 533 Events, only 6 reported by more than one source. 41 hand-built Events add the multi-source case: every (stock, event type) at least two sources reported over the whole export, with no time window. 574 in all.
- **Eval set.** 223 of them (every multi-article Event plus a stratified sample of single-article ones), split by stock code into dev (132, used for tuning) and test (91, held out).
- **Score reference.** `gemini-3.1-pro-preview` (thinking on) scores each Event from **all** its article summaries with §8.4's rubric, twice; the second run is Pro's own agreement, the ceiling.
- **Summary judge.** Pro grades each summary against all article summaries: unsupported claims, missed price-relevant facts, `summary_short`/`key_numbers` checks, a 1–5 quality score. Judging the same outputs twice moved "any unsupported claim" by 0.015 and quality by 0.05.
- **Noise.** Rerunning one config unchanged (V4b) changed 20 of 132 summaries and 4 of 132 scores at temperature 0, and moved Spearman by 0.016. Score differences are given with paired-bootstrap 95% intervals.

## Results

Score columns compare against Pro's reference; "unsupported" is the share of summaries with at least one claim no source supports.

**Dev (132 Events; Pro vs Pro: Spearman 0.941, sign agreement 0.902)**

| Variant | Spearman | Sign agree | Unsupported | Missed facts / multi-article Event | Quality |
|---|---|---|---|---|---|
| V0 as originally documented (cap 200, temp 0.1) | 0.844 | 0.750 | 0.485 | 0.25 | 4.27 |
| V1 V0 + no cap, temp 0, numbers rule, new-info rule | 0.819 | 0.742 | 0.402 | 0.22 | 4.38 |
| V2 V1, Scoring reads the article summaries | 0.807 | 0.727 | 0.409 | 0.20 | 4.39 |
| V3 V1 as one combined call | 0.866 | 0.795 | 0.652 | 0.38 | 3.80 |
| **V4 V1 + sourced-only `summary_full`, duplicates rule (documented, except Rule 3's wording and §8.4 Example 5's last clause; the final docs were measured in the full-set run below)** | **0.853** | **0.780** | **0.083** | **0.16** | **4.77** |
| V4b V4 again, unchanged | 0.837 | 0.773 | 0.098 | 0.19 | 4.75 |
| V5 V4, Scoring zero-shot | 0.846 | 0.795 | 0.063 | 0.19 | 4.75 |
| V6 V4 + a +3 analyst-upgrade example | 0.841 | 0.773 | 0.068 | 0.17 | 4.77 |
| V7 V4, Scoring on `gemini-3.8-flash` | 0.874 | 0.826 | 0.083 | 0.20 | 4.77 |
| V8 V4 without the new-info rule | 0.811 | 0.742 | 0.076 | 0.19 | 4.75 |
| V9 V4 with a stricter timing rule in place of the duplicates rule | 0.808 | 0.750 | 0.061 | 0.09 | 4.82 |

**Test (91 held-out Events; Pro vs Pro: Spearman 0.967, sign agreement 0.912)**

| Variant | Spearman | Sign agree | Unsupported | Missed facts / multi-article Event | Quality |
|---|---|---|---|---|---|
| V0 | 0.847 | 0.802 | 0.407 | 0.48 | 4.36 |
| **V4** | 0.851 | 0.835 | **0.022** | **0.28** | **4.85** |
| V7 | 0.897 | 0.835 | 0.022 | 0.21 | 4.89 |

What decided it:

- **The summary fix is large and holds on the held-out set:** unsupported claims 41% → 2%, quality 4.36 → 4.85, missed facts on multi-article Events 0.48 → 0.28 (V0 → V4 bundles all the changes; the cap's own share is the Q-8 evidence above).
- **No Flash-Lite scoring variant beats V0 beyond noise.** Every interval for V1–V6 against V0 contains 0 (test: V4 − V0 Spearman +0.007, −0.102..+0.131). The gap to Pro's ceiling is the model, not the prompt.
- **The new-info rule stays** because it stops routine announcements of suspended companies from scoring −4/−5 (e.g. a board-meeting notice −4 → 0). Without it (V8) agreement with Pro is lower but within noise (Spearman −0.042, −0.105..+0.005; sign −0.038, −0.076..+0.000).
- **`gemini-3.8-flash` for Scoring (V7) scored best on both sets but not significantly against the documented V4** (V7 − V4: dev Spearman +0.020, −0.044..+0.092, sign +0.045, −0.015..+0.106; test Spearman +0.045, −0.013..+0.126, sign −0.000, −0.077..+0.077). Against V0 its dev sign agreement is higher beyond noise (+0.076, +0.015..+0.136). It costs 2.5× the output tokens (thinking), p95 20.5 s for both calls together against Flash-Lite's 7.2 s, and needed retries in both runs. Worth re-checking at volume, not switching now.
- **A combined single call (V3) writes worse summaries** (quality 3.80). Feeding Scoring the article summaries (V2) gains nothing over the Brief.
- **V9's stricter timing rule** halved the timing errors but cost score agreement beyond noise (Spearman −0.045, −0.092..−0.010).
- **Pro and the zero-shot variants use almost only the table's anchor values** (±1, ±3, ±5); the few-shots teach in-between values (+4, −2). Rank correlation is unaffected; MAE against Pro is, so it isn't used to compare.

## The documented Skills on all 574 Events

`run_eval.py` on the docs as written (`data/runs/`, gitignored):

| Check | Result |
|---|---|
| Fallbacks / retries | 0 / 0 for both Skills |
| Output tokens | Brief p95 177, max 240 (limit 1024); Scoring p95 79, max 89 (limit 512) |
| Latency | Brief p95 2.3 s, max 6.0 s; Scoring p95 2.1 s, max 6.8 s (an earlier full run of the same Brief prompt had a 14.0 s Scoring maximum) |
| Truncated by business validation | `summary_short` 4, `summary_full` 5, `adjustment_reason` 1 |
| Inputs without any number | 106, of which 1 got a `key_number` |
| Numbers not literally in the input | 24, hand-checked: 22 are rounding or unit conversions that are numerically right (1.3倍 → +130%, 1,668,176股 → 166.8萬股) but that Rule 4 (不得自行…換算) asks the model not to make; 2 are one error, 智譜's 156.6億港元 + 201.4億人民幣 written as "逾350億" |
| Simplified characters | none; the 7 flags (台, 床, 布, 折, 周) are standard Traditional forms |
| `stock_impact_score` | every value −5..+5 used; 35% are 0 |
| Run to run, 20 Events × 5 | `summary_full` changed in 7, the score in 1 (by 1) |
| Source cap | cut 11 Events; the largest input was 470 characters |

## Findings for other tickets

- **Aggregation:** `GENERAL_ANNOUNCEMENT` works as a catch-all, so unrelated market-wrap items about one stock (a price move, northbound flow, a product launch) merge into one Event. AASTOCKS market wraps make up most `GENERAL_ANNOUNCEMENT` Events.
- **Entity Analysis:** `entity_summary` drops the time of day of price moves ("升11.09%" vs "全日升9.47%"), which is where most of the remaining unsupported summary claims come from. It also sometimes writes a wrong company name into a summary (麥科田 as 邁德醫療/邁克生物).
- **Per-source cap:** keeping the 3 latest per source can keep near-duplicate reprints of one story and drop a different one.

## Code

- **`reference_scoring.py`:** both Skills as documented, in one file. It reads the system prompts and few-shots from the current implementation.md and builds the user messages from §8.3/§8.4's templates; `generate_structured()` is the Entity Analysis spike's.
- **`build_events.py`:** runs Entity Analysis on the already-read articles and aggregates them into `data/events.json`.
- **`run_eval.py`:** the documented Skills on every Event, plus the run-to-run check.
- **`rounds.py`:** the variant rounds, Pro reference and judge, report and bootstrap. It reads the prompts as they stood before the writeback (git `9fa749d`) so the rounds reproduce.

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -r ../llm-skill/requirements.txt
gcloud auth application-default login
.venv/bin/python build_events.py                  # reads poc/sapi/cleaned_news_export.csv (gitignored)
.venv/bin/python reference_scoring.py --event-id hand-00388-GENERAL_ANNOUNCEMENT
.venv/bin/python run_eval.py prod
.venv/bin/python rounds.py ref dev && .venv/bin/python rounds.py run V4 dev && .venv/bin/python rounds.py report dev V4
```

`data/` holds article-derived text (summaries, raw responses) and is never committed. No `reserved` article was read.
