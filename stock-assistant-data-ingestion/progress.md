# SADI — Implementation Progress

Last updated: 2026-04-07

---

## Status Legend
- `[ ]` Not started
- `[~]` In progress
- `[x]` Done

---

## Phase 1 — Foundation

- [ ] `app/config.py` — `AppConfig`, `load_config()`, `get_config()` singleton
- [ ] `config/sources.yaml` — per-source crawl behaviour config

## Phase 2 — Infrastructure Clients

- [ ] `app/db/connection.py` — `DatabaseClient` (`execute`, `fetch_one`, `fetch_all`, `close`), `create_db_client()`
- [ ] `app/redis/stream_client.py` — `StreamClient` + stream name constants, `create_stream_client()`

## Phase 3 — Data Models & API Contracts

- [ ] `app/models/raw_news.py`
- [ ] `app/models/cleaned_news.py`
- [ ] `app/models/crawl_error_log.py`
- [ ] `app/api/schemas.py` — Pydantic schemas (`CrawlRequest`, `CrawlResponse`, `CleanedNewsRecord`, `BatchRequest`, `BatchResponse`, `HealthResponse`, `ErrorResponse`)
- [ ] `app/api/dependencies.py` — `get_db`, `get_crawl_service`, `get_stream_client`, `get_config`

## Phase 4 — Health Route

- [ ] `app/api/routes/health.py` — `GET /v1/health`

## Phase 5 — Crawler Utilities

- [ ] `app/crawler/feed_fetcher.py` — `fetch_rss()`, `FeedEntry`
- [ ] `app/crawler/html_parser.py` — `extract_body_trafilatura()`, `extract_body_css()`
- [ ] `app/crawler/pdf_parser.py` — `parse_pdf()`, `PdfEncryptedError`, `PdfParseError`
- [ ] `app/crawler/browser_manager.py` — `BrowserManager` (`start`, `stop`, `acquire_context`, `release_context`)
- [ ] `app/crawler/page_crawler.py` — `PageCrawler.fetch()` with error log pre-check + retry, `CrawlSkippedError`, `CrawlNetworkError`, `CrawlBlockedError`

## Phase 6 — Crawler Core

- [ ] `app/crawler/base_crawler.py` — `BaseCrawler` ABC, `CrawlResult`, `CrawlSuccessItem`, `CrawlFailItem`, `CrawlFatalError`
- [ ] `app/crawler/hkex_crawler.py` — `HKEXCrawler` (Phase 1 playwright pagination + Phase 2 parallel PDF fetch)
- [ ] `app/crawler/mingpao_crawler.py` — `MingPaoCrawler` (RSS + playwright browser)
- [ ] `app/crawler/aastocks_crawler.py` — `AAStocksCrawler` (list page + httpx)
- [ ] `app/crawler/yahoo_hk_crawler.py` — `YahooHKCrawler` (RSS + trafilatura + coverage gap detection)
- [ ] `app/crawler/crawl_service.py` — `CrawlService` (`_create_crawler`, `execute`, `_save_crawl_error`)
- [ ] `app/api/routes/crawl.py` — `POST /v1/crawl`

## Phase 7 — Cleaning Layer

- [ ] `app/cleaner/text_normaliser.py` — `normalise()`
- [ ] `app/cleaner/dedup_service.py` — `compute_title_hash()`, `is_duplicate()`
- [ ] `app/cleaner/stream_handler.py` — `StreamHandler` (`ensure_consumer_group`, `read_messages`, `reclaim_pending`, `ack`, `publish_cleaned`)
- [ ] `app/cleaner/cleaning_service.py` — `CleaningService` (`start`, `process_record`, 7-step pipeline, `_mark_deleted`, `_fetch_raw_news`, `_insert_cleaned_news`)
- [ ] `app/api/routes/cleaned_news.py` — `GET /v1/cleaned_news/{id}`, `POST /v1/cleaned_news/batch`
- [ ] `app/api/main.py` — `create_app()` with routers + exception handlers

## Phase 8 — Database Schema

- [ ] `alembic/versions/001_create_tables.py` — `raw_news`, `cleaned_news`, `crawl_error_log` tables + indexes

## Phase 9 — Service Wiring

- [ ] `app/main.py` — lifespan handler (startup + shutdown), `app` instance

## Phase 10 — Packaging

- [ ] `requirements.txt`
- [ ] `Dockerfile`
- [ ] `docker-compose.yml`

---

## Open Questions

| # | Question | Status |
|---|---|---|
| Q-2 | Validate `CLEAN_BODY_MIN_LENGTH = 50` against real crawl data | Unresolved — do not implement workarounds |
