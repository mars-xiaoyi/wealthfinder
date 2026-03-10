# Stock Assistant Data Ingestion (SADI)
## Technical Architecture Document — v0.5

| Field | Detail |
|---|---|
| Service Name | Stock Assistant Data Ingestion (SADI) |
| Document Version | TAD v0.5 |
| Parent System | HK Stock AI Research Assistant |
| Service Responsibility | News data ingestion and cleaning |
| Tech Stack | Python + asyncio + FastAPI |
| Dependencies | PostgreSQL 16, Downstream parsing service (via NOTIFY + API) |
| Document Status | DRAFT — Pending team review |

---

## Table of Contents

1. [Service Overview](#1-service-overview)
2. [Data Source Design](#2-data-source-design)
3. [Crawler Layer](#3-crawler-layer)
4. [Cleaning Layer](#4-cleaning-layer)
5. [NOTIFY Signal Specification](#5-notify-signal-specification)
6. [API](#6-api)
7. [Database Retry Strategy](#7-database-retry-strategy)
8. [Coroutine Health Management](#8-coroutine-health-management)
9. [Deployment](#9-deployment)
10. [Open Questions](#10-open-questions)

---

## 1. Service Overview

### 1.1 Responsibility Boundary

SADI is the data entry point of the HK Stock AI Research Assistant. It is responsible for crawling raw content from configured news sources and producing clean, plain-text records for downstream consumption.

> **Core Principle:** SADI only handles data acquisition and pre-processing. It does not participate in any business analysis or content judgment. The only contract with downstream services is: every record's `body_cleaned` field is guaranteed to be plain text.

| Layer | Input | Output | Out of Scope |
|---|---|---|---|
| Crawler | RSS feeds and page URLs from `data_sources` | `raw_news` records (plain text body) + NOTIFY signal | Content analysis, classification, entity recognition |
| Cleaner | `raw_news` records with no corresponding entry in `cleaned_news` | `cleaned_news` records + NOTIFY signal to downstream | Business rules, scoring, NLP |

### 1.2 Tech Stack

| Component | Choice | Rationale |
|---|---|---|
| Runtime | Python 3.12 | Best ecosystem for crawling and AI/LLM integration |
| Async framework | asyncio | Standard library; optimal for IO-intensive workloads vs threading (GIL limitation) |
| API framework | FastAPI | Native async support; automatic OpenAPI documentation |
| HTTP client | httpx | vs aiohttp: cleaner API, supports sync/async; vs requests: native async support |
| RSS parsing | feedparser | De facto standard for RSS/Atom in Python; no viable alternative |
| Content extraction | trafilatura | vs newspaper3k: actively maintained, lighter, higher body extraction accuracy |
| HTML fallback parser | BeautifulSoup4 | CSS selector override when trafilatura extraction quality is insufficient |
| PDF parsing | pdfplumber | vs PyPDF2: significantly better text extraction for complex layouts |
| Text normalisation | unicodedata (stdlib) | NFKC normalisation for fullwidth-to-halfwidth conversion; zero dependency |
| Hash computation | hashlib (stdlib) | SHA-256 for title deduplication; zero dependency |
| Database client | asyncpg | vs psycopg2: native asyncio driver; native LISTEN/NOTIFY support (critical) |
| Database migration | Alembic | Standard Python migration tool; supports versioned seed data |
| Containerisation | Docker | Independent deployment; docker-compose for local development |

---

## 2. Data Source Design

### 2.1 MVP Data Sources

| Source | Type | Language | Strategy | Priority |
|---|---|---|---|---|
| HKEx NEWSLINE | Official RSS | EN / ZH | RSS_FULL — RSS full text, no crawler required | P1 |
| Ming Pao Finance | RSS + Web | Traditional Chinese | RSS_CRAWLER — RSS for URL discovery, Crawler for body | P1 |
| AAStocks | Web | Traditional Chinese | RSS_CRAWLER — RSS for URL discovery, Crawler for body | P2 |
| Yahoo Finance HK | RSS + Web | English | RSS_CRAWLER — RSS for URL discovery, Crawler for body | P2 |

> **Note:** NewsAPI is reserved as a post-MVP data source. The pluggable `data_sources` table design accommodates its integration without code changes.
>
> **Excluded from MVP:** SCMP (paywalled), Bloomberg HK (no stable RSS, strong anti-crawl), Reuters Asia (unstable RSS endpoints — pending Week 1 validation).

### 2.2 `data_sources` Table

All source configurations are stored in the database. Enabling or disabling a source requires only a record update — no code changes or service restarts needed.

| Field | Type | Description |
|---|---|---|
| source_id | UUID | Primary key |
| source_name | VARCHAR(50) | Source identifier, e.g. HKEX, MINGPAO, AASTOCKS, YAHOO_HK |
| source_type | VARCHAR(20) | RSS_FULL / RSS_CRAWLER / API — determines crawl strategy branch |
| rss_url | TEXT | RSS feed URL |
| base_url | TEXT | Site root URL, used for resolving relative paths in crawler |
| is_active | BOOLEAN | Whether the source is enabled; default true |
| priority | VARCHAR(5) | P1 / P2 / P3 |
| max_concurrent | INTEGER | Max concurrent crawler coroutines for this source; default 3 |
| request_interval_min_ms | INTEGER | Minimum request interval (ms); default 500 |
| request_interval_max_ms | INTEGER | Maximum request interval (ms); default 1000 |
| crawl_config | JSONB | Source-specific extension config, e.g. CSS selectors, custom headers |
| created_at | TIMESTAMPTZ | Created at UTC |
| updated_at | TIMESTAMPTZ | Last updated at UTC |

> **Constraint:** `request_interval_min_ms < request_interval_max_ms` enforced via CHECK constraint in schema.
>
> **Initialisation:** Seed data managed via Alembic migration scripts, versioned alongside table creation scripts.

**Index strategy:**
- `source_name` — unique index
- `is_active` — index

---

## 3. Crawler Layer

### 3.1 Concurrency Model

The crawler layer is fully async, driven by Python's asyncio event loop. All four sources start concurrently as independent tasks and do not interfere with each other.

```mermaid
flowchart TD
    A([Job Triggered]) --> B[Start 4 Concurrent Source Tasks]

    B --> S1[HKEx NEWSLINE]
    B --> S2[Ming Pao Finance]
    B --> S3[AAStocks]
    B --> S4[Yahoo Finance HK]

    S1 -->|RSS_FULL| R1[fetch_rss]
    R1 --> DB1[save to raw_news]

    S2 -->|RSS_CRAWLER| R2[fetch_rss]
    S3 -->|RSS_CRAWLER| R3[fetch_rss]
    S4 -->|RSS_CRAWLER| R4[fetch_rss]

    R2 --> Q2[URL Queue]
    R3 --> Q3[URL Queue]
    R4 --> Q4[URL Queue]

    Q2 --> C2[Concurrent Crawlers]
    Q3 --> C3[Concurrent Crawlers]
    Q4 --> C4[Concurrent Crawlers]

    C2 --> DB2[save to raw_news]
    C3 --> DB2
    C4 --> DB2

    DB1 --> N[NOTIFY raw_news_inserted]
    DB2 --> N
```

### 3.2 Pipeline Model

RSS_CRAWLER sources use a producer-consumer pipeline. RSS fetching (producer) and page crawling (consumer) run concurrently per source — crawling begins as soon as the first URLs are available, without waiting for other sources.

| Role | Implementation | Responsibility |
|---|---|---|
| Producer | `fetch_rss(source)` | Parses RSS feed, filters articles outside the time window, enqueues URLs |
| Consumer | `crawl_worker(queue, source)` | Dequeues URLs, fetches pages, extracts body, writes to `raw_news` |
| Queue | `asyncio.Queue` (one per source) | Decouples producer and consumer throughput; enforces per-source concurrency limit |

### 3.3 Content Format Handling

After fetching a page, the crawler determines the parsing path based on the HTTP response `Content-Type` header. URL suffix is not used — it is unreliable for dynamically generated URLs (e.g. HKEx announcement links).

| Content-Type | Parser | Output |
|---|---|---|
| text/html | trafilatura (auto-extraction); CSS selector override from `crawl_config` if quality is insufficient | Plain text body |
| application/pdf | pdfplumber — extracts PDF text layer; encrypted or scanned PDFs are logged as errors | Plain text body |
| Other | Skip | Logged to `crawl_error_log` with `error_code = UNSUPPORTED_CONTENT_TYPE` |

### 3.4 Crawl Parameters

| Parameter | Default | Description | Config Location |
|---|---|---|---|
| max_concurrent | 3 | Max concurrent crawler coroutines per source | `data_sources.max_concurrent` |
| request_interval | 500–1000ms random | Random jitter between requests to mimic human behaviour | `data_sources.request_interval_min/max_ms` |
| CRAWL_REQUEST_TIMEOUT_S | 10s | Single page fetch timeout | Environment variable |
| CRAWL_MAX_RETRY | 3 | Max retry attempts with exponential backoff | Environment variable |

### 3.5 Retry and Error Handling

Retries are scoped to the stage of failure. Successful stages are not repeated.

**Retryable failures — retry from failed stage only:**

| Failure | Stage | Action |
|---|---|---|
| Connection timeout | Network fetch | Retry fetch; exponential backoff |
| HTTP 500 | Network fetch | Retry fetch; exponential backoff |
| RSS format error | RSS parsing | Retry full source; exponential backoff |
| PDF parse failure (non-encrypted) | Content parsing | Retry parsing only; do not re-fetch page |

**Non-retryable failures — log and discard:**

| Failure | Action |
|---|---|
| HTTP 403 anti-crawl block | Log to `crawl_error_log`; discard URL |
| HTTP 404 not found | Log to `crawl_error_log`; discard URL |
| Encrypted or scanned PDF | Log to `crawl_error_log`; discard URL |
| HTML structure changed (empty body) | Log to `crawl_error_log`; requires manual intervention |
| Unsupported Content-Type | Log to `crawl_error_log`; discard URL |

> **Note:** `crawl_error_log` is a pure audit log and does not drive any retry business logic. Failed URLs within the time window will be re-discovered naturally in the next Job via RSS.

**Retry mechanism:**
- Retries execute within the same coroutine; no database state required
- Backoff formula: `wait = CRAWL_RETRY_BASE_WAIT_MS × 2^(attempt - 1)`
- After exhausting all retries, the final failure is written to `crawl_error_log`
- Database operation retries are handled separately — see [Section 7](#7-database-retry-strategy)

**Crawl-scoped coroutine error handling:**

`rss_fetcher` and `crawl_worker` run for the duration of a single crawl execution and are not managed by Watchdog. On exception:
- Exception is captured at the execution level via `asyncio.gather()`
- Failure is logged to application log and `crawl_error_log`
- Other source Tasks are unaffected and continue running
- Unprocessed URLs in the Queue are discarded; they will be re-discovered naturally in the next crawl execution via RSS

### 3.6 Data Model

**`raw_news` table** — Written by the crawler layer. Never modified after initial insert.

| Field | Type | Description |
|---|---|---|
| raw_id | UUID | Primary key |
| source_id | UUID | FK → `data_sources.source_id` ON DELETE SET NULL |
| source_url | TEXT | Original article URL; unique index |
| title | TEXT | Original title; never modified |
| body | TEXT | Plain text body guaranteed by crawler layer; never modified |
| published_at | TIMESTAMPTZ | Article publish time UTC; nullable — consuming layers fall back to `created_at` when null |
| created_at | TIMESTAMPTZ | Record creation time UTC |
| raw_hash | VARCHAR(64) | SHA-256(normalised_title); unique index for cross-source deduplication |
| is_deleted | BOOLEAN | Soft delete flag set by cleaning layer for rejected records; default false |
| deleted_reason | VARCHAR(50) | EMPTY_FIELD / DUPLICATE_TITLE / BODY_TOO_SHORT; required when `is_deleted = true` |

**`crawl_error_log` table** — Pure audit log. Does not drive any business decisions.

| Field | Type | Description |
|---|---|---|
| error_id | UUID | Primary key |
| execution_id | UUID | Execution context ID |
| source_id | UUID | FK → `data_sources.source_id` |
| url | TEXT | Failed article URL; null indicates RSS-level failure |
| error_type | VARCHAR(50) | NETWORK / PARSE / STORAGE |
| error_code | VARCHAR(50) | Specific error code, e.g. HTTP_403, PDF_ENCRYPTED, TIMEOUT |
| attempt_count | INTEGER | Total attempts made |
| created_at | TIMESTAMPTZ | Record creation time UTC |

**Index strategy:**

`raw_news`:
- `source_url` — unique index; `INSERT ON CONFLICT DO NOTHING`
- `raw_hash` — unique index; `INSERT ON CONFLICT DO NOTHING`
- `is_deleted` — index
- `created_at` — index
- `source_id` — FK index

### 3.7 Crawler Data Flow

| Stage | Action | Output |
|---|---|---|
| Crawl triggered | Read time window from request; read all `is_active = true` sources from `data_sources` | Time window + source config list |
| Concurrent start | Create one `asyncio.Task` per source; run all via `asyncio.gather()` | 4 concurrent tasks |
| RSS fetch | Parse feed; filter articles outside time window | Article list (title, url, published_at) |
| RSS_FULL save | Write directly to `raw_news`; trigger `NOTIFY raw_news_inserted(raw_id)` | `raw_news` record + NOTIFY |
| RSS_CRAWLER enqueue | Enqueue article URLs into per-source `asyncio.Queue`; crawler workers consume concurrently | URLs in queue |
| Crawler fetch | httpx request; Content-Type detection; trafilatura or pdfplumber parsing | Plain text body |
| Save | Write to `raw_news` (`INSERT ON CONFLICT DO NOTHING`); trigger `NOTIFY raw_news_inserted(raw_id)` | `raw_news` record + NOTIFY |
| Error handling | Retryable: exponential backoff up to max retries. Non-retryable: write to `crawl_error_log` immediately | `crawl_error_log` record |

---

## 4. Cleaning Layer

### 4.1 Overview

```mermaid
flowchart TD
    A([NOTIFY / Fallback Poll]) --> B[Enqueue raw_id]
    B --> C[Concurrent Workers]
    C --> D{raw_id exists in cleaned_news?}
    D -->|Yes| E([Skip])
    D -->|No| F[Execute Cleaning Steps]
    F --> G{Passed?}
    G -->|No| H[Log rejection / mark is_deleted in raw_news]
    G -->|Yes| I[Write to cleaned_news]
    I --> J[NOTIFY raw_news_cleaned]
```

### 4.2 Trigger Mechanism

| Trigger | Mechanism | Use Case |
|---|---|---|
| Primary | `LISTEN raw_news_inserted` — on NOTIFY, enqueue `raw_id` to internal queue | Normal operation, real-time processing |
| Fallback | Poll `raw_news` for records with no corresponding entry in `cleaned_news` every `FALLBACK_POLL_INTERVAL_S` seconds | Catches missed NOTIFYs due to service restarts or abnormal exits |

> **Design note:** NOTIFY carries only the `raw_id` signal — no data payload. The cleaner fetches the full record from the database. Migrating to Kafka in the future only requires changing the trigger entry point; all internal processing logic remains unchanged.

Both triggers feed into the same internal `asyncio.Queue`. Workers consume from a single queue regardless of how the `raw_id` arrived.

**Idempotency:** Before processing, each worker checks whether a record with the same `raw_id` already exists in `cleaned_news`. If so, the record is skipped, ensuring duplicate entries in the queue cause no data corruption.

**Concurrency:** Default 5 concurrent workers, configurable via `CLEAN_WORKER_CONCURRENCY`. Guidelines for tuning:
- Workers should not exceed 60% of the database connection pool size
- Recommended pool size: `CPU cores × 2`
- Example: 4-core server → pool size 8 → max clean workers 4

### 4.3 Cleaning Steps

| Step | Operation | On Failure |
|---|---|---|
| 1. Null check | Validate `title` and `body` are not empty | Either null → log rejection (`EMPTY_FIELD`); mark `is_deleted = true` in `raw_news`; stop processing |
| 2. `published_at` check | `published_at` is nullable; no substitution is applied by the cleaning layer. Downstream consuming layers are responsible for falling back to `created_at` when null | Does not block processing |
| 3. Title normalisation | Fullwidth-to-halfwidth (NFKC), strip excess whitespace | Normalisation failure logged as warning; does not block |
| 4. Title hash deduplication | Compute `SHA-256(normalised_title)`; if collision found, retain record with earliest `created_at` | Duplicate → log rejection (`DUPLICATE_TITLE`); mark `is_deleted = true` in `raw_news`; stop processing |
| 5. Body normalisation | Strip excess whitespace and line breaks, fullwidth-to-halfwidth | Normalisation failure logged as warning; does not block |
| 6. Body length check | After normalisation, if `len(body_cleaned) < CLEAN_BODY_MIN_LENGTH` | Too short → log rejection (`BODY_TOO_SHORT`); mark `is_deleted = true` in `raw_news`; stop processing |
| 7. Write to `cleaned_news` | Insert record with `title_cleaned`, `body_cleaned`, `created_at = now()` | On success: trigger `NOTIFY raw_news_cleaned(cleaned_id)`. On failure: rely on fallback poll — do not send NOTIFY |

> **Rejection logging:** Rejected records are marked `is_deleted = true` with `deleted_reason` in `raw_news`. No entry is written to `cleaned_news`. Rejection details are recorded in application logs only.

### 4.4 Data Model

**`cleaned_news` table** — Written by the cleaning layer. Contains only successfully cleaned records. Consumed by downstream parsing service via API.

| Field | Type | Description |
|---|---|---|
| cleaned_id | UUID | Primary key |
| raw_id | UUID | FK → `raw_news.raw_id`; index for traceability joins |
| title_cleaned | TEXT | Normalised title |
| body_cleaned | TEXT | Normalised plain text body |
| created_at | TIMESTAMPTZ | Record creation time UTC |

**Index strategy:**
- `cleaned_id` — primary key; auto-indexed
- `raw_id` — FK index; used for traceability joins to `raw_news`

### 4.5 Cleaning Data Flow

| Stage | Action | Output |
|---|---|---|
| NOTIFY received | asyncpg `LISTEN raw_news_inserted`; extract `raw_id` from payload | `raw_id` enqueued |
| Fallback poll | Every `FALLBACK_POLL_INTERVAL_S` seconds, scan `raw_news` for records with no entry in `cleaned_news`; enqueue `raw_id` | `raw_id` enqueued |
| Idempotency check | Worker dequeues `raw_id`; checks if `raw_id` exists in `cleaned_news` — skip if found | Skip or proceed |
| Execute cleaning | Run 7-step cleaning sequence (see Section 4.3) | Field updates or soft delete in `raw_news` |
| Write result | Insert into `cleaned_news`; set `created_at = now()` | `cleaned_news` record |
| Notify downstream | Trigger `NOTIFY raw_news_cleaned(cleaned_id)` on successful write only | Downstream parsing service receives signal |

---

## 5. NOTIFY Signal Specification

NOTIFY is used as a lightweight event signal between SADI's internal layers and downstream services. Payload carries only the record ID — receivers fetch full data via database query (internal) or API (external).

| Signal | Triggered When | Payload | Listener |
|---|---|---|---|
| `raw_news_inserted` | Crawler successfully writes one `raw_news` record | `raw_id` (UUID string) | SADI cleaning layer |
| `raw_news_cleaned` | Cleaner successfully writes one `cleaned_news` record | `cleaned_id` (UUID string) | Downstream parsing service |

> **Migration note:** Migrating to Kafka requires replacing only the NOTIFY sender and LISTEN receiver entry points — all downstream processing logic is unchanged.

---

## 6. API

SADI exposes a minimal REST API via FastAPI. The API serves two purposes: external trigger for crawl execution, and data access for downstream services.

### 6.1 Endpoints (MVP)

| Method | Endpoint | Description |
|---|---|---|
| POST | `/crawl` | Trigger a crawl execution; accepts optional time window parameter |
| GET | `/health` | Service health check |
| GET | `/cleaned_news/{cleaned_id}` | Return a cleaned article record by ID; consumed by downstream parsing service after receiving NOTIFY |

### 6.2 `GET /health` Response

| Field | Values | Description |
|---|---|---|
| status | healthy / degraded / unhealthy | Overall service status |
| database | ok / error | Database connectivity |
| coroutines.clean_worker | ok / error | clean_worker coroutine status |
| coroutines.notify_listener | ok / error | notify_listener coroutine status |
| coroutines.fallback_poller | ok / error | fallback_poller coroutine status |

HTTP response codes:
- All healthy → `200 healthy`
- Any coroutine degraded → `200 degraded`
- Database unreachable → `503 unhealthy`

---

## 7. Database Retry Strategy

### 7.1 Scope

Applies to all database write operations within SADI. Retry logic is handled at the asyncpg connection pool layer; business code does not implement retry logic directly.

### 7.2 Failure Classification

| Type | Examples | Action |
|---|---|---|
| Transient failure | Connection timeout, pool exhaustion, temporary network interruption | Retry with exponential backoff |
| Permanent failure | Unique constraint violation, type mismatch, permission error | Raise exception immediately; no retry |

> **Note:** Unique constraint violations (e.g. duplicate `source_url`) are expected behaviour in the crawler layer and are treated as successful idempotent writes, not errors.

### 7.3 Retry Parameters

| Parameter | Default | Description |
|---|---|---|
| DB_MAX_RETRY | 3 | Max retry attempts for database operations |
| DB_RETRY_BASE_WAIT_MS | 100 | Base wait time for exponential backoff (ms) |

Backoff formula: `wait = DB_RETRY_BASE_WAIT_MS × 2^(attempt - 1)`

| Attempt | Wait |
|---|---|
| 1 | 100ms |
| 2 | 200ms |
| 3 | 400ms |

---

## 8. Coroutine Health Management

### 8.1 Managed Coroutines

Watchdog manages only long-running coroutines. Crawl-scoped coroutines (`rss_fetcher`, `crawl_worker`) are handled at the execution level — see Section 3.5.

| Coroutine | Responsibility |
|---|---|
| `clean_worker` | Dequeues raw_ids; executes cleaning steps |
| `notify_listener` | Listens for PostgreSQL NOTIFY signals |
| `fallback_poller` | Periodically scans for unprocessed records |

### 8.2 Watchdog Mechanism

Each coroutine is monitored for two failure modes:

**Unexpected exit** — Coroutine terminates due to an unhandled exception. Watchdog detects the exit and immediately restarts the coroutine.

**Deadlock / hang** — Coroutine becomes unresponsive without raising an exception. Each coroutine has a configurable timeout threshold. If exceeded, Watchdog forcibly cancels and restarts the coroutine.

The fallback poller runs as an internal asyncio timed loop. It is monitored by the same Watchdog mechanism to handle unexpected cancellation or hang.

### 8.3 Restart Strategy

- Restart interval uses exponential backoff
- If consecutive restarts exceed `WATCHDOG_MAX_RESTART`, the coroutine is not restarted
- A critical alert is written to the application log
- Manual intervention is required to resume

### 8.4 Watchdog Configuration

| Parameter | Default | Description |
|---|---|---|
| WATCHDOG_CLEAN_WORKER_TIMEOUT_S | 30 | Hang detection threshold for `clean_worker` (seconds) |
| WATCHDOG_NOTIFY_LISTENER_TIMEOUT_S | 30 | Hang detection threshold for `notify_listener` (seconds) |
| WATCHDOG_FALLBACK_POLLER_TIMEOUT_S | 30 | Hang detection threshold for `fallback_poller` (seconds) |
| WATCHDOG_MAX_RESTART | 5 | Max consecutive restarts before halting and awaiting manual intervention |

---

## 9. Deployment

### 9.1 Container Configuration

| Service | Image | Notes |
|---|---|---|
| sadi | python:3.12-slim (custom build) | Main SADI service; includes crawler, cleaning layers and API |
| postgres | postgres:16-alpine | Primary database; persistent volume mounted |

### 9.2 Environment Variables

| Variable | Default | Description |
|---|---|---|
| DATABASE_URL | — | PostgreSQL connection string; required |
| DB_POOL_SIZE | 10 | Database connection pool size |
| DB_MAX_RETRY | 3 | Max database operation retry attempts |
| DB_RETRY_BASE_WAIT_MS | 100 | Database retry base wait time (ms) |
| CRAWL_MAX_RETRY | 3 | Max crawler retry attempts |
| CRAWL_RETRY_BASE_WAIT_MS | 500 | Crawler retry base wait time (ms) |
| CRAWL_REQUEST_TIMEOUT_S | 10 | Single page fetch timeout (seconds) |
| CLEAN_WORKER_CONCURRENCY | 5 | Number of concurrent cleaning workers |
| CLEAN_BODY_MIN_LENGTH | 50 | Minimum body length after cleaning (characters) |
| FALLBACK_POLL_INTERVAL_S | 300 | Fallback poll interval (seconds) |
| WATCHDOG_CLEAN_WORKER_TIMEOUT_S | 30 | `clean_worker` hang detection threshold (seconds) |
| WATCHDOG_NOTIFY_LISTENER_TIMEOUT_S | 30 | `notify_listener` hang detection threshold (seconds) |
| WATCHDOG_FALLBACK_POLLER_TIMEOUT_S | 30 | `fallback_poller` hang detection threshold (seconds) |
| WATCHDOG_MAX_RESTART | 5 | Max consecutive coroutine restarts before halting |

### 9.3 Project Structure

```
sadi/
├── app/
│   ├── crawler/
│   │   ├── feed_fetcher.py          # RSS parsing, fetch_rss()
│   │   ├── page_crawler.py          # HTTP fetch, crawl_with_retry()
│   │   ├── html_parser.py           # trafilatura extraction + BS4 fallback
│   │   ├── pdf_parser.py            # pdfplumber text extraction
│   │   └── crawler_service.py       # process_source(), concurrency orchestration
│   ├── cleaner/
│   │   ├── cleaning_service.py      # Cleaning layer main service, queue management
│   │   ├── notify_listener.py       # PostgreSQL LISTEN/NOTIFY handler
│   │   ├── fallback_poller.py       # Fallback poll loop
│   │   ├── text_normaliser.py       # Fullwidth conversion, whitespace cleaning
│   │   └── dedup_service.py         # Title hash deduplication logic
│   ├── watchdog/
│   │   └── watchdog.py              # Coroutine health monitoring and restart
│   ├── api/
│   │   ├── routes/
│   │   │   ├── crawl.py             # POST /crawl
│   │   │   ├── health.py            # GET /health
│   │   │   └── cleaned_news.py      # GET /cleaned_news/{cleaned_id}
│   │   └── main.py                  # FastAPI app initialisation
│   ├── db/
│   │   ├── connection.py            # asyncpg connection pool
│   │   └── notify.py                # NOTIFY sender abstraction
│   ├── models/                      # Data model definitions
│   ├── config.py                    # Environment variable loading
│   └── main.py                      # Service entry point
├── alembic/
│   └── versions/
│       ├── 001_create_tables.py     # Schema creation
│       └── 002_seed_sources.py      # Initial data source configuration
├── Dockerfile
├── requirements.txt
└── docker-compose.yml
```

---

## 10. Open Questions

| # | Question | Impact | Target |
|---|---|---|---|
| Q-1 | Validate RSS URLs and CSS selectors for Ming Pao Finance and AAStocks via real crawl test | HTML parsing accuracy | Week 1 |
| Q-2 | Validate whether `CLEAN_BODY_MIN_LENGTH = 50` is appropriate based on real data | BODY_TOO_SHORT rejection rate | Week 2 |
| Q-3 | Assess anti-crawl measures per source: User-Agent, Cookie, or proxy requirements | Crawl success rate | Week 1 |

---

*— End of Document | SADI TAD v0.5 | Pending team review before status change to APPROVED —*