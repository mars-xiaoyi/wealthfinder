# SADI — Claude Implementation Guide

> **Before touching any code:** Read `docs/implementation.md` (the authoritative spec) and `docs/architecture/system-design.md` (the TAD). This file is a navigation aid, not a replacement.

---

## Working Rules

### 1. Plan before implementing each phase
Before writing any code for a new phase, re-read the relevant sections of `docs/implementation.md`. Then:
- Identify anything that looks ambiguous, missing, or in conflict with other sections
- Raise those points explicitly and discuss before proceeding
- State what you plan to implement and how, and wait for confirmation

Do not begin coding until the plan is agreed.

### 2. Explain before modifying existing code
When updating an existing implementation, state clearly:
- Which file and function(s) will change
- What the change is and why

Then wait for confirmation before making edits.

### 3. Add dependencies to requirements.txt only when first needed
When a phase introduces a new dependency, add it to `requirements.txt` as part of the plan presented before coding — not speculatively ahead of time. Install with:
```bash
uv pip install -r requirements.txt
```
Never install packages ad-hoc without updating `requirements.txt` first.

### 4. Write unit tests for every function created or updated
After implementing or modifying any function, write a corresponding unit test. Tests live in `tests/` mirroring the `app/` structure (e.g. `app/cleaner/text_normaliser.py` → `tests/cleaner/test_text_normaliser.py`). Tests must cover:
- The happy path
- Key failure/edge cases documented in `docs/implementation.md`

---

## Critical Design Contracts

These are the non-obvious invariants most likely to be violated. Full rationale is in the impl doc.

- **Crawlers never touch DB or Redis.** Fetching and parsing only. `CrawlService.execute()` handles all persistence and stream signals after `crawler.run()` returns.
- **ACK only after both writes succeed.** `StreamHandler.ack()` must be called only after the `cleaned_news` row is committed AND `stream:raw_news_cleaned` is published. On DB failure: raise, leave unACKed, let `reclaim_loop` redeliver.
- **`stream:crawl_completed` is always published** — SUCCESS or FAILED, even if zero records were saved.
- **No magic stream name strings.** All stream names are constants in `app/redis/stream_client.py`. Import from there everywhere.
- **`os.environ` only in `app/config.py`.** All other modules use `get_config()`.
- **Routes never access `app.state` directly.** Use `Depends()` helpers from `app/api/dependencies.py`.
- **`YahooHKCrawler` is the only crawler that takes `db` in its constructor.** `CrawlService._create_crawler()` handles this — callers never construct it directly.

---

## Reference

| Doc | Path |
|-----|------|
| Implementation guide (authoritative spec) | `docs/implementation.md` |
| TAD (architecture decisions) | `docs/architecture/system-design.md` |
| API spec | `../docs/api.md` |
| Implementation progress | `progress.md` |
