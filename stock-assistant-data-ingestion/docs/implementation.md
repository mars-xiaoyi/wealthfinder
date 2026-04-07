# SADI — Implementation Guide

| Field | Detail |
|---|---|
| Service | Stock Assistant Data Ingestion (SADI) |
| Document Version | v0.1 |
| Status | DRAFT |
| Reference TAD | `stock-assistant-data-ingestion/docs/architecture/system-design.md` |
| Reference API | `docs/api.md` |

---

## Table of Contents

1. [Development Environment Setup](#1-development-environment-setup)
2. [Project Structure](#2-project-structure)
3. [Data Models](#3-data-models)
4. [Configuration — `app/config.py`](#4-configuration--appconfigpy)
5. [Database Layer — `app/db/`](#5-database-layer--appdb)
6. [Redis Layer — `app/redis/`](#6-redis-layer--appredis)
7. [API Layer — `app/api/`](#7-api-layer--appapi)
8. [Crawler Layer — `app/crawler/`](#8-crawler-layer--appcrawler)
9. [Cleaning Layer — `app/cleaner/`](#9-cleaning-layer--appcleaner)
10. [Service Entry Point — `app/main.py`](#10-service-entry-point--appmainpy)
11. [Database Migrations — `alembic/`](#11-database-migrations--alembic)
12. [Open Questions](#12-open-questions)

---

## 1. Development Environment Setup

### Prerequisites

- Python 3.12
- Docker and Docker Compose (for PostgreSQL + Redis)
- `uv` package manager (`pip install uv`)

### Steps

```bash
# 1. Clone the repo and navigate to the service directory
cd stock-assistant-data-ingestion

# 2. Create and activate virtual environment
uv venv
source .venv/bin/activate

# 3. Install dependencies
uv pip install -r requirements.txt

# 4. Install playwright browsers (required for HKEX + MingPao crawlers)
playwright install chromium

# 5. Start local dependencies
docker compose up -d postgres redis

# 6. Run database migrations
alembic upgrade head

# 7. Start the service
uvicorn app.main:app --reload --port 8000
```

### Environment Variables for Local Development

Create a `.env` file in the project root (do NOT commit this file):

```env
# Shared infrastructure
DATABASE_URL=postgresql://postgres:password@localhost:5432/sadi
DB_POOL_SIZE=10
DB_MAX_RETRY=3
DB_RETRY_BASE_WAIT_MS=100
REDIS_URL=redis://localhost:6379

# Crawler
CRAWL_MAX_RETRY=3
CRAWL_RETRY_BASE_WAIT_MS=500
CRAWL_REQUEST_TIMEOUT_S=10

# Cleaner
CLEAN_WORKER_CONCURRENCY=5
CLEAN_BODY_MIN_LENGTH=50
STREAM_CLAIM_TIMEOUT_MS=30000
```

---

## 2. Project Structure

```
sadi/
├── app/
│   ├── crawler/
│   │   ├── crawl_service.py         # CrawlService: orchestration, DB save, stream signals
│   │   ├── base_crawler.py          # Abstract base class; defines CrawlResult dataclass
│   │   ├── hkex_crawler.py          # HKEXCrawler
│   │   ├── mingpao_crawler.py       # MingPaoCrawler
│   │   ├── aastocks_crawler.py      # AAStocksCrawler
│   │   ├── yahoo_hk_crawler.py      # YahooHKCrawler
│   │   ├── browser_manager.py       # Shared playwright Browser/BrowserContext lifecycle
│   │   ├── feed_fetcher.py          # RSS feed parsing via feedparser
│   │   ├── page_crawler.py          # HTTP fetch with retry logic
│   │   ├── html_parser.py           # trafilatura + BS4 CSS selector fallback
│   │   └── pdf_parser.py            # pymupdf primary + pdfminer.six fallback
│   ├── cleaner/
│   │   ├── cleaning_service.py      # Cleaning layer: queue management + 7-step pipeline
│   │   ├── stream_handler.py        # Redis Streams consumer + producer
│   │   ├── text_normaliser.py       # NFKC normalisation, whitespace cleaning
│   │   └── dedup_service.py         # SHA-256 title hash deduplication
│   ├── api/
│   │   ├── routes/
│   │   │   ├── crawl.py             # POST /v1/crawl
│   │   │   ├── health.py            # GET /v1/health
│   │   │   └── cleaned_news.py      # GET /v1/cleaned_news/{id}, POST /v1/cleaned_news/batch
│   │   ├── schemas.py               # Pydantic request/response schemas
│   │   ├── dependencies.py          # FastAPI Depends() helpers: get_db, get_crawl_service, etc.
│   │   └── main.py                  # FastAPI app factory
│   ├── db/
│   │   └── connection.py            # asyncpg connection pool lifecycle
│   ├── redis/
│   │   └── stream_client.py         # Redis Streams abstraction (read/write/ack)
│   ├── models/
│   │   ├── raw_news.py              # RawNews dataclass
│   │   ├── cleaned_news.py          # CleanedNews dataclass
│   │   └── crawl_error_log.py       # CrawlErrorLog dataclass
│   ├── config.py                    # App config: env vars + sources.yaml loader
│   └── main.py                      # Service entry point (lifespan)
├── config/
│   └── sources.yaml                 # Per-source behaviour config
├── alembic/
│   └── versions/
│       └── 001_create_tables.py
├── Dockerfile
├── requirements.txt
└── docker-compose.yml
```

---

## 3. Data Models

> **On UUID collision:** All primary keys use `uuid.uuid4()` (random UUID v4), which has 122 bits of randomness. The probability of generating two identical UUIDs is ~1 in 5.3×10³⁶ — astronomically small even under high write throughput. No collision-handling code is needed; treat duplicates as a non-issue.

Data models are plain Python dataclasses used internally. Pydantic schemas (for API request/response validation) are kept separately in `app/api/schemas.py` — co-located with the API layer since they are tightly coupled to route contracts.

### 3.1 `app/models/raw_news.py`

```python
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from typing import Optional

@dataclass
class RawNews:
    raw_id: UUID                        # Primary key; generate with uuid.uuid4() on insert
    source_name: str                    # e.g. "HKEX", "MINGPAO", "AASTOCKS", "YAHOO_HK"
    source_url: str                     # Original article URL (unique index in DB)
    title: str                          # Original title; never modified after insert
    body: str                           # Plain text body; never modified after insert
    published_at: Optional[datetime]    # UTC; nullable if source doesn't provide it
    created_at: datetime                # UTC; set to now() on insert
    raw_hash: str                       # SHA-256 of normalised title (unique index in DB)
    extra_metadata: Optional[dict]      # e.g. {"stock_code": "00700"} for HKEX; null for others
    is_deleted: bool = False            # Soft delete flag set by cleaning layer
    deleted_reason: Optional[str] = None  # "EMPTY_FIELD" / "DUPLICATE_TITLE" / "BODY_TOO_SHORT"
```

### 3.2 `app/models/cleaned_news.py`

```python
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from typing import Optional

@dataclass
class CleanedNews:
    cleaned_id: UUID        # Primary key; generate with uuid.uuid4() on insert
    raw_id: UUID            # FK → raw_news.raw_id
    title_cleaned: str      # Normalised title (NFKC + whitespace stripped)
    body_cleaned: str       # Normalised plain text body
    created_at: datetime    # UTC; set to now() on insert
```

> **Note:** `cleaned_news` does not store `published_at`, `source_name`, or `source_url`. Those are fetched via JOIN with `raw_news` when the API serves them. See Section 7.3 for the API response assembly.

### 3.3 `app/models/crawl_error_log.py`

```python
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from typing import Optional

@dataclass
class CrawlErrorLog:
    error_id: UUID              # Primary key; generate with uuid.uuid4()
    execution_id: Optional[UUID]  # Correlation ID from POST /crawl; null if crawl triggered outside Admin context
    source_name: str            # e.g. "HKEX"
    url: str                    # Failed article URL; always present — only URL-level failures are logged here
    error_type: str             # "NETWORK" / "PARSE" / "STORAGE"
    error_code: str             # e.g. "HTTP_403", "PDF_ENCRYPTED", "TIMEOUT"
    attempt_count: int          # Total attempts made before giving up
    created_at: datetime        # UTC
```

### 3.4 `app/api/schemas.py` — Pydantic Schemas

These are the request/response shapes for the FastAPI routes. Pydantic validates incoming JSON automatically.

```python
from pydantic import BaseModel, Field, field_validator
from uuid import UUID
from typing import Optional, List
from datetime import datetime, date
from enum import Enum

# ── Enums ────────────────────────────────────────────────────────────────────

class SourceName(str, Enum):
    """
    All known source identifiers. Adding a new source requires:
    1. Adding a new member here.
    2. Implementing the corresponding Crawler class in app/crawler/.
    3. Registering it in CRAWLER_REGISTRY in app/api/routes/crawl.py.
    """
    HKEX      = "HKEX"
    MINGPAO   = "MINGPAO"
    AASTOCKS  = "AASTOCKS"
    YAHOO_HK  = "YAHOO_HK"

class CrawlStatus(str, Enum):
    ACCEPTED = "accepted"

# ── POST /v1/crawl ──────────────────────────────────────────────────────────

class CrawlRequest(BaseModel):
    execution_id: UUID
    source_name: SourceName          # Enum — invalid values rejected automatically by Pydantic
    date: Optional[date] = None      # Parsed from "YYYYMMDD" string in request body; only used by HKEX

    @field_validator("date", mode="before")
    @classmethod
    def parse_yyyymmdd(cls, v) -> Optional[date]:
        """
        Accept the API wire format "YYYYMMDD" (e.g. "20260406") and convert to a date object.
        Pydantic's default date parser expects ISO 8601 ("2026-04-06"); this validator handles
        the project-specific format defined in the API spec.
        """
        if v is None:
            return None
        if isinstance(v, str):
            try:
                return datetime.strptime(v, "%Y%m%d").date()
            except ValueError:
                raise ValueError("must be format YYYYMMDD, e.g. 20260406")
        return v  # already a date object (e.g. in tests)

class CrawlResponse(BaseModel):
    execution_id: UUID
    status: CrawlStatus = CrawlStatus.ACCEPTED

# ── GET /v1/health ────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str         # "healthy" / "degraded" / "unhealthy"
    database: str       # "ok" / "error"
    redis: str          # "ok" / "error"

# ── GET /v1/cleaned_news/{cleaned_id} and POST /v1/cleaned_news/batch ─────────

class CleanedNewsRecord(BaseModel):
    cleaned_id: UUID
    raw_id: UUID
    title_cleaned: str
    body_cleaned: str
    published_at: Optional[datetime]    # From raw_news; null if unavailable
    source_name: str                    # From raw_news
    source_url: str                     # From raw_news
    created_at: datetime                # cleaned_news.created_at

class BatchRequest(BaseModel):
    cleaned_ids: List[UUID] = Field(..., min_length=1, max_length=50)

class BatchResponse(BaseModel):
    results: List[CleanedNewsRecord]   # IDs not found are silently omitted from this list

# ── Error Response (used by all routes) ──────────────────────────────────────

class ErrorDetail(BaseModel):
    errors: List[dict]   # e.g. [{"field": "source_name", "issue": "..."}]

class ErrorResponse(BaseModel):
    error_code: str
    message: str
    detail: dict = {}
```

---

## 4. Configuration — `app/config.py`

### Purpose

Loads all environment variables and the `config/sources.yaml` file into a single `AppConfig` object. All other modules import config from here — never use `os.environ` directly in business code.

### Class: `SourceConfig`

```python
@dataclass
class SourceConfig:
    max_concurrent: int               # Max parallel crawler coroutines for this source
    request_interval_min_ms: int      # Minimum random jitter between requests (ms)
    request_interval_max_ms: int      # Maximum random jitter between requests (ms)
```

### Class: `CrawlConfig`

Config for the crawler layer. Passed to all Crawler classes and `page_crawler.py`.

```python
@dataclass
class CrawlConfig:
    max_retry: int                # Max retry attempts per URL fetch (env: CRAWL_MAX_RETRY)
    retry_base_wait_ms: int       # Base wait for exponential backoff (env: CRAWL_RETRY_BASE_WAIT_MS)
    request_timeout_s: int        # Single page fetch timeout in seconds (env: CRAWL_REQUEST_TIMEOUT_S)
    sources: dict[str, SourceConfig]  # Per-source behaviour; loaded from sources.yaml
```

### Class: `CleanConfig`

Config for the cleaning layer. Passed to `CleaningService`.

```python
@dataclass
class CleanConfig:
    worker_concurrency: int       # Number of concurrent cleaning workers (env: CLEAN_WORKER_CONCURRENCY)
    body_min_length: int          # Minimum body length after normalisation (env: CLEAN_BODY_MIN_LENGTH)
    stream_claim_timeout_ms: int  # Pending message idle time before XAUTOCLAIM redelivery (env: STREAM_CLAIM_TIMEOUT_MS)
```

### Class: `DatabaseConfig`

Config for the database connection pool and retry strategy. Passed to `app/db/connection.py`.

```python
@dataclass
class DatabaseConfig:
    url: str                      # PostgreSQL connection string (env: DATABASE_URL)
    pool_size: int                # Connection pool size (env: DB_POOL_SIZE)
    max_retry: int                # Max retry attempts for transient DB failures (env: DB_MAX_RETRY)
    retry_base_wait_ms: int       # Base wait for exponential backoff (env: DB_RETRY_BASE_WAIT_MS)
```

### Class: `AppConfig`

Top-level config object. Composed entirely of sub-configs.

```python
@dataclass
class AppConfig:
    db: DatabaseConfig
    redis_url: str                # env: REDIS_URL
    crawl: CrawlConfig
    clean: CleanConfig
```

### Function: `load_config() -> AppConfig`

```
Purpose : Read environment variables and sources.yaml; construct and return a validated AppConfig.
Called  : Once at service startup in app/main.py lifespan.
Raises  : ValueError if a required env var is missing.
```

Implementation notes:
- Use `os.environ` here only (not in business code)
- Load `sources.yaml` with `yaml.safe_load()`; construct one `SourceConfig` per source entry
- Build `DatabaseConfig`, `CrawlConfig`, and `CleanConfig` from their respective env var groups, then compose into `AppConfig`
- Store the loaded `AppConfig` as a module-level singleton so routes can import it

```python
# Usage in other modules:
from app.config import get_config
config = get_config()
```

### `config/sources.yaml`

```yaml
sources:
  HKEX:
    max_concurrent: 5           # Controls parallel PDF fetch workers (Phase 2); playwright pagination is always sequential
    request_interval_min_ms: 500
    request_interval_max_ms: 1000
  MINGPAO:
    max_concurrent: 3
    request_interval_min_ms: 500
    request_interval_max_ms: 1000
  AASTOCKS:
    max_concurrent: 3
    request_interval_min_ms: 500
    request_interval_max_ms: 1000
  YAHOO_HK:
    max_concurrent: 3
    request_interval_min_ms: 500
    request_interval_max_ms: 1000
```

---

## 5. Database Layer — `app/db/`

### 5.1 `app/db/connection.py`

#### Purpose

Exposes a `DatabaseClient` class that encapsulates the `asyncpg` connection pool and all retry logic. Business code receives a `DatabaseClient` instance and calls methods on it — pool and config are never passed around by callers.

#### Class: `DatabaseClient`

```python
class DatabaseClient:
    def __init__(self, pool: asyncpg.Pool, config: DatabaseConfig):
        self._pool = pool
        self._config = config
```

Created once in the FastAPI lifespan startup handler and stored in `app.state.db`. Injected into routes and services via FastAPI `Depends()`.

---

#### Method: `execute(query: str, *args) -> None`

```
Purpose : Execute a write query (INSERT / UPDATE) with transient-failure retry
          and exponential backoff. Use this for all writes.
Params  : query — SQL string with positional placeholders ($1, $2, ...)
          *args — query parameter values in positional order
Raises  : asyncpg.PostgresError on permanent failure, or after all retries
          exhausted on transient failure.

Retry behaviour:
  Transient (retry):  TooManyConnectionsError, ConnectionDoesNotExistError
  Permanent (no retry): UniqueViolationError, DataError, NotNullViolationError
```

Backoff formula: `wait_ms = config.retry_base_wait_ms × 2^(attempt - 1)`

```
Attempt 1 → wait 100ms
Attempt 2 → wait 200ms
Attempt 3 → wait 400ms → raise if still failing
```

> **Unique constraint violations:** `UniqueViolationError` is raised immediately (no retry) and should be caught at the call site and treated as a no-op — it means the record already exists, which is expected behaviour in the crawler layer (`ON CONFLICT DO NOTHING` handles most cases at SQL level, but double-check).

---

#### Method: `fetch_one(query: str, *args) -> Optional[asyncpg.Record]`

```
Purpose : Execute a SELECT query and return the first matching row, or None if not found.
          No retry logic — reads are safe to re-issue by the caller if needed.
Params  : query — SQL string
          *args — query parameter values
Returns : asyncpg.Record (access fields as record["field_name"]), or None
```

---

#### Method: `fetch_all(query: str, *args) -> list[asyncpg.Record]`

```
Purpose : Execute a SELECT query and return all matching rows.
          No retry logic.
Params  : query — SQL string
          *args — query parameter values
Returns : List of asyncpg.Record; empty list if no rows found
```

---

#### Function: `create_db_client(config: DatabaseConfig) -> DatabaseClient`

```
Purpose : Create the asyncpg pool and wrap it in a DatabaseClient.
          This is the only place asyncpg.create_pool() is called.
Called  : Once in the FastAPI lifespan startup handler (app/main.py).
Returns : DatabaseClient instance ready for use.
```

```python
async def create_db_client(config: DatabaseConfig) -> DatabaseClient:
    pool = await asyncpg.create_pool(
        dsn=config.url,
        min_size=2,
        max_size=config.pool_size,
    )
    return DatabaseClient(pool, config)
```

---

#### Method: `close() -> None`

```
Purpose : Gracefully close the underlying connection pool.
Called  : In the FastAPI lifespan shutdown handler.
```

---

#### Dependency injection in routes

```python
# In app/api/dependencies.py
from fastapi import Request
from app.db.connection import DatabaseClient

def get_db(request: Request) -> DatabaseClient:
    return request.app.state.db

# In route handlers:
from fastapi import Depends
async def get_cleaned_news(cleaned_id: UUID, db: DatabaseClient = Depends(get_db)):
    row = await db.fetch_one("SELECT ... WHERE cleaned_id = $1", cleaned_id)
```

---

## 6. Redis Layer — `app/redis/`

### 6.1 `app/redis/stream_client.py`

#### Purpose

Abstracts all Redis Streams operations. Business code should never call `redis-py` directly — always go through `StreamClient`.

#### Stream Name Constants

Define all stream names as module-level constants at the top of this file. Every module that needs a stream name imports from here — no magic strings anywhere else.

```python
# app/redis/stream_client.py

STREAM_RAW_NEWS_INSERTED = "stream:raw_news_inserted"  # published by CrawlService after each INSERT
STREAM_RAW_NEWS_CLEANED  = "stream:raw_news_cleaned"   # published by CleaningService after each cleaned record
STREAM_CRAWL_COMPLETED   = "stream:crawl_completed"    # published by CrawlService at end of each execution
```

| Constant | Producer | Consumer |
|---|---|---|
| `STREAM_RAW_NEWS_INSERTED` | `CrawlService` | `StreamHandler` (cleaning layer) |
| `STREAM_RAW_NEWS_CLEANED` | `CleaningService` | SAPI (NLP layer, external) |
| `STREAM_CRAWL_COMPLETED` | `CrawlService` | Orchestrator / monitoring |

#### Class: `StreamClient`

```python
class StreamClient:
    def __init__(self, client: redis.asyncio.Redis): ...
```

**Constructor:** Accepts an already-connected `redis.asyncio.Redis` instance. Do not call this directly — use `create_stream_client()` instead (see below), which creates the client and verifies connectivity.

---

#### Function: `create_stream_client(redis_url: str) -> StreamClient`

```
Purpose : Create a Redis connection pool, verify connectivity with PING, and return
          a StreamClient wrapping it. This is the only place redis.asyncio.Redis is
          instantiated.
Called  : Once in the FastAPI lifespan startup handler (app/main.py).
Raises  : ConnectionError if Redis is unreachable.
```

```python
async def create_stream_client(redis_url: str) -> StreamClient:
    client = redis.asyncio.Redis.from_url(redis_url, decode_responses=True)
    # from_url() creates a connection pool but opens no TCP connections yet.
    # PING forces an actual connection and fails fast if Redis is unreachable.
    await client.ping()
    return StreamClient(client)
```

> **Why `decode_responses=True`?** redis-py returns `bytes` by default. Setting `decode_responses=True` makes it return `str` automatically, so fields from stream messages are plain strings without manual `.decode()` calls everywhere.

#### Method: `publish(stream: str, fields: dict) -> str`

```
Purpose : Write a message to a Redis Stream.
Params  : stream — stream name, e.g. "stream:raw_news_inserted"
          fields — dict of field-value pairs, e.g. {"raw_id": str(raw_id)}
Returns : The Redis message ID (e.g. "1712345678901-0") — log this for debugging.
Note    : All values must be strings. Convert UUIDs and other types before calling.
```

#### Method: `read_group(stream, group, consumer, count, block_ms) -> list[dict]`

```
Purpose : Read messages from a stream as part of a consumer group (XREADGROUP).
Params  : stream    — stream name
          group     — consumer group name, e.g. "sadi-cleaner"
          consumer  — unique consumer name within the group, e.g. "worker-1"
          count     — max number of messages to fetch per call
          block_ms  — how long to block waiting for messages (milliseconds)
Returns : List of message dicts: [{"id": "...", "fields": {...}}, ...]
          Returns empty list if timeout elapses with no messages.
```

#### Method: `ack(stream: str, group: str, message_id: str) -> None`

```
Purpose : Acknowledge a processed message (XACK). Call this ONLY after the record
          has been successfully written to cleaned_news and the stream signal sent.
          If you ACK before writing, the message is permanently lost on failure.
Params  : stream     — stream name
          group      — consumer group name
          message_id — the message ID returned by read_group
```

#### Method: `autoclaim(stream, group, consumer, min_idle_ms, count) -> list[dict]`

```
Purpose : Reclaim messages that have been pending (unACKed) longer than min_idle_ms.
          Used by the cleaning layer to recover messages after worker crashes.
          Wraps XAUTOCLAIM command.
Params  : min_idle_ms — corresponds to STREAM_CLAIM_TIMEOUT_MS in config
Returns : Same format as read_group output.
```

#### Method: `create_group_if_not_exists(stream: str, group: str) -> None`

```
Purpose : Create a consumer group for a stream, starting from the beginning ("0").
          Safe to call on service startup — uses XGROUP CREATE with MKSTREAM flag
          and catches the BUSYGROUP error if group already exists.
Called  : In the CleaningService startup, before the consumer loop begins.
```

#### Method: `close() -> None`

```
Purpose : Close the Redis connection. Called in the service shutdown handler.
```

---

## 7. API Layer — `app/api/`

### 7.1 `app/api/main.py` — FastAPI App Factory

#### Function: `create_app() -> FastAPI`

```
Purpose : Create and return the configured FastAPI application instance with all
          routers registered and exception handlers attached.
          Note: the uvicorn entry point is "app.main:app" (Section 10), not this module.
          This module builds the FastAPI instance; app/main.py attaches the lifespan.
```

```python
def create_app() -> FastAPI:
    app = FastAPI(title="SADI", version="1.0.0")
    app.include_router(crawl_router, prefix="/v1")
    app.include_router(health_router, prefix="/v1")
    app.include_router(cleaned_news_router, prefix="/v1")
    return app
```

**Exception handlers to register:**

| Exception | Response |
|---|---|
| `RequestValidationError` (Pydantic) | HTTP 400, `error_code: "COMMON-4001"`, populate `detail.errors` from Pydantic's error list |
| `JSONDecodeError` | HTTP 400, `error_code: "COMMON-4000"` |
| General `Exception` | HTTP 500, `error_code: "COMMON-5000"` |

### 7.2 `app/api/routes/crawl.py` — `POST /v1/crawl`

#### Flowchart

```mermaid
flowchart TD
    A([POST /v1/crawl]) --> B[Pydantic validates CrawlRequest\nSourceName enum rejects unknown source_name]
    B -->|Validation error| BE([HTTP 400 COMMON-4001])
    B -->|Valid| C{Check DB + Redis connectivity}
    C -->|Unavailable| CE([HTTP 503 COMMON-5001])
    C -->|OK| F[asyncio.create_task:\ncrawl_service.execute]
    F --> G([HTTP 202 CrawlResponse])
    G -.->|Background| H[CrawlService.execute runs async]
    H --> I[stream:crawl_completed published]
```

#### Handler: `post_crawl(request: CrawlRequest) -> CrawlResponse`

```
Purpose : Validate the crawl request, launch the crawler as a background asyncio Task,
          and immediately return HTTP 202 Accepted.
          The crawl runs asynchronously — the HTTP response is sent before the crawl finishes.
Returns : HTTP 202 with CrawlResponse (execution_id echoed, status="accepted")
Errors  : HTTP 400 if source_name unknown or validation fails
          HTTP 503 if DB or Redis unavailable (check connectivity before accepting)
```

Implementation notes:
- Use `asyncio.create_task()` to fire `crawl_service.execute(...)` in the background
- Do NOT `await` the task — return HTTP 202 immediately
- Attach an `add_done_callback` to the task to log any unhandled exceptions that escape `CrawlService`
- `source_name` validation is handled by Pydantic (`SourceName` enum) before the handler is called — no manual registry lookup needed here
- `CrawlService` is injected via `Depends(get_crawl_service)` from `app/api/dependencies.py`

### 7.3 `app/api/routes/cleaned_news.py`

#### Handler: `get_cleaned_news(cleaned_id: UUID) -> CleanedNewsRecord`

```
Purpose : Fetch a single cleaned article by primary key. Returns data assembled
          from a JOIN between cleaned_news and raw_news.
Returns : HTTP 200 CleanedNewsRecord
Errors  : HTTP 404 COMMON-4004 if not found
          HTTP 503 COMMON-5001 if DB unavailable
```

SQL to use:
```sql
SELECT
    cn.cleaned_id,
    cn.raw_id,
    cn.title_cleaned,
    cn.body_cleaned,
    cn.created_at,
    rn.published_at,
    rn.source_name,
    rn.source_url
FROM cleaned_news cn
JOIN raw_news rn ON cn.raw_id = rn.raw_id
WHERE cn.cleaned_id = $1
```

#### Handler: `post_cleaned_news_batch(request: BatchRequest) -> BatchResponse`

```
Purpose : Fetch multiple cleaned articles by ID list. IDs not found are silently
          omitted from the results array — no error is raised for missing IDs.
Params  : request.cleaned_ids — list of 1–50 UUIDs (validated by Pydantic)
Returns : HTTP 200 BatchResponse with results array (only found records)
Errors  : HTTP 400 COMMON-4001 if validation fails
          HTTP 503 COMMON-5001 if DB unavailable
```

Implementation notes:
- Fetch all records in a single SQL query using `WHERE cn.cleaned_id = ANY($1::uuid[])`
- Do NOT make N separate queries (one per ID) — that would be very slow

### 7.4 `app/api/routes/health.py`

#### Handler: `get_health() -> HealthResponse`

```
Purpose : Check DB and Redis connectivity. Returns overall health status.
Returns : HTTP 200 if healthy or degraded
          HTTP 503 if unhealthy (DB or Redis unreachable)
```

#### Flowchart

```mermaid
flowchart TD
    A([GET /v1/health]) --> B[Ping DB: SELECT 1]
    A --> C[Ping Redis: PING]
    B -->|OK| D[database = ok]
    B -->|Error| E[database = error]
    C -->|OK| F[redis = ok]
    C -->|Error| G[redis = error]
    D & E & F & G --> H{Any error?}
    H -->|No| I([HTTP 200 status=healthy])
    H -->|DB or Redis error| J([HTTP 503 status=unhealthy])
```

Health status rules:
- `database=ok` AND `redis=ok` → `status=healthy`, HTTP 200
- `database=error` OR `redis=error` → `status=unhealthy`, HTTP 503

> There is no `degraded` state for SADI in MVP (unlike SAPI which has layer-level health).

---

## 8. Crawler Layer — `app/crawler/`

### 8.1 `app/crawler/base_crawler.py` — Abstract Base Class and CrawlResult

#### Dataclasses: `CrawlSuccessItem`, `CrawlFailItem`, `CrawlResult`

`CrawlResult` is the single return type from every crawler's `run()`. It contains two lists — successes and failures — so `CrawlService` can handle both without unpacking a tuple.

```python
@dataclass
class CrawlSuccessItem:
    title: str
    body: str                           # Plain text; guaranteed non-empty by the crawler
    source_url: str                     # Original article URL; used as dedup key
    published_at: Optional[datetime]    # UTC; None if source does not provide it
    extra_metadata: Optional[dict] = None
    # extra_metadata is only populated by HKEXCrawler: {"stock_code": "00700"}
    # All other crawlers leave it None.

@dataclass
class CrawlFailItem:
    source_url: str   # URL that failed — always present; RSS-level failures are never CrawlFailItems
    error_type: str   # "NETWORK" / "PARSE"
    error_code: str       # e.g. "HTTP_403", "PDF_ENCRYPTED", "TIMEOUT"
    attempt_count: int    # Total attempts made before giving up

@dataclass
class CrawlResult:
    successes: list[CrawlSuccessItem]
    failures: list[CrawlFailItem]
```

#### Class: `BaseCrawler`

All four crawler classes extend `BaseCrawler`. Crawlers are responsible only for **fetching and parsing** — they do not touch the database or Redis.

```python
from abc import ABC, abstractmethod
from typing import Optional
from datetime import date
from app.config import SourceConfig
from app.crawler.page_crawler import PageCrawler
from app.crawler.browser_manager import BrowserManager

class BaseCrawler(ABC):
    def __init__(
        self,
        source_config: SourceConfig,
        page_crawler: PageCrawler,
        crawl_date: Optional[date] = None,   # only HKEXCrawler uses this
    ):
        self.source_config = source_config
        self.page_crawler = page_crawler
        self.crawl_date = crawl_date

    @abstractmethod
    async def run(self) -> CrawlResult:
        """
        Fetch and parse all articles for this source.
        Returns a CrawlResult containing:
          - successes: one CrawlSuccessItem per successfully parsed article
          - failures:  one CrawlFailItem per article that failed after all retries exhausted;
                       CrawlService writes these to crawl_error_log immediately — no further retry.
        Raises CrawlFatalError on unrecoverable source-level failure (e.g. RSS unreachable
        after all retries) — CrawlService catches this and publishes crawl_completed FAILED.
        """
        ...
```

> **Key contract:** `run()` only fetches and parses. It never writes to the database or publishes to Redis — that is `CrawlService`'s responsibility. Crawlers that need a browser (`HKEXCrawler`, `MingPaoCrawler`) manage the `BrowserManager` lifecycle entirely inside their own `run()` — `BaseCrawler` and `CrawlService` have no awareness of it.

---

### 8.2 `app/crawler/crawl_service.py` — CrawlService

Orchestrates the full crawl execution: selects the crawler, runs it, saves results, and publishes stream signals.

#### Class: `CrawlService`

```python
class CrawlService:
    CRAWLER_REGISTRY: dict[SourceName, type[BaseCrawler]] = {
        SourceName.HKEX:     HKEXCrawler,
        SourceName.MINGPAO:  MingPaoCrawler,
        SourceName.AASTOCKS: AAStocksCrawler,
        # YahooHKCrawler is NOT in the registry — it needs extra constructor args.
        # Use _create_crawler() which handles this case explicitly.
    }

    def __init__(
        self,
        db: DatabaseClient,
        stream_client: StreamClient,
        page_crawler: PageCrawler,
        config: CrawlConfig,      # needed to look up per-source SourceConfig
    ): ...
```

#### Method: `_create_crawler(source_name, crawl_date) -> BaseCrawler`

```
Purpose : Instantiate the correct crawler for the given source.
          Centralises constructor wiring — callers just pass source_name + crawl_date.
          YahooHKCrawler is the only crawler that needs db; handled here so execute()
          doesn't need to know about it.
```

```python
def _create_crawler(
    self,
    source_name: SourceName,
    crawl_date: Optional[date],
) -> BaseCrawler:
    source_config = self._config.sources[source_name.value]
    if source_name == SourceName.YAHOO_HK:
        return YahooHKCrawler(source_config, self._page_crawler, crawl_date, self._db)
    crawler_cls = self.CRAWLER_REGISTRY[source_name]
    return crawler_cls(source_config, self._page_crawler, crawl_date)
```

#### Method: `execute(execution_id, source_name, crawl_date) -> None`

```
Purpose : Run one full crawl execution end-to-end.
          Called as a background asyncio.Task from POST /v1/crawl.
          Never raises — all outcomes are signalled via stream:crawl_completed.
Params  : execution_id — UUID echoed from POST /crawl request; used in stream signals
          source_name  — SourceName enum value
          crawl_date   — date for HKEX batch pull; None for all other sources
```

#### Flowchart

```mermaid
flowchart TD
    A([execute called]) --> B[_create_crawler source_name crawl_date]
    B --> C[crawler.run\nBrowserManager lifecycle managed inside run if needed]
    C -->|CrawlFatalError| D[log error]
    D --> E[publish crawl_completed FAILED]
    C -->|CrawlResult| F[For each item in result.failures:\nwrite crawl_error_log immediately\nno retry — retries already exhausted]
    F --> G[For each item in result.successes:\nbuild RawNews\ndb.execute INSERT ON CONFLICT DO NOTHING\npublish stream:raw_news_inserted]
    G --> H[publish crawl_completed SUCCESS]
    E --> I([done])
    H --> I
```

Implementation notes:
- `BrowserManager` is created and torn down inside `HKEXCrawler.run()` and `MingPaoCrawler.run()` — `CrawlService` has no knowledge of it. If browser startup time becomes a bottleneck across many executions, `BrowserManager` can be promoted to a service-level singleton in `app.state`
- `crawler.run()` returns `CrawlResult` — failures are articles that exhausted all retries inside `PageCrawler`; `CrawlService` writes them to `crawl_error_log` immediately with no further retry
- Build `RawNews` from each `CrawlResult`: generate `raw_id = uuid4()`, set `source_name`, compute `raw_hash = compute_title_hash(normalise(title))`, set `created_at = datetime.utcnow()`
- `UniqueViolationError` on insert = duplicate record; treat as no-op, do not publish `stream:raw_news_inserted` for that record
- `stream:crawl_completed` is always published — even if zero records were saved (e.g. all duplicates or all errors)

### 8.3 `app/crawler/browser_manager.py` — Playwright Browser Lifecycle

HKEX and MingPao both use playwright. They share one `Browser` process but use separate `BrowserContext` instances.

#### Class: `BrowserManager`

```python
class BrowserManager:
    """
    Manages a single shared playwright Browser instance.
    Each crawler that needs a browser calls acquire_context() to get
    a fresh BrowserContext; it must call release_context() when done.
    """
    def __init__(self): ...

    async def start(self) -> None:
        """Launch the playwright Chromium browser. Call once at crawl execution start."""

    async def stop(self) -> None:
        """Close the browser and all open contexts. Call once at crawl execution end."""

    async def acquire_context(self) -> BrowserContext:
        """
        Create and return a fresh BrowserContext (no cookie carryover).
        Each call creates a new context — contexts are NOT shared between sources.
        """

    async def release_context(self, context: BrowserContext) -> None:
        """Close the context. Always call in a finally block."""
```

Usage pattern (enforced in each playwright crawler):
```python
context = await browser_manager.acquire_context()
try:
    page = await context.new_page()
    # ... crawl ...
finally:
    await browser_manager.release_context(context)
```

> **Why shared browser?** Starting a playwright Chromium instance takes ~1–2 seconds. HKEX and MingPao may run concurrently, so sharing avoids the overhead of two startup sequences.

### 8.4 `app/crawler/feed_fetcher.py` — RSS Feed Parsing

#### Function: `fetch_rss(url: str) -> list[FeedEntry]`

```
Purpose : Fetch and parse an RSS feed. Returns a list of structured entries.
          Uses feedparser — synchronous library wrapped in asyncio.to_thread().
Params  : url — full RSS URL
Returns : List of FeedEntry dataclasses (title, url, published_at)
Raises  : FeedFetchError if feedparser fails or returns an empty/invalid feed
          (caller should retry per crawl retry config)
```

#### Dataclass: `FeedEntry`

```python
@dataclass
class FeedEntry:
    title: str
    url: str
    published_at: Optional[datetime]  # UTC; None if not in feed
```

Implementation notes:
- feedparser is synchronous; wrap with `await asyncio.to_thread(feedparser.parse, url)`
- Parse `published` from `entry.published_parsed` (a `time.struct_time`); convert to UTC `datetime` using `calendar.timegm()`
- If `published_parsed` is None, set `published_at = None` (do not default to now)

### 8.5 `app/crawler/page_crawler.py` — HTTP Fetch with Retry

#### Class: `PageCrawler`

```python
class PageCrawler:
    def __init__(self, client: httpx.AsyncClient, config: CrawlConfig, db: DatabaseClient): ...
```

Wraps the shared `httpx.AsyncClient` with pre-fetch error log check and retry logic. Does not own the client lifecycle — the client is created at service startup and shared across all crawlers (see Section 10).

#### Method: `fetch(url: str) -> httpx.Response`

```
Purpose : Check crawl_error_log first, then fetch the URL with exponential backoff retry.
Params  : url — target URL
Returns : httpx.Response on success
Raises  : CrawlSkippedError if URL is already in crawl_error_log — caller silently skips,
          does NOT add to result.failures (already recorded in a previous execution)
          CrawlNetworkError after all retries exhausted — caller adds to result.failures
          CrawlBlockedError immediately on HTTP 403 or 404 — caller adds to result.failures
```

#### Flowchart

```mermaid
flowchart TD
    A([fetch called]) --> B{URL in crawl_error_log?}
    B -->|Yes| C([raise CrawlSkippedError])
    B -->|No| D[attempt = 1]
    D --> E[httpx GET with timeout]
    E -->|HTTP 403 or 404| F([raise CrawlBlockedError immediately])
    E -->|HTTP 500 or timeout| G{attempt < MAX_RETRY?}
    G -->|Yes| H[wait: base × 2^attempt-1\nincrement attempt]
    H --> E
    G -->|No| I([raise CrawlNetworkError])
    E -->|HTTP 200| J([return response])
```

Implementation notes:
- Error log check: `SELECT EXISTS(SELECT 1 FROM crawl_error_log WHERE url = $1)` — use `db.fetch_one()`
- Retry config (`max_retry`, `retry_base_wait_ms`) comes from `self._config` — not passed per call
- Add random jitter between requests: `await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)`; use `self._config.sources[source_name]` interval values in the Crawler, not inside `fetch()`
- Backoff formula: `wait_ms = config.retry_base_wait_ms × 2^(attempt - 1)`

### 8.6 `app/crawler/html_parser.py` — HTML Body Extraction

#### Function: `extract_body_trafilatura(html: str) -> Optional[str]`

```
Purpose : Extract the main article body from HTML using trafilatura auto-extraction.
Params  : html — raw HTML string
Returns : Extracted plain text, or None if trafilatura returns empty/None
```

#### Function: `extract_body_css(html: str, selector: str) -> Optional[str]`

```
Purpose : Extract text from HTML using a CSS selector (BS4 fallback).
          Use when trafilatura quality is insufficient or a specific element is needed.
Params  : html     — raw HTML string
          selector — CSS selector string, e.g. "[class*='newscon']"
Returns : Extracted plain text from the matched element, or None if selector doesn't match
```

Implementation notes:
- `extract_body_css` uses BeautifulSoup4: `soup.select_one(selector).get_text(separator=" ", strip=True)`
- Always try `extract_body_trafilatura` first where applicable; only fall back to CSS selector if needed

### 8.7 `app/crawler/pdf_parser.py` — PDF Text Extraction

#### Function: `parse_pdf(content: bytes) -> str`

```
Purpose : Extract plain text from a PDF binary. Tries pymupdf first; falls back
          to pdfminer.six if pymupdf returns empty text (complex layout PDFs).
Params  : content — raw PDF bytes (from httpx response.content)
Returns : Extracted plain text string
Raises  : PdfEncryptedError if PDF is encrypted (log and discard — non-retryable)
          PdfParseError if both parsers fail on a non-encrypted PDF (retry parsing only)
```

#### Flowchart

```mermaid
flowchart TD
    A([parse_pdf bytes]) --> B[Open with pymupdf fitz.open]
    B -->|metadata.encrypted = True| C([raise PdfEncryptedError])
    B -->|OK| D[Extract text from all pages with page.get_text]
    D -->|text not empty| E([return text])
    D -->|text empty| F[Try pdfminer.six: extract_text]
    F -->|text not empty| G([return text])
    F -->|text empty or error| H([raise PdfParseError])
```

Implementation notes:
- pymupdf: `doc = fitz.open(stream=content, filetype="pdf")` then iterate `doc.pages()`
- Check `doc.is_encrypted` before extracting
- pdfminer.six: use `pdfminer.high_level.extract_text(BytesIO(content))`
- Both parsers are synchronous — wrap with `asyncio.to_thread()`

### 8.8 `app/crawler/hkex_crawler.py` — HKEXCrawler

#### Class: `HKEXCrawler(BaseCrawler)`

The HKEX crawl has two distinct phases:
- **Phase 1 — Link collection** (sequential): A single playwright session loads `titlesearch.xhtml` and clicks LOAD MORE until all rows are visible. This is inherently sequential — one browser session, one page.
- **Phase 2 — PDF fetching** (parallel): Each PDF is an independent httpx GET with no shared session state. Fetched concurrently using `asyncio.Semaphore(source_config.max_concurrent)`.

> `max_concurrent` in the HKEX `SourceConfig` controls Phase 2 PDF fetch concurrency only. Phase 1 playwright pagination is always sequential by nature and ignores this value.

#### Method: `run() -> CrawlResult`

```
Purpose : Phase 1 — collect all PDF links via playwright. Phase 2 — fetch and parse
          PDFs in parallel. Return results with successes and failures.
Returns : CrawlResult with successes and failures populated
Raises  : CrawlFatalError if playwright fails to load the search page after retries
```

#### Flowchart

```mermaid
flowchart TD
    A([run]) --> START[BrowserManager.start]
    START --> B[Phase 1: playwright GET titlesearch.xhtml]
    B --> C[waitUntil=domcontentloaded]
    C --> D[Read .component-loadmore-leftPart__container text]
    D --> E{Showing N of N?}
    E -->|No| F[Click a.component-loadmore__link\nwait domcontentloaded]
    F --> D
    E -->|Yes| H[Extract all rows: table tbody tr\nFor each: PDF link, headline, stock_code, release_time]
    H --> I[Phase 2: Create asyncio.Queue + Semaphore max_concurrent]
    I --> J[Launch PDF fetch workers]
    J --> K{Queue empty?}
    K -->|No| L[Dequeue PDF URL + metadata]
    L --> M[Acquire semaphore\npage_crawler.fetch PDF URL]
    M -->|CrawlSkippedError| SKIP[Release semaphore\nsilently skip]
    M --> N[parse_pdf bytes]
    N -->|PdfEncryptedError\nor PdfParseError after retries| O[Build CrawlFailItem\nAppend to result.failures\nRelease semaphore]
    N -->|OK| P[Build CrawlSuccessItem\nextra_metadata=stock_code\nAppend to result.successes\nRelease semaphore]
    O & P --> K
    K -->|Done| S[BrowserManager.stop]
    S --> T([return CrawlResult])
```

#### Helper: `_parse_release_time(raw: str) -> datetime`

```
Purpose : Parse HKEX release time string to UTC datetime.
Input   : "02/04/2026 22:59" (format: DD/MM/YYYY HH:mm, HKT timezone)
Returns : UTC datetime (subtract 8 hours from HKT)
```

#### Helper: `_parse_pagination_counts(text: str) -> tuple[int, int]`

```
Purpose : Extract (showing_count, total_count) from the pagination label.
Input   : "Showing 1471 of 1471 records"
Returns : (1471, 1471)
Raises  : ValueError if regex match fails (indicates page structure changed — log warning)
```

### 8.9 `app/crawler/mingpao_crawler.py` — MingPaoCrawler

MingPao uses a producer-consumer model: RSS feed discovery → playwright browser fetch.

#### Class: `MingPaoCrawler(BaseCrawler)`

#### Method: `run() -> CrawlResult`

```
Purpose : Fetch RSS, discover article URLs, crawl bodies via playwright, return results.
Returns : CrawlResult with successes and failures populated
Raises  : CrawlFatalError if RSS fetch fails after all retries
```

#### Flowchart

```mermaid
flowchart TD
    A([run]) --> START[BrowserManager.start]
    START --> B[fetch_rss RSS_URL]
    B -->|FeedFetchError after retries| C[BrowserManager.stop]
    C --> C2([raise CrawlFatalError])
    B -->|OK ~26 entries| D[Create asyncio.Queue\nEnqueue all article URLs]
    D --> E[Create asyncio.Semaphore max_concurrent=3]
    E --> F[Launch N crawl_worker coroutines]
    F --> G{Queue empty?}
    G -->|No| H[Dequeue URL + RSS entry]
    H --> I[Acquire semaphore]
    I --> J[page_crawler pre-check\nURL in crawl_error_log?]
    J -->|CrawlSkippedError| SKIP[Release semaphore\nsilently skip]
    J -->|OK| JJ[playwright GET article URL\nwaitUntil=domcontentloaded]
    JJ --> K[Extract article selector]
    K -->|Body empty after retries| L[Build CrawlFailItem\nAppend to result.failures\nRelease semaphore]
    K -->|OK| M{published_at from RSS?}
    M -->|No| N[Extract from page time element\nregex parse]
    M -->|Yes| O[Use RSS published_at UTC]
    N --> O
    O --> P[Build CrawlSuccessItem\nAppend to result.successes\nRelease semaphore]
    L & P --> G
    G -->|Empty + all workers done| S[BrowserManager.stop]
    S --> T([return CrawlResult])
```

#### Helper: `_extract_published_at_from_page(page_html: str) -> Optional[datetime]`

```
Purpose : Fallback published_at extraction from article HTML when RSS field is missing.
          Searches for a <time> element and parses its innerText.
Input   : Raw HTML string of the article page
Pattern : r"\d{4}年\d{1,2}月\d{1,2}日.*?\d{1,2}:\d{2}[AP]M"
Returns : UTC datetime, or None if not found
```

### 8.10 `app/crawler/aastocks_crawler.py` — AAStocksCrawler

#### Class: `AAStocksCrawler(BaseCrawler)`

#### Method: `run() -> CrawlResult`

```
Purpose : Fetch list page, discover up to 9 article URLs, crawl bodies via httpx, return results.
Returns : CrawlResult with successes and failures populated
Raises  : CrawlFatalError if the list page fetch fails after all retries
```

#### Flowchart

```mermaid
flowchart TD
    A([run]) --> B[page_crawler.fetch list page /tc/]
    B -->|CrawlNetworkError after retries| C([raise CrawlFatalError])
    B -->|OK| D[CSS selector a href aafn-con/NOW.\nExtract up to 9 URLs]
    D --> E[Create asyncio.Queue + Semaphore max_concurrent=3]
    E --> F[Launch crawl_worker coroutines]
    F --> G{Queue empty?}
    G -->|No| H[Dequeue URL]
    H --> I[page_crawler.fetch article URL /tc/]
    I -->|CrawlSkippedError| SKIP[Release semaphore\nsilently skip]
    I -->|CrawlBlockedError 403/404\nor CrawlNetworkError after retries| J[Build CrawlFailItem\nAppend to result.failures\nRelease semaphore]
    I -->|OK| K[CSS selector newscon\nExtract body]
    K -->|Empty| J
    K -->|OK| M[Regex extract published_at\nfrom page text\nConvert HKT → UTC]
    M --> N[Build CrawlSuccessItem\nAppend to result.successes\nRelease semaphore]
    J & N --> G
    G -->|Done| R([return CrawlResult])
```

#### Helper: `_parse_published_at(page_html: str) -> Optional[datetime]`

```
Purpose : Extract published_at from AAStocks article page visible text.
Pattern : r"\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}"  e.g. "2026/04/06 01:27"
Returns : UTC datetime (convert from HKT by subtracting 8 hours), or None if not found
```

### 8.11 `app/crawler/yahoo_hk_crawler.py` — YahooHKCrawler

#### Class: `YahooHKCrawler(BaseCrawler)`

`YahooHKCrawler` is the only crawler that needs DB read access (for coverage gap detection). Its constructor takes one extra parameter compared to other crawlers:

```python
class YahooHKCrawler(BaseCrawler):
    def __init__(
        self,
        source_config: SourceConfig,
        page_crawler: PageCrawler,
        crawl_date: Optional[date],
        db: DatabaseClient,         # extra — for _get_previous_crawl_time()
    ): ...
```

`CrawlService._create_crawler()` handles this special case — callers never construct `YahooHKCrawler` directly.

#### Method: `run() -> CrawlResult`

```
Purpose : Fetch RSS, filter ad URLs, crawl articles via httpx + trafilatura, return results.
          Includes coverage gap detection.
Returns : CrawlResult with successes and failures populated
Raises  : CrawlFatalError if RSS fetch fails after all retries
```

#### Flowchart

```mermaid
flowchart TD
    A([run]) --> B[fetch_rss RSS_URL]
    B -->|FeedFetchError after retries| C([raise CrawlFatalError])
    B -->|OK| D[Filter entries: keep URL contains hk.finance.yahoo.com/news/]
    D --> E{Oldest entry published_at\n> _get_previous_crawl_time?}
    E -->|Yes| F[Log WARNING: potential coverage gap\nProceed anyway]
    E -->|No| G[Normal]
    F & G --> H[Create Queue + Semaphore max_concurrent=3]
    H --> I[crawl_worker: page_crawler.fetch article]
    I -->|CrawlSkippedError| SKIP[Release semaphore\nsilently skip]
    I -->|CrawlNetworkError after retries| J[Build CrawlFailItem\nAppend to result.failures\nRelease semaphore]
    I -->|OK| K[trafilatura extract body]
    K -->|Empty| J
    K -->|OK| L[Build CrawlSuccessItem\npublished_at from RSS UTC\nAppend to result.successes\nRelease semaphore]
    J & L --> N{More URLs?}
    N -->|Yes| I
    N -->|No| O([return CrawlResult])
```

> **Coverage gap:** Yahoo RSS only returns 5 entries. If the crawl runs too infrequently, entries may drop off the feed before being captured. This is a scheduling concern handled by Admin Service — SADI only logs the warning.

#### Helper: `_get_previous_crawl_time() -> Optional[datetime]`

```
Purpose : Look up the most recent raw_news record time for YAHOO_HK.
          Used for coverage gap detection. See Section 8.12 for detail.
```

### 8.12 `YahooHKCrawler` — DB helper

#### Helper: `_get_previous_crawl_time(db: DatabaseClient) -> Optional[datetime]`

```
Purpose : Look up the most recent raw_news record time for YAHOO_HK.
          Used for coverage gap detection inside YahooHKCrawler.run().
          Note: db is available via self — this is an instance method, not a standalone function.
Query   : SELECT MAX(created_at) FROM raw_news WHERE source_name = 'YAHOO_HK'
Returns : UTC datetime of most recent record, or None if no records exist
```

> **Note:** `YahooHKCrawler` is the only crawler that needs DB read access — for the coverage gap check. Pass `db: DatabaseClient` into its constructor only.

---

### 8.13 `CrawlService` — Persistence Logic Detail

The SQL and stream signals that `CrawlService.execute()` uses when saving each `CrawlResult`:

**Insert SQL:**
```sql
INSERT INTO raw_news (
    raw_id, source_name, source_url, title, body,
    published_at, created_at, raw_hash, extra_metadata
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (source_url) DO NOTHING
```

> `raw_hash` has its own unique constraint — if a title duplicate slips through `source_url` dedup, the DB will reject it. Catch `UniqueViolationError` and treat as no-op.

**Stream signal per saved record:**
```python
from app.redis.stream_client import STREAM_RAW_NEWS_INSERTED, STREAM_CRAWL_COMPLETED

await stream_client.publish(
    STREAM_RAW_NEWS_INSERTED,
    {"raw_id": str(raw_id)}
)
```

**`crawl_completed` signal — published once per execution:**
```python
# On success (all records processed — even if count is zero):
await stream_client.publish(
    STREAM_CRAWL_COMPLETED,
    {
        "execution_id": str(execution_id),
        "status": "SUCCESS",
        "error_detail": "",        # empty string; Admin side checks status field
    }
)

# On CrawlFatalError:
await stream_client.publish(
    STREAM_CRAWL_COMPLETED,
    {
        "execution_id": str(execution_id),
        "status": "FAILED",
        "error_detail": str(e),    # exception message for Admin complete_with_error()
    }
)
```

> **Field spec from Admin TAD §4:** Consumer group is `admin-scheduler`. Fields are `execution_id`, `status` (`SUCCESS`/`FAILED`), `error_detail`. Admin `CrawlHandler.execute()` filters by `execution_id` to match the right message.

**`save_crawl_error` — best-effort audit log:**
```
Purpose : Insert a CrawlErrorLog record for per-article failures encountered during run().
          Crawlers raise per-article errors; CrawlService catches and logs them here.
          Best-effort — if this insert fails, log to application logger and swallow.
```

---

## 9. Cleaning Layer — `app/cleaner/`

### 9.1 `app/cleaner/text_normaliser.py`

#### Function: `normalise(text: str) -> str`

```
Purpose : Apply NFKC normalisation and whitespace cleaning to a string.
          Converts fullwidth characters to halfwidth, strips leading/trailing whitespace,
          and collapses internal sequences of whitespace/newlines to a single space.
Params  : text — input string (title or body)
Returns : Normalised string
Note    : If normalisation raises an unexpected exception, log a WARNING and return
          the original text unchanged — do not block the pipeline.
```

Implementation:
```python
import unicodedata, re

def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
```

### 9.2 `app/cleaner/dedup_service.py`

#### Function: `compute_title_hash(normalised_title: str) -> str`

```
Purpose : Compute SHA-256 hash of a normalised title for deduplication.
Params  : normalised_title — already NFKC-normalised title string
Returns : Hex-encoded SHA-256 hash string (64 characters)
```

```python
import hashlib

def compute_title_hash(normalised_title: str) -> str:
    return hashlib.sha256(normalised_title.encode("utf-8")).hexdigest()
```

#### Function: `is_duplicate(raw_hash: str, db: DatabaseClient) -> bool`

```
Purpose : Check if a raw_hash already exists in raw_news.
          Used by the cleaning layer to detect cross-source title duplicates.
Params  : raw_hash — SHA-256 hex string
          db       — DatabaseClient instance
Returns : True if a record with this hash already exists (and has is_deleted=False)
Query   : SELECT EXISTS(SELECT 1 FROM raw_news WHERE raw_hash = $1 AND is_deleted = FALSE)
```

### 9.3 `app/cleaner/stream_handler.py`

#### Class: `StreamHandler`

Wraps the `StreamClient` with cleaning-layer-specific logic.

```python
from app.redis.stream_client import STREAM_RAW_NEWS_INSERTED, STREAM_RAW_NEWS_CLEANED

class StreamHandler:
    STREAM_IN  = STREAM_RAW_NEWS_INSERTED
    STREAM_OUT = STREAM_RAW_NEWS_CLEANED
    GROUP_NAME = "sadi-cleaner"
    CONSUMER_NAME = "sadi-cleaner-1"   # One consumer in MVP; expand for horizontal scaling

    def __init__(self, stream_client: StreamClient): ...
```

#### Method: `ensure_consumer_group() -> None`

```
Purpose : Create the consumer group if it doesn't exist. Call once at startup.
          Uses create_group_if_not_exists() from StreamClient.
```

#### Method: `read_messages(count: int = 10) -> list[dict]`

```
Purpose : Read up to `count` new messages from stream:raw_news_inserted.
          Blocks up to 5000ms (5s) if no new messages.
Returns : List of message dicts with keys "id" and "fields" (containing "raw_id")
```

#### Method: `reclaim_pending(min_idle_ms: int) -> list[dict]`

```
Purpose : Reclaim messages that have been pending longer than min_idle_ms.
          Called periodically (every N seconds) to recover from worker crashes.
Params  : min_idle_ms — from config.stream_claim_timeout_ms (default 30000)
```

#### Method: `ack(message_id: str) -> None`

```
Purpose : Acknowledge a successfully processed message.
          Call ONLY after cleaned_news record is written AND stream:raw_news_cleaned is published.
```

#### Method: `publish_cleaned(cleaned_id: UUID) -> None`

```
Purpose : Publish to stream:raw_news_cleaned after a record is successfully cleaned.
          SAPI NLP layer consumes this stream.
```

### 9.4 `app/cleaner/cleaning_service.py` — Main Cleaning Loop

#### Class: `CleaningService`

The heart of the cleaning layer. Runs the consumer loop and orchestrates the 7-step pipeline.

```python
class CleaningService:
    def __init__(
        self,
        db: DatabaseClient,
        stream_handler: StreamHandler,
        config: CleanConfig,
    ): ...
```

#### Method: `start() -> None`

```
Purpose : Entry point for the cleaning layer. Creates the internal asyncio.Queue,
          starts the stream reader coroutine, and launches CLEAN_WORKER_CONCURRENCY
          worker coroutines. All run concurrently via asyncio.gather().
Called  : In app/main.py lifespan, as an asyncio.Task (background task).
```

#### Flowchart — Consumer Loop

```mermaid
flowchart TD
    A([CleaningService.start]) --> B[ensure_consumer_group]
    B --> C[Create asyncio.Queue internal_queue]
    C --> D[asyncio.gather:\n- stream_reader coroutine\n- N worker coroutines\n- reclaim_loop coroutine]

    subgraph stream_reader
        SR1([loop forever]) --> SR2[StreamHandler.read_messages]
        SR2 -->|Messages| SR3[For each message:\nenqueue raw_id + message_id\nto internal_queue]
        SR3 --> SR1
        SR2 -->|Timeout / empty| SR1
    end

    subgraph reclaim_loop
        RL1([loop every 30s]) --> RL2[StreamHandler.reclaim_pending\nmin_idle_ms=STREAM_CLAIM_TIMEOUT_MS]
        RL2 -->|Reclaimed messages| RL3[Enqueue to internal_queue]
        RL3 --> RL1
        RL2 -->|None| RL1
    end

    subgraph worker_N
        W1([loop forever]) --> W2[Dequeue raw_id + message_id]
        W2 --> W3[process_record raw_id]
        W3 -->|Success| W4[StreamHandler.ack message_id]
        W3 -->|Failure| W5[Leave unACKed\nreclaim_loop will retry]
        W4 --> W1
        W5 --> W1
    end
```

#### Method: `process_record(raw_id: UUID) -> None`

The 7-step cleaning pipeline. This is the most important method in the cleaning layer.

```
Purpose : Run the full cleaning pipeline for one raw_news record.
          On success: write cleaned_news, publish stream:raw_news_cleaned.
          On rejection: mark is_deleted=True in raw_news, log reason.
          On storage failure: raise exception (message remains unACKed for redelivery).
```

#### Flowchart — 7-Step Pipeline

```mermaid
flowchart TD
    A([process_record raw_id]) --> B{raw_id already in cleaned_news?}
    B -->|Yes - idempotency| C([Skip - return])
    B -->|No| D[Fetch raw_news record from DB]
    D --> E{Step 1: title and body not null/empty?}
    E -->|Fail| F[mark_deleted EMPTY_FIELD\nlog rejection]
    F --> C
    E -->|Pass| G[Step 2: published_at check\nalways passes - nullable OK]
    G --> H[Step 3: normalise title\nunicodedata NFKC + whitespace]
    H --> I[Step 4: compute SHA-256 of normalised_title\ncheck raw_hash collision in raw_news]
    I -->|Duplicate found| J[mark_deleted DUPLICATE_TITLE\nlog rejection]
    J --> C
    I -->|No duplicate| K[Step 5: normalise body\nNFKC + whitespace]
    K --> L{Step 6: len body_cleaned >= CLEAN_BODY_MIN_LENGTH?}
    L -->|Fail| M[mark_deleted BODY_TOO_SHORT\nlog rejection]
    M --> C
    L -->|Pass| N[Step 7: INSERT cleaned_news]
    N -->|DB error| O([raise exception\nmessage stays unACKed])
    N -->|OK| P[StreamHandler.publish_cleaned cleaned_id]
    P --> Q([return success])
```

#### Method: `_mark_deleted(raw_id: UUID, reason: str) -> None`

```
Purpose : Set is_deleted=True and deleted_reason on a raw_news record.
          Uses self.db — no pool parameter needed.
Params  : reason — "EMPTY_FIELD" / "DUPLICATE_TITLE" / "BODY_TOO_SHORT"
SQL     : UPDATE raw_news SET is_deleted=TRUE, deleted_reason=$1 WHERE raw_id=$2
```

#### Method: `_fetch_raw_news(raw_id: UUID) -> Optional[RawNews]`

```
Purpose : Fetch a single raw_news record by primary key.
Returns : RawNews dataclass, or None if not found (should not happen in normal flow —
          log a WARNING if this occurs; it may mean the DB was reset)
```

#### Method: `_insert_cleaned_news(record: CleanedNews) -> None`

```
Purpose : Insert the cleaned record into cleaned_news table.
          This must succeed before ACKing the stream message.
SQL     : INSERT INTO cleaned_news (cleaned_id, raw_id, title_cleaned, body_cleaned, created_at)
          VALUES ($1, $2, $3, $4, $5)
          Uses db.execute() — transient failure retry is handled inside DatabaseClient.
```

---

## 10. Service Entry Point — `app/main.py`

This file is the top-level entry point: `uvicorn app.main:app`

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.config import load_config
from app.db.connection import create_db_client
from app.redis.stream_client import create_stream_client
from app.crawler.page_crawler import PageCrawler
from app.crawler.crawl_service import CrawlService
from app.cleaner.cleaning_service import CleaningService
from app.cleaner.stream_handler import StreamHandler
from app.api.main import create_app
import httpx
import asyncio

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ─────────────────────────────────────────────────────────────
    config = load_config()

    db = await create_db_client(config.db)
    stream_client = await create_stream_client(config.redis_url)
    http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=config.crawl.request_timeout_s,
    )
    page_crawler = PageCrawler(http_client, config.crawl, db)
    crawl_service = CrawlService(db, stream_client, page_crawler, config.crawl)

    # Store in app.state so the Depends() helpers in dependencies.py can access them
    app.state.db = db
    app.state.stream_client = stream_client
    app.state.crawl_service = crawl_service
    app.state.config = config

    # Start the cleaning layer as a background task
    stream_handler = StreamHandler(stream_client)
    await stream_handler.ensure_consumer_group()
    cleaning_service = CleaningService(db, stream_handler, config.clean)
    cleaning_task = asyncio.create_task(cleaning_service.start())

    yield  # Service is running here

    # ── Shutdown ─────────────────────────────────────────────────────────────
    cleaning_task.cancel()
    await http_client.aclose()
    await stream_client.close()
    await db.close()

app = create_app()
app.router.lifespan_context = lifespan
```

> **Accessing shared resources in routes:** Routes must never access `app.state` directly. Use the `Depends()` helpers defined in `app/api/dependencies.py` (see Section 5.1) to inject `db`, `stream_client`, and `config` cleanly.

---

## 11. Database Migrations — `alembic/`

### Setup

Alembic is used for schema versioning. The migration history is tracked in the `alembic_version` table.

```bash
# Run all pending migrations
alembic upgrade head

# Create a new migration file (after changing schema)
alembic revision --autogenerate -m "description"

# Roll back one migration
alembic downgrade -1
```

### `alembic/versions/001_create_tables.py`

This is the initial migration that creates all three tables. Implement the `upgrade()` function to create the following tables:

**`raw_news`**
```sql
CREATE TABLE raw_news (
    raw_id          UUID PRIMARY KEY,
    source_name     VARCHAR(50) NOT NULL,
    source_url      TEXT NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    published_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL,
    raw_hash        VARCHAR(64) NOT NULL,
    extra_metadata  JSONB,
    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_reason  VARCHAR(50),
    CONSTRAINT uq_raw_news_source_url UNIQUE (source_url),
    CONSTRAINT uq_raw_news_raw_hash   UNIQUE (raw_hash)
);

CREATE INDEX idx_raw_news_is_deleted  ON raw_news (is_deleted);
CREATE INDEX idx_raw_news_created_at  ON raw_news (created_at);
CREATE INDEX idx_raw_news_extra_meta  ON raw_news USING GIN (extra_metadata);
```

**`cleaned_news`**
```sql
CREATE TABLE cleaned_news (
    cleaned_id      UUID PRIMARY KEY,
    raw_id          UUID NOT NULL REFERENCES raw_news(raw_id),
    title_cleaned   TEXT NOT NULL,
    body_cleaned    TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_cleaned_news_raw_id ON cleaned_news (raw_id);
```

**`crawl_error_log`**
```sql
CREATE TABLE crawl_error_log (
    error_id        UUID PRIMARY KEY,
    execution_id    UUID,
    source_name     VARCHAR(50) NOT NULL,
    url             TEXT NOT NULL,
    error_type      VARCHAR(50) NOT NULL,
    error_code      VARCHAR(50) NOT NULL,
    attempt_count   INTEGER NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_crawl_error_log_url ON crawl_error_log (url);
-- PageCrawler.fetch() checks "SELECT EXISTS(... WHERE url = $1)" before every page fetch.
-- Without this index the query becomes a full table scan as the error log grows.
```

---

## 12. Open Questions

These are unresolved questions from the TAD that may affect implementation. Do NOT implement around them — flag them to the team first.

| # | Question | Impact |
|---|---|---|
| Q-2 | Validate `CLEAN_BODY_MIN_LENGTH = 50` against real data | May need adjustment after first real crawl — AASTOCKS averages ~361 chars (safe), but edge cases unknown |

All other TAD open questions (Q-1, Q-3, Q-4, Q-5, Q-6, Q-7) are resolved. See TAD Section 9 for details.

---

*— End of Document | SADI Implementation Guide v0.1 —*
