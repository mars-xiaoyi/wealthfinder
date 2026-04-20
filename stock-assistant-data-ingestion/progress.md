# SADI — Implementation Progress

Last updated: 2026-04-17

---

## Status Legend
- `[ ]` Not started
- `[~]` In progress
- `[x]` Done

---

## Phase 1 — Foundation

- [x] `app/config.py` — `AppConfig`, `load_config()`, `get_config()` singleton
- [x] `config/sources.yaml` — per-source crawl behaviour config

## Phase 2 — Infrastructure Clients

- [x] `app/db/connection.py` — `DatabaseClient` (`execute`, `fetch_one`, `fetch_all`, `close`), `create_db_client()`
- [x] `app/redis/stream_client.py` — `StreamClient` + stream name constants, `create_stream_client()`

## Phase 3 — Data Models & API Contracts

- [x] `app/models/raw_news.py`
- [x] `app/models/cleaned_news.py`
- [x] `app/models/crawl_error_log.py`
- [x] `app/api/schemas.py` — Pydantic schemas (`CrawlRequest`, `CrawlResponse`, `CleanedNewsRecord`, `BatchRequest`, `BatchResponse`, `HealthResponse`, `ErrorResponse`)
- [x] `app/api/dependencies.py` — `get_db`, `get_crawl_service`, `get_stream_client`, `get_config`

## Phase 4 — Health Route

- [x] `app/api/routes/health.py` — `GET /v1/health`

## Phase 5 — Crawler Utilities

- [x] `app/crawl/fetchers/feed_fetcher.py` — `fetch_rss()`, `FeedEntry`
- [x] `app/crawl/parsers/html_parser.py` — `extract_body_auto()`, `extract_body_css()`
- [x] `app/crawl/parsers/pdf_parser.py` — `parse_pdf()`, `PdfEncryptedError`, `PdfParseError`
- [x] `app/crawl/fetchers/browser_manager.py` — `BrowserManager` (`start`, `stop`, `acquire_context`, `release_context`)
- [x] `app/crawl/fetchers/page_crawler.py` — `PageCrawler.fetch()` with error log pre-check + retry, `CrawlSkippedError`, `CrawlNetworkError`, `CrawlBlockedError`
- [x] `app/common/error_codes.py` — `ErrorCode` base, `NetworkErrorCode` (SADI-61xx), `DocumentParseErrorCode` (SADI-62xx)

## Phase 6 — Crawler Core

- [x] `app/crawl/crawlers/base_crawler.py` — `BaseCrawler` ABC, `CrawlResult`, `CrawlSuccessItem`, `CrawlFailItem`, `CrawlFatalError`
- [x] `app/crawl/crawlers/hkex_crawler.py` — `HKEXCrawler` (Phase 1 playwright pagination + Phase 2 parallel PDF fetch)
- [x] `app/crawl/crawlers/mingpao_crawler.py` — `MingPaoCrawler` (RSS + playwright browser)
- [x] `app/crawl/crawlers/aastocks_crawler.py` — `AAStocksCrawler` (list page + httpx)
- [x] `app/crawl/crawlers/yahoo_hk_crawler.py` — `YahooHKCrawler` (RSS + trafilatura + coverage gap detection)
- [x] `app/crawl/crawl_service.py` — `CrawlService` (`_create_crawler`, `execute`, `_save_crawl_error`)
- [x] `app/api/routes/crawl.py` — `POST /v1/crawl`

## Phase 7 — Cleaning Layer

- [x] `app/common/text_utils.py` — `normalise()`, `compute_hash()` _(pure text tools pulled forward — shared by crawl & clean layers)_
- [x] `app/cleaner/dedup_service.py` — `is_duplicate()` _(pulled forward — query helper used by Phase 7)_
- [x] `app/cleaner/stream_handler.py` — `StreamHandler` (`ensure_consumer_group`, `read_messages`, `reclaim_pending`, `ack`, `publish_cleaned`)
- [x] `app/cleaner/cleaning_service.py` — `CleaningService` (`start`, `process_record`, 7-step pipeline, `_mark_deleted`, `_fetch_raw_news`, `_insert_cleaned_news`)
- [x] `app/api/routes/cleaned_news.py` — `GET /v1/cleaned_news/{id}`, `POST /v1/cleaned_news/batch`
- [x] `app/api/main.py` — `create_app()` with routers + exception handlers

## Phase 8 — Database Schema

- [x] `alembic.ini` — Alembic configuration
- [x] `alembic/env.py` — Migration runner (reads `DATABASE_URL` from env)
- [x] `alembic/script.py.mako` — Migration file template
- [x] `alembic/versions/001_create_tables.py` — `raw_news`, `cleaned_news`, `crawl_error_log` tables + indexes

## Phase 9 — Service Wiring

- [ ] `app/main.py` — lifespan handler (startup + shutdown), `app` instance

## Phase 10 — Packaging

- [ ] `requirements.txt`
- [ ] `Dockerfile`
- [ ] `docker-compose.yml`

---

## TODO

- [x] Time a real HKEX Phase 1 run (`page.goto` + LOAD MORE loop) to pick a reasonable default for `CRAWL_BROWSER_NAV_TIMEOUT_MS`. _Measured 2026-04-15 via `poc/sadi/time_hkex_phase1.py` against 2026-04-14 (560 filings, 5 LOAD MORE clicks). Per-op p95: `page.goto` 3656 ms, `click` 155 ms, XHR-wait 625 ms, `query_selector_all` 482 ms. Default lowered 30000 → 15000 ms (~4× worst-op p95). Also fixed a latent bug: `_collect_announcements` was using `wait_for_load_state("domcontentloaded")` which returns before the LOAD MORE XHR completes, causing detached-element hangs — replaced with `page.wait_for_function` polling the pagination counter._
- [x] Run integration tests against live sources for all 4 crawlers. _Added `tests/integration/crawlers/` opt-in via `pytest -m live` (registered in `pytest.ini`; skipped by default). Stub DB satisfies the error-log and coverage-gap reads; real httpx + real playwright exercise fetch/parse. Results on 2026-04-15: **Yahoo HK** ✅ 5 successes / 0 failures (2.6 s), **AAStocks** ✅ 21 / 0 (12 s), **HKEX** ✅ 379 / 0 for 2026-04-14 (94 s, Phase 1 560 rows → 379 parsed, rest were empty-body skips), **Ming Pao** ❌ 0 / 0 — blocked by Cloudflare (see follow-up)._
- [x] **MingPaoCrawler blocked by Cloudflare.** _Fixed 2026-04-15 in shared `BrowserManager`: `launch(channel="chromium", args=["--disable-blink-features=AutomationControlled"])` (forces the full Chromium build over `chromium_headless_shell`, hides the `navigator.webdriver` tell) and `new_context(user_agent=<desktop Chrome UA>, locale="zh-HK", extra_http_headers={"Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8"})`. Live results 2026-04-15: **MingPao** 88 / 0 (148 s), **HKEX** regression-check 379 / 0 (97 s, unchanged). Deployment note: needs `playwright install chromium` (full build), not just `chromium-headless-shell`._
- [x] **HKEX data-quality spot checks.** _Investigated 2026-04-16 by adding row-bucket instrumentation to `_collect_announcements` (drop reasons: `no_pdf_link`, `no_href`). Live re-run on 2026-04-14 showed `total=560 parsed=379 drops={'no_pdf_link': 181}` — the 181 "missing" rows are not PDF-parse failures or empty bodies, they are legitimate drops at `_extract_row` for rows without a `td > div.doc-link > a[href$='.pdf']` anchor (multi-document filings, HTML-only notices, non-PDF forms). The empty-body branch at `hkex_crawler.py:285-287` is effectively dead code given `parse_pdf`'s contract — left in place as a defensive guard. Separately, the `published_at=None` issue was **100% of successes, not "at least one"**: live `td.release-time` cells render as ` Release Time: DD/MM/YYYY HH:MM` and the bare `strptime("%d/%m/%Y %H:%M")` rejected every one. Fixed by regex-extracting the datetime substring in `_parse_release_time`. Post-fix live run: 379/379 successes carry `published_at`, 0 warnings._

---

## Open Questions

| # | Question | Status |
|---|---|---|
| Q-2 | Validate `CLEAN_BODY_MIN_LENGTH = 50` against real crawl data | Unresolved — do not implement workarounds |
| Q-4 | `CleaningService` processes records one-by-one (3–4 DB round-trips per record). For high-volume crawls (e.g. HKEX 500+ filings), consider batching DB operations in the worker — batch idempotency checks, fetches, dedup lookups, and inserts to reduce round-trips from ~4N to ~4 per batch. Tradeoff: added complexity in per-item error handling and ACK tracking within a batch. | Unresolved — evaluate after observing real throughput |
| Q-3 | Audit `_parse_published_at` in all crawlers — current `re.search` over full page HTML is fragile (can match sidebar dates, footer copyright, embedded scripts). Scope regex to a CSS-selected DOM element instead. Needs sample article HTML from each source to identify the correct timestamp selector. | Resolved 2026-04-16. Sampled live HTML from both affected sources. **AAStocks**: timestamp sits in `div.newstime5` (skip the `newshead-Source` sibling), inside a `document.write(ConvertToLocalTime({dt:'YYYY/MM/DD HH:MM'}))` JS call — `_parse_published_at` now scopes the existing regex to that div's inner HTML. **Ming Pao**: timestamp sits in `div.date.color2nd` (e.g. `2026年4月15日 星期三　6:04AM`); the `div.date[itemprop="datePublished"]` sibling only carries the date with no time and is avoided — `_extract_published_at_from_page` now reads `get_text()` off the scoped node. HKEX (`td.release-time`) and Yahoo HK (RSS `pubDate` only) were already CSS-scoped / not affected. Unit tests added for noise-rejection (sidebar/footer dates, date-only sibling); live re-run: AAStocks 19/0, MingPao 88/0, both with populated timestamps. |
