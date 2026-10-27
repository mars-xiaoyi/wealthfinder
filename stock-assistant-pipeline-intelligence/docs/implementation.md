# SAPI — Implementation Guide

| Field | Detail |
|---|---|
| Service | Stock Assistant Pipeline Intelligence (SAPI) |
| Document Version | v0.1 |
| Status | DRAFT |
| Reference system-design doc | `stock-assistant-pipeline-intelligence/docs/system-design.md` |
| Reference API | `docs/api.md` |

---

## Table of Contents

1. [Development Environment Setup](#1-development-environment-setup)
2. [Project Structure](#2-project-structure)
3. [Data Models](#3-data-models)
4. [Configuration and HK Stock List](#4-configuration-and-hk-stock-list)
5. [Database Layer — `app/db/`](#5-database-layer--appdb)
6. [Redis Layer — `app/redis/`](#6-redis-layer--appredis)
7. [LLM Adapter — `app/llm/`](#7-llm-adapter--appllm)
8. [LLM Skills — `app/entity_analysis/`, `app/scoring/`](#8-llm-skills--appentity_analysis-appscoring)
9. [Clients — `app/clients/`](#9-clients--appclients)
10. [Repository — `app/persistent/`](#10-repository--apppersistent)
11. [Entity Analysis Layer — `app/entity_analysis/`](#11-entity-analysis-layer--appentity_analysis)
12. [Aggregation Layer — `app/aggregation/`](#12-aggregation-layer--appaggregation)
13. [Scoring Layer — `app/scoring/`](#13-scoring-layer--appscoring)
14. [Cache Layer — `app/cache/`](#14-cache-layer--appcache)
15. [API Layer — `app/api/`](#15-api-layer--appapi)
16. [Service Entry Point — `app/main.py`](#16-service-entry-point--appmainpy)
17. [Database Migrations — `alembic/`](#17-database-migrations--alembic)
18. [Open Questions](#18-open-questions)

---

## 1. Development Environment Setup

### Prerequisites

- Python 3.12
- Docker and Docker Compose (for PostgreSQL + Redis)
- `uv` package manager (`pip install uv`)
- A Google Cloud project with the Vertex AI API enabled, and either a service account key or `gcloud auth application-default login` for local dev

### Tooling

Configured in `pyproject.toml`; test-only dependencies are pinned in `requirements-test.txt`.

- `pytest` + `pytest-asyncio` — unit tests (`asyncio_mode = "auto"`)
- `pytest-timeout` — bounds the Docker-backed integration tests (marker `live`, skipped by default; run with `pytest -m live`, which starts its own Postgres and Redis containers through the docker CLI)
- `pytest-cov` — on-demand coverage report for the unit tests: `pytest --cov=app --cov-branch --cov-report=term-missing` (add `--cov-report=html` for a browsable report in `htmlcov/`). Not in `addopts`, so a plain `pytest` run carries no coverage overhead.
- `ruff` — linting and formatting
- `mypy` — static type checking, `strict` for `app/`

### Steps

```bash
# 1. Clone the repo and navigate to the service directory
cd stock-assistant-pipeline-intelligence

# 2. Create and activate virtual environment
uv venv
source .venv/bin/activate

# 3. Install dependencies
uv pip install -r requirements.txt

# 4. Authenticate to Vertex AI for local development
gcloud auth application-default login
# or: export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

# 5. Start local dependencies (shared with SADI; same postgres/redis instances
#    in docker-compose for local dev — see Section 13.1 of the system-design doc)
docker compose up -d postgres redis

# 6. Create SAPI's own database — compose's POSTGRES_DB only creates SADI's `sadi`.
#    SAPI uses a separate `sapi` database on the same instance, so the two services'
#    alembic_version tables never collide (§17). Set DATABASE_URL to match (§4.1):
#    postgresql://postgres:password@localhost:5432/sapi
docker compose exec postgres createdb -U postgres sapi

# 7. Run database migrations
alembic upgrade head

# 8. Start the service
uvicorn app.main:app --reload --port 8001
```

### Environment Variables for Local Development

Copy `.env.example` to `.env` (gitignored — do NOT commit it) and adjust as needed:

```bash
cp .env.example .env
```

`.env.example` is the single source of truth for the variable list and defaults (§4). The `SADI_API_URL`/`MWP_ADMIN_API_URL` defaults are local-dev only; production values are operator-supplied.

---

## 2. Project Structure

```
sapi/
├── app/
│   ├── skills/
│   │   ├── skill_base.py                # SkillInput/SkillResult base types + LlmCallMetric/LlmErrorEntry + SkillBase: run() template method + llm_process/build_fail_result — parent of all three Skills (§8.1)
│   │   ├── entity_analysis_skill.py     # EntityAnalysisSkill; EntityAnalysisInput; llm_process (one structured-output call); _verify_entity; ExtractedEntity/LLMOutput/EntityAnalysisResult (AnalysisOutcome itself lives in app/models/entity_event.py, §11.1)
│   │   ├── brief_summary_skill.py       # BriefSummarySkill; BriefSummaryInput; llm_process; input construction; fallback; BriefSummaryResult (BriefSummary itself is in app/models/event.py)
│   │   └── event_scoring_skill.py       # EventLLMScoringSkill; EventLLMScoringInput; llm_process; fallback; EventLLMScoringResult (LLMScore itself is in app/models/event_score.py)
│   ├── entity_analysis/
│   │   ├── entity_analysis_service.py   # EntityAnalysisService: find_entity_events() business logic; EntityEvent
│   │   ├── cleaned_news_consumer.py  # CleanedNewsConsumer: XREADGROUP consumer; idempotency Set; WorkerPool dispatch; ACK/backoff
│   │   └── worker_pool.py               # WorkerPool: asyncio.Semaphore concurrency control + rate-limit-backoff coordination
│   ├── aggregation/
│   │   ├── aggregation_service.py       # AggregationService: process_entity_event() business logic; coroutine manager; flush()
│   │   ├── aggregation_consumer.py      # AggregationConsumer: XREADGROUP consumer; _enqueued Set; on_processed callback; ACK
│   │   ├── group_coroutine.py           # GroupCoroutine: per-group asyncio.Queue, strictly-serial execution — no business logic
│   │   ├── sliding_window.py            # Pure sliding-window merge algorithm (no I/O)
│   │   └── event_aggregation_store.py   # EventAggregationStore: Redis active-Event HASH read/write/scan (agg:event:*)
│   ├── scoring/
│   │   ├── scoring_service.py           # ScoringService: score_event() business logic; Score Fusion; stale-data detection
│   │   ├── scoring_consumer.py          # ScoringConsumer: XREADGROUP consumer; _in_process guard; WorkerPool dispatch; ACK/backoff
│   │   └── rule_score.py                # Rule Score (excl. recency/direction) + Sentiment Aggregation (§5.6, §5.8)
│   ├── cache/
│   │   ├── cache_service.py             # CacheService: build_new_version() business logic; incremental merge; recency recomputation; atomic versioned replacement; orphan_cleanup_loop
│   │   ├── cache_consumer.py            # CacheConsumer: XREADGROUP consumer; inline reclaim; dead-letter; batch ACK
│   │   ├── recency.py                   # apply_recency(), ranking_key() — shared by the cache build and GET /v1/morning-brief (§14.3)
│   │   └── cache_reader.py              # CacheReader: the current cache version, parsed once and reused by GET /v1/morning-brief (§14.8)
│   ├── api/
│   │   ├── routes/
│   │   │   ├── morning_brief.py         # GET /v1/morning-brief
│   │   │   ├── health.py                # GET /v1/health
│   │   │   ├── hk_stock_list_sync.py # POST /v1/admin/hk-stock-list-sync (delegates to common/hk_stock_list.py)
│   │   │   └── morning_brief_refresh.py # POST /v1/admin/morning-brief-refresh — daily cache refresh, called by Admin (§15.7)
│   │   ├── schemas.py                   # Pydantic request/response schemas
│   │   ├── error_handlers.py            # SAPIError → HTTP status + ErrorResponse mapping; handler registration (§15.1)
│   │   └── main.py                      # FastAPI app factory
│   ├── llm/
│   │   ├── adapter.py                   # LLMAdapter ABC; standard data structures
│   │   ├── vertex_adapter.py            # Vertex AI implementation: generate_structured (google-genai native structured output)
│   │   └── errors.py                    # LLMError base + LLMRateLimitError / LLMProviderError / LLMAuthenticationError / LLMSchemaViolationError
│   ├── clients/
│   │   ├── news_client.py              # httpx client: fetch cleaned article content from SADI (Entity Analysis Layer input)
│   │   └── admin_client.py              # httpx client: fetch source authority config from Admin (§1.5)
│   ├── db/
│   │   ├── connection.py                # asyncpg connection pool + DatabaseClient + TransactionConnection; _db_write_with_retry() unified write retry (system-design.md §12.1)
│   │   └── errors.py                    # UniqueConstraintError / DatabaseError (driver-agnostic)
│   ├── redis/
│   │   ├── stream_client.py             # RedisStreamClient — messaging + reconnect logic; stream name constants
│   │   └── state_client.py              # RedisStateClient — thin generic Redis primitives (get/set/hash/scan/multi_exec); no business logic
│   ├── persistent/
│   │   ├── entity_event_repository.py   # EntityEventRepository: persist_entities(), fetch_entity_event() — entity_events reads/writes
│   │   ├── event_repository.py          # EventRepository: persist_events(), fetch_event(), fetch_events(), fetch_aggregation_updated_at(), update_brief_summary() — events/event_entity_map reads/writes
│   │   ├── event_score_repository.py    # EventScoreRepository: persist_score_if_not_stale(), fetch_scores() — event_scores reads/writes
│   │   └── cache_store.py                # CacheStore: morning_brief:* key ownership — version counter, versioned SET/DEL, orphan scan (§10.4)
│   ├── models/
│   │   ├── versioned_metadata.py        # VersionedMetadata[T] (no serialization method — each write site builds its own dict) — shared by entity_event.py/event.py/event_score.py
│   │   ├── entity_event.py              # EventTypeEnum/SentimentEnum (§3.1) + AnalysisOutcome (EntityAnalysisSkill's output schema, §8.2) + EntityEvent — the entity_events row itself: Skill-output hand-off, persist target, and read-back shape (§11.1, §12.1)
│   │   ├── event.py                     # Event dataclass + BriefSummary (BriefSummarySkill's output type, reused directly)
│   │   ├── event_score.py               # EventScore dataclass + RuleScore (compute_rule_score()'s own return type, reused directly) + LLMScore (Skill's own output type, reused directly)
│   │   └── event_entity_map.py          # EventEntityMap dataclass
│   ├── common/
│   │   ├── error_codes.py               # Every error code string, as commented constants (§15.1)
│   │   ├── error_categories.py          # ErrorCategory — the part of SAPI an error was raised from (§15.1)
│   │   ├── errors.py                    # SAPIError base + NotFoundError / ServiceUnavailableError / ParseError / ...
│   │   ├── http_client.py               # get_http_client()/create_http_client() — shared httpx.AsyncClient singleton (§4.2); translate_client_errors decorator (§9)
│   │   ├── stock.py                     # Stock dataclass — the HK Stock List's entry type, also used by EntityAnalysisSkill (§4.2)
│   │   ├── hk_stock_list.py             # HKStockListCache + get_hk_stock_list_cache(); HKEXnews fetch + equity filter; name index; lookup_stock + name_matches_stock; no Redis (§4.2)
│   │   └── supervised.py                # run_supervised() — leak-free concurrent-coroutine supervision, used by every layer's start() (§6.1)
│   ├── layer_tasks.py                   # register_layer_tasks()/get_layer_tasks() — the four layer Tasks, for GET /health's liveness check (§15.6)
│   ├── logger.py                        # Unified system logger; structlog; stdout JSON output (§4.3)
│   ├── config.py                        # App config: env var loading
│   └── main.py                          # Service entry point (lifespan): eager HK Stock List load; 4 layer Tasks + done_callback monitoring
├── alembic/
│   └── versions/
│       ├── 001_create_entity_events.py  # entity_events
│       ├── 002_create_events.py         # events + event_entity_map
│       └── 003_create_event_scores.py   # event_scores
├── Dockerfile
├── requirements.txt
└── docker-compose.yml
```

---

## 3. Data Models

> **On UUID collision:** as in SADI, all primary keys use `uuid.uuid4()`. No collision-handling code is needed.

> **Timestamps are timezone-aware UTC everywhere:** `datetime.now(timezone.utc)`, never `datetime.utcnow()` (naive, and deprecated since Python 3.12) — same as SADI. Every `TIMESTAMPTZ` column comes back from asyncpg timezone-aware, and Python raises `TypeError` when comparing or subtracting naive and aware values.

Data models are plain Python dataclasses, one per DB table (system-design.md §10). Pydantic schemas for the API live in `app/api/schemas.py`; Skill inputs and Result wrappers live in each Skill's own module (§8). The exceptions are the Skill output schemas `AnalysisOutcome`, `BriefSummary` and `LLMScore`: each is also the type its DB-model field wraps, so it is defined in `app/models/` (§11.1, §3.3, §13.1).

### 3.1 `EventTypeEnum`, `SentimentEnum` — `app/models/entity_event.py`

The event vocabulary of an entity's analysis, defined in `app/models/entity_event.py` beside `AnalysisOutcome` (§11.1), which they describe. `Event` (§3.3) and `app/api/schemas.py` (§3.5) import them from there.

```python
from enum import StrEnum

class EventTypeEnum(StrEnum):
    EARNINGS = "EARNINGS"
    BUYBACK = "BUYBACK"
    MA = "MA"
    REGULATORY = "REGULATORY"
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    ANALYST_RATING = "ANALYST_RATING"
    DIVIDEND = "DIVIDEND"
    GENERAL_ANNOUNCEMENT = "GENERAL_ANNOUNCEMENT"

class SentimentEnum(StrEnum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"
```

### 3.2 `app/models/versioned_metadata.py`

`VersionedMetadata[T]` is shared by four JSONB columns across three tables (`entity_events.additional_outcome`, `events.brief_summary`, `event_scores.rule_score_detail`, `event_scores.llm_score_detail`), so it has its own module. `event.py` (§3.3), `event_score.py` (§13.1) and `entity_event.py` (§11.1) import it.

**Each column wraps the type its originating computation returns, directly.** `additional_outcome` wraps `AnalysisOutcome` (§8.2/§11.1), `brief_summary` wraps `BriefSummary` (§3.3), and `rule_score_detail`/`llm_score_detail` wrap `RuleScore`/`LLMScore` (§13.1). All four types are defined in `app/models/`, and their originating modules import them (`app/models/` never imports from `app/scoring/` or `app/skills/`).

**No shared serialization method, and no `dataclasses.asdict()`/`.model_dump()`.** Each write site hand-builds its `{"version": ..., "output": {...}}` dict, naming every persisted field, so a field added to a type later doesn't silently start appearing in the DB. `additional_outcome` is a narrower projection (`company_name_zh`, `company_name_en`, `event_type_secondary`, `headline`, `entity_summary`) than `AnalysisOutcome`'s seven fields (§10.1); the read side (`_row_to_entity_event()`) is equally explicit.

```python
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

@dataclass
class VersionedMetadata(Generic[T]):
    """Generic wrapper for the {"version": ..., "output": {...}} JSONB shape (§3.2 intro).
    No serialization method: each write site builds its own dict."""
    version: str
    output: T
```

### 3.3 `app/models/event.py`

```python
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from typing import Optional

from pydantic import BaseModel

from app.models.versioned_metadata import VersionedMetadata   # §3.2

class BriefSummary(BaseModel):
    """Schema-validated output of BriefSummarySkill.run() (§8.3); wrapped by Event.brief_summary."""
    summary_short: str      # <=30 Traditional Chinese characters
    summary_full: str       # <=150 Traditional Chinese characters
    key_numbers: list[str]  # max 3 items; may be empty

@dataclass
class SourceListItem:
    source_name: str        # e.g. "HKEX", "MINGPAO"; matches entity_events.source_name / source:config keys
    url: str                # Original article URL for this source's constituent EntityEvent
    published_at: datetime  # UTC

@dataclass
class Event:
    event_id: UUID                       # Primary key; generate with uuid.uuid4() on first creation
    exchange: str                        # Morning Brief API filter
    stock_code: str                      # Morning Brief API filter
    event_type_primary: str              # Morning Brief API filter
    first_seen_at: datetime              # published_at of earliest constituent EntityEvent
    last_seen_at: datetime               # published_at of most recent constituent EntityEvent
    event_type_secondary: Optional[list[str]] = None       # Up to 2 secondary types, frequency-ranked (PRD §OQ-8) — None until persisted; the UPSERT computes it (§10.2), so whatever's set pre-persist is never read
    source_list: Optional[list[SourceListItem]] = None      # One entry per distinct source reporting this Event — None until persisted, same as event_type_secondary
    brief_summary: Optional[VersionedMetadata[BriefSummary]] = None  # NULL until the Scoring Layer's first pass (§13.1)
    brief_summary_aggregation_updated_at: Optional[datetime] = None  # the aggregation_updated_at brief_summary was generated for — lets a retry reuse it (§13.1 step 2)
    brief_summary_fallback: Optional[bool] = None                    # whether brief_summary came from BriefSummarySkill's fallback (§8.3)
    aggregation_updated_at: Optional[datetime] = None       # Written by Aggregation Layer only — set by merge_or_create() (§12.4) the moment a merge/create actually happens, not by the DB write; still None only before the first-ever merge for a group
    updated_at: Optional[datetime] = None                    # same
    created_at: Optional[datetime] = None                    # same — preserved across re-flushes of the same event_id by the UPSERT's own ON CONFLICT clause, which never re-sets it
    entity_event_ids: Optional[list[UUID]] = None            # Aggregation Layer-only, pre-persist: drives EventRepository.persist_events()'s event_entity_map write (§10.2). Never populated by fetch_event() — nothing reads it back off a persisted Event.

    @property
    def source_count(self) -> Optional[int]:
        """len(source_list). No backing column (§17); use jsonb_array_length(source_list) for a DB-level count."""
        return len(self.source_list) if self.source_list is not None else None
```

`Event` is one type across its three lifecycle stages: Redis-active (§12.3–§12.5), input to `EventRepository.persist_events()` (§10.2), and the persisted row read back by Scoring/Cache (§13.1/§14.1). `event_id`/`exchange`/`stock_code`/`event_type_primary`/`first_seen_at`/`last_seen_at`/`aggregation_updated_at` are set by `merge_or_create()` (§12.4); the remaining fields are `Optional` because the DB write computes them. After a read-back every field except `entity_event_ids` is populated.

> `source_list` is stored as JSONB (§17) but modelled as `list[SourceListItem]` for attribute access. Serialize with `dataclasses.asdict()` when writing the column or the cache payload (§14.5), and deserialize into `SourceListItem` on read. It is a plain dataclass, like the rest of `app/models/`.

> **`brief_summary` wraps `BriefSummarySkill`'s output type directly.** `ScoringService` wraps the `BriefSummaryResult.output` without conversion:
> ```python
> brief_summary = VersionedMetadata(version=result.skill_log.skill_version, output=result.output)
> ```
> Persistence hand-builds the dict (§3.2); see `EventRepository.update_brief_summary()` (§10.2).

### 3.4 `app/models/event_entity_map.py`

```python
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

@dataclass
class EventEntityMap:
    event_id: UUID           # FK -> events.event_id
    entity_event_id: UUID    # FK -> entity_events.entity_event_id; unique constraint
    created_at: datetime     # UTC; time this EntityEvent was aggregated into the Event — the flush that first
                             # persisted the mapping (EventRepository.persist_events(), §10.2); a re-flush's
                             # ON CONFLICT DO NOTHING keeps the original value
```

### 3.5 `app/api/schemas.py` — Pydantic Schemas

These mirror `docs/api.md` §3. `EventTypeEnum` and `SentimentEnum` are imported from `app.models.entity_event` (§3.1).

```python
from pydantic import BaseModel, Field
from uuid import UUID
from typing import List, Optional, Union
from datetime import datetime
from enum import Enum

from app.models.entity_event import EventTypeEnum, SentimentEnum

class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"

class ComponentStatus(str, Enum):
    OK = "ok"
    ERROR = "error"

class HKStockListStatus(str, Enum):
    OK = "ok"
    NOT_READY = "not_ready"

# ── GET /v1/health ────────────────────────────────────────────────────────────

class LayerStatus(BaseModel):
    entity_analysis: ComponentStatus
    aggregation: ComponentStatus
    scoring: ComponentStatus
    cache: ComponentStatus

class HealthResponse(BaseModel):
    status: HealthStatus
    database: ComponentStatus
    redis_stream: ComponentStatus
    redis_state: ComponentStatus
    hk_stock_list: HKStockListStatus
    layers: LayerStatus

# ── GET /v1/morning-brief ──────────────────────────────────────────────────────

class SourceListEntry(BaseModel):
    source_name: str
    url: str
    published_at: datetime

class EventDetail(BaseModel):
    event_type_secondary: List[EventTypeEnum]    # empty list if none
    source_count: int
    source_list: List[SourceListEntry]
    summary_short: str
    summary_full: str
    key_numbers: List[str]                       # empty list if none

class RuleScoreDetail(BaseModel):
    event_type_score: float
    source_authority_score: float
    sentiment_strength_score: float
    source_heat_score: float

class LLMScoreDetail(BaseModel):
    adjustment_reason: str
    score_version: str
    scored_at: datetime

class ScoreDetail(BaseModel):
    stock_impact_score: float
    llm_fallback: bool
    rule_score_detail: RuleScoreDetail
    llm_score_detail: LLMScoreDetail

class ScoredEventRecord(BaseModel):
    event_id: UUID
    exchange: str
    stock_code: str
    event_type_primary: EventTypeEnum
    abs_final_score: float          # includes recency as of the cache version's build (system-design.md §6.3)
    base_rule_score: float          # signed; includes recency as of the cache version's build
    first_seen_at: datetime
    last_seen_at: datetime
    event: EventDetail
    score: ScoreDetail

class MorningBriefResponse(BaseModel):
    cache_version: Optional[int]
    last_updated: Optional[datetime]
    events: List[ScoredEventRecord]

# ── POST /v1/admin/morning-brief-refresh ──────────────────────────────────

class MorningBriefRefreshResponse(BaseModel):
    status: str          # always "success" on 200
    cache_version: int   # the version the refresh wrote

# ── POST /v1/admin/hk-stock-list-sync ────────────────────────────────────────────

class HKStockListSyncResponse(BaseModel):
    status: str = "success"
    entries_loaded: int

# ── Error Response (used by all routes) ───────────────────────────────────────

class ErrorResponse(BaseModel):
    error_code: str
    message: str
    detail: Union[str, dict] = {}
```

---

## 4. Configuration and HK Stock List

### 4.1 Configuration — `app/config.py`

#### Purpose

Loads all environment variables into a single `AppConfig` object. All other modules import config from here — never use `os.environ` directly in business code (same rule as SADI).

#### Class: `DatabaseConfig`

```python
@dataclass
class DatabaseConfig:
    url: str                # env: DATABASE_URL
    pool_size: int           # env: DB_POOL_SIZE (default 10)
    close_timeout_s: int     # env: DB_CLOSE_TIMEOUT_S (default 10) — bound on close() (§5.1)
    max_retry: int           # env: DB_WRITE_MAX_RETRY (default 3)
    retry_base_wait_ms: int  # env: DB_RETRY_BASE_WAIT_MS (default 100)
```

#### Class: `HttpConfig`

```python
@dataclass
class HttpConfig:
    """Timeout for every outbound REST call through the shared httpx.AsyncClient (§4.2):
    HKEXnews, SADI (§9.1), Admin (§9.2). Separate from LLMConfig.api_timeout_s: the LLM
    adapter uses the google-genai SDK, not httpx (see Q-11, §18)."""
    timeout_s: int   # env: HTTP_TIMEOUT_S (default 120)
```

#### Class: `HealthConfig`

```python
@dataclass
class HealthConfig:
    """Per-check bound for GET /v1/health's DB/Redis checks (§15.2). Keep it below the
    orchestrator readiness probe's own timeout, so SAPI's 503 arrives before the probe gives up."""
    check_timeout_s: int   # env: HEALTH_CHECK_TIMEOUT_S (default 2)
```

#### Class: `RedisConfig`

```python
@dataclass
class RedisConfig:
    stream_url: str                 # env: REDIS_STREAM_URL
    state_url: str                  # env: REDIS_STATE_URL
    reconnect_interval_s: int       # env: REDIS_RECONNECT_INTERVAL_S (default 5)
    stream_claim_timeout_ms: int    # env: STREAM_CLAIM_TIMEOUT_MS
```

#### Class: `LLMConfig`

```python
@dataclass
class LLMConfig:
    vertex_project: str            # env: VERTEX_AI_PROJECT
    vertex_location: str           # env: VERTEX_AI_LOCATION (default "global" — current Gemini models are not served in asia-east1/asia-east2)
    api_timeout_s: int             # env: LLM_API_TIMEOUT_S (default 30 — gemini-3.1-flash-lite on EntityAnalysis calls: p95 8.4 s, max 18 s)
    rate_limit_backoff_s: int      # env: RATE_LIMIT_BACKOFF_S
```

#### Class: `EntityAnalysisConfig`

```python
@dataclass
class EntityAnalysisConfig:
    batch_size: int                       # env: ENTITY_ANALYSIS_BATCH_SIZE (default 10)
    batch_timeout_ms: int                 # env: ENTITY_ANALYSIS_BATCH_TIMEOUT_MS (default 5000)
    max_concurrent: int                   # env: ENTITY_ANALYSIS_MAX_CONCURRENT
    max_retry: int                        # env: ENTITY_ANALYSIS_MAX_RETRY
    max_input_chars: int                  # env: ENTITY_ANALYSIS_MAX_INPUT_CHARS (default 30000) — article body cut before
                                           # the LLM call (§8.2); HKEX announcements reach 2,000,000+ chars
    sadi_api_url: str                     # env: SADI_API_URL — local dev default: http://localhost:8000
                                           # (SADI's own quickstart port, stock-assistant-data-ingestion/docs/implementation.md §1);
                                           # excludes "/v1" — NewsClient appends it per-call (§9.1)
    consumer_name: str                    # env: HOSTNAME, fallback "sapi-entity-analysis-1" — see §6.1's Reclaim pattern
```

#### Class: `AggConfig`

```python
@dataclass
class AggConfig:
    batch_size: int                 # env: AGG_BATCH_SIZE
    batch_timeout_ms: int           # env: AGG_BATCH_TIMEOUT_MS
    max_concurrent: int             # env: AGG_MAX_CONCURRENT
    group_idle_timeout_s: int       # env: AGG_GROUP_IDLE_TIMEOUT_S
    db_flush_interval_s: int        # env: AGG_DB_FLUSH_INTERVAL_S
    max_retry: int                  # env: AGG_MAX_RETRY
    sliding_window_hours: int       # env: SLIDING_WINDOW_HOURS
    event_max_timespan_hours: int   # env: EVENT_MAX_TIMESPAN_HOURS
    consumer_name: str               # env: HOSTNAME, fallback "sapi-aggregation-1" — see §6.1's Reclaim pattern
```

#### Class: `EventTypeScore`

```python
@dataclass
class EventTypeScore:
    """One (event_type, score) pair: a Rule Score dimension input (§13.3). Lives in
    app/config.py because it shapes one config field, not a persisted or Skill-produced object."""
    event_type: str    # one of EventTypeEnum's 8 values (§3.1)
    score: float         # [0, 10]
```

#### Class: `ScoringConfig`

```python
@dataclass
class ScoringConfig:
    batch_size: int                        # env: SCORING_BATCH_SIZE
    batch_timeout_ms: int                  # env: SCORING_BATCH_TIMEOUT_MS
    max_concurrent: int                    # env: SCORING_MAX_CONCURRENT
    max_retry: int                         # env: SCORING_MAX_RETRY
    source_config_ttl_s: int               # env: SOURCE_CONFIG_TTL_S
    admin_api_url: str                     # env: MWP_ADMIN_API_URL — local dev default: http://localhost:8080
                                            # (Spring Boot's conventional port; admin/ doesn't exist yet, confirm
                                            # once application.yml is written, admin-tad.md). Excludes "/v1" —
                                            # AdminClient appends it per-call (§9.2)
    total_active_sources_default: int      # env: TOTAL_ACTIVE_SOURCES_DEFAULT
    consumer_name: str                     # env: HOSTNAME, fallback "sapi-scoring-1" — see §6.1's Reclaim pattern
    event_type_score: list[EventTypeScore]  # 8 entries, one per EventTypeEnum member (§3.1); each from its own
                                             # EVENT_TYPE_WEIGHT_* env var (PRD §12.1), assembled by load_config()
```

> **Config is loaded once at startup, not live-reloadable.** PRD §12.1 marks the `EVENT_TYPE_WEIGHT_*` family "Hot-reload: Yes", but `load_config()` runs once; the only runtime-refreshed value is `source:config` (§1.5/§13.5), which is Admin-backed Redis state. Runtime reload (e.g. `SIGHUP`) is out of scope.

#### Class: `CacheConfig`

```python
@dataclass
class CacheConfig:
    batch_timeout_ms: int                    # env: CACHE_BATCH_TIMEOUT_MS
    max_retry: int                           # env: CACHE_MAX_RETRY
    noise_filter_threshold: float            # env: NOISE_FILTER_THRESHOLD
    orphan_cleanup_interval_s: int           # env: CACHE_ORPHAN_CLEANUP_INTERVAL_S
    recency_decay_lambda: float              # env: RECENCY_DECAY_LAMBDA
    top_k_default: int                       # env: TOP_K_DEFAULT
    full_fetch_page_size: int                # env: CACHE_FULL_FETCH_PAGE_SIZE — EventScoreRepository.fetch_scores()'s (§10.3) page size, used only by the full build (`_build_full()`, §14.1)
    consumer_name: str                       # env: HOSTNAME, fallback "sapi-cache-1" — see §6.1's Reclaim pattern
```

> **`consumer_name`.** `load_config()` sets each to `os.environ.get("HOSTNAME", "<layer fallback>")`. Kubernetes sets `HOSTNAME` to the unique pod name, and uniqueness only needs to hold within a consumer group (§6.1). The fallback keeps local `XPENDING` output readable. SAPI runs as a single replica (system-design.md §13.2). A changing identity across restarts is fine: `_reclaim_loop()` (§6.1) recovers abandoned entries after `STREAM_CLAIM_TIMEOUT_MS`.

#### Class: `AppConfig`

```python
@dataclass
class AppConfig:
    db: DatabaseConfig
    http: HttpConfig
    health: HealthConfig
    redis: RedisConfig
    llm: LLMConfig
    entity_analysis: EntityAnalysisConfig
    agg: AggConfig
    scoring: ScoringConfig
    cache: CacheConfig
```

#### Function: `load_config() -> AppConfig`

```
Purpose : Read environment variables; construct and return a validated AppConfig.
Called  : Once at service startup in app/main.py lifespan.
Raises  : SAPIError (app.common.errors, §15.1) if a required env var is missing.
```

Implementation notes:
- Use `os.environ` here only (not in business code)
- Build each sub-config from its respective env var group (tables above), then compose into `AppConfig`
- `consumer_name` on all four sub-configs: `os.environ.get("HOSTNAME", "<layer fallback>")` (see the note above)
- `ScoringConfig.event_type_score` is the other field built from more than a single env var — 8 separate reads (`EVENT_TYPE_WEIGHT_EARNINGS`/`_MA`/`_REGULATORY`/`_BUYBACK`/`_MANAGEMENT_CHANGE`/`_DIVIDEND`/`_ANALYST_RATING`/`_GENERAL`, defaults 9.0/8.5/8.0/7.0/7.0/6.0/5.0/4.0), each becoming one `EventTypeScore(event_type=..., score=...)` entry in the list, in `EventTypeEnum` order (§3.1)
- Store the loaded `AppConfig` as a module-level singleton so routes can import it; `get_config()` raises `ServiceUnavailableError` if called before `load_config()`, like every `get_X()` (§5.1)

```python
# Usage in other modules:
from app.config import get_config
config = get_config()
```

---

### 4.2 HK Stock List — `app/common/hk_stock_list.py`

Its own section because it isn't only an LLM concern: `EntityAnalysisSkill`'s verification step (§8.2) reads it, `POST /admin/hk-stock-list-sync` (§15.5) refreshes it, and `GET /health` (§15.2) gates readiness on it. The two name checks of `EntityAnalysisSkill._verify_entity()` (§8.2) live here too, because both read the cache's name index: `name_matches_stock()` (step 1) and `lookup_stock()` (step 3).

**In-process only, no Redis** (rationale: system-design.md §9.2.1). One shared mutable `HKStockListCache` instance holds the fetch result.

#### `app/common/stock.py` — `Stock`

Shared by `lookup_stock()` and `EntityAnalysisSkill` (§8.2). Not DB-persisted, so it lives in `app/common/` rather than `app/models/`. It represents a *found* match; `lookup_stock()` returns `None` for a miss.

`stock_code` is the **bare native code** (e.g. `"00700"`, no exchange suffix), matching HKEXnews's feed. `exchange` (currently always `"HKEX"`) is its own field, as on every other stock-bearing model (§3).

```python
from dataclasses import dataclass

@dataclass
class Stock:
    stock_code: str          # bare native code, e.g. "00700" — no exchange suffix
    exchange: str             # e.g. "HKEX" — currently always "HKEX", single-exchange MVP scope
    company_name_zh: str
    company_name_en: str

    @property
    def company_name(self) -> str:
        """company_name_zh, falling back to company_name_en when the Chinese name is empty."""
        return self.company_name_zh or self.company_name_en
```

#### `app/common/http_client.py` — the one other new getter this module needs

`refresh_hk_stock_list()` (below) is called from an API route (§15.5), so the shared `httpx.AsyncClient` created at startup gets a `get_X()` accessor (§5.1) in its own module:

```python
# app/common/http_client.py
_http_client: Optional[httpx.AsyncClient] = None

def get_http_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise ServiceUnavailableError("http client is not initialized", category=ErrorCategory.COMMON)
    return _http_client

def create_http_client(timeout_s: int) -> httpx.AsyncClient:
    global _http_client
    _http_client = httpx.AsyncClient(timeout=timeout_s)
    return _http_client
```

`timeout_s` is the only thing it configures (`HttpConfig.timeout_s`, §4.1), so it is an explicit typed parameter rather than `**kwargs`.

#### Class: `HKStockListCache`

`entries` holds `Stock` (below) directly, so `entries[k].stock_code == k`. `name_index` is the same list's names prepared for matching (`StockNameIndex`, below): the constructor builds it from `entries`, and `reload()` replaces both together.

The one shared mutable holder for the in-process HK Stock List, with the same `get_X()` treatment as other infra (§5.1). `app/main.py` (§16) constructs it at startup; `EntityAnalysisSkill` (§8.2) and the sync route (§15.5) reach the same instance via `get_hk_stock_list_cache()`, so a refresh's `reload()` is immediately visible.

```python
class HKStockListCache:
    def __init__(self, entries: dict[str, Stock]):
        self.entries = entries
        self.name_index = _build_name_index(entries)

    async def reload(self, entries: dict[str, Stock]) -> None:
        name_index = await asyncio.to_thread(_build_name_index, entries)   # CPU work off the event loop
        # WARNING: rebind both, back to back with no await between, never mutate in place. A
        # lookup_stock() call must see one list's entries and name_index together, and a call
        # mid-iteration over the old ones must keep seeing a consistent snapshot.
        self.entries = entries
        self.name_index = name_index

    def __len__(self) -> int:
        """Lets GET /health's readiness gate (§15.2) just do len(cache) > 0."""
        return len(self.entries)

_hk_stock_list_cache: Optional[HKStockListCache] = None

def get_hk_stock_list_cache() -> HKStockListCache:
    if _hk_stock_list_cache is None:
        raise ServiceUnavailableError(
            "get_hk_stock_list_cache() called before refresh_hk_stock_list() initialized it",
            category=ErrorCategory.STOCK_LIST,
        )
    return _hk_stock_list_cache
```

#### Company-name matching

One normalizer and one name index serve both name checks in `_verify_entity()` (§8.2): `name_matches_stock()` (step 1) and `lookup_stock()` (step 3). Rationale: system-design.md §9.2. Traditional-to-simplified folding needs `opencc` (runtime dependency). Everything below is private except the index types and the two checks.

**Normalization.** Each name has up to three variants, distinct, longest first: the normalized name, then without its legal suffix, then also without its generic tail (騰訊控股有限公司 → 腾讯控股有限公司, 腾讯控股, 腾讯):

```python
import opencc

_T2S = opencc.OpenCC("t2s")
_COUNTER_MARKER_RE = re.compile(r"[－\-][A-ZＡ-Ｚ]{1,3}$")          # －Ｗ, -SW, －Ｂ …
_FULLWIDTH = str.maketrans({chr(c): chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)})
_SPACE_BETWEEN_LATIN_RE = re.compile(r"(?<=[a-z0-9])\s+(?=[a-z0-9])")
_ZH_LEGAL_SUFFIX_RE = re.compile(r"(股份)?有限公司$|公司$")
_ZH_GENERIC_TAIL_RE = re.compile(r"(控股|股份)+$")                   # matched after t2s
_EN_LEGAL_WORDS = {"limited", "ltd", "co", "company", "corporation", "corp", "inc", "plc", "llc", "the"}
_EN_GENERIC_WORDS = {"holdings", "holding", "hldg", "hldgs", "group", "gp", "international", "intl", "int'l",
                     "and", "&"}

def _normalize(text: str) -> str:
    """Counter marker dropped, full-width to ASCII, traditional to simplified, case-folded; whitespace removed
    except one space between two Latin words."""
    text = _T2S.convert(_COUNTER_MARKER_RE.sub("", text).translate(_FULLWIDTH)).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return _SPACE_BETWEEN_LATIN_RE.sub("\x00", text).replace(" ", "").replace("\x00", " ")

def _name_variants(raw: str) -> list[str]:
    full = _normalize(raw)
    if full.isascii():
        legal = " ".join(t for t in re.split(r"[\s.,]+", full) if t and t not in _EN_LEGAL_WORDS)
        generic = " ".join(t for t in legal.split() if t not in _EN_GENERIC_WORDS)
    else:
        legal = _ZH_LEGAL_SUFFIX_RE.sub("", full)
        generic = _ZH_GENERIC_TAIL_RE.sub("", legal)
    return list(dict.fromkeys(v for v in (full, legal, generic) if v))
```

**Index.** `_build_name_index(entries)` builds it for `HKStockListCache`'s constructor and `reload()` (above):

```python
@dataclass(frozen=True)
class NameEntry:
    stock_code: str
    variants: list[str]             # _name_variants() of one of the stock's names
    latin: bool                     # variants[0].isascii()

@dataclass(frozen=True)
class StockNameIndex:
    names: list[NameEntry]                              # one per distinct non-empty zh/en name of each stock
    names_by_code: dict[str, list[NameEntry]]           # stock_code -> its names (step 1)
    names_by_latin_prefix: dict[str, list[NameEntry]]   # first 3 letters of a 3+-letter word of a Latin variant

def _build_name_index(entries: dict[str, Stock]) -> StockNameIndex: ...
```

**`name_matches_stock(name: str, stock: Stock) -> bool`** (step 1). Reads `get_hk_stock_list_cache().name_index.names_by_code[stock.stock_code]` (no entry: `False`). True when some variant `q` of `name` and some variant `n` of one of the stock's names satisfy: `q` is an ordered subsequence of `n` (農行 ⊂ 農業銀行), or `n` is an ordered subsequence of `q` and `len(n) >= 0.6 * len(q)` (a short listed name inside a long legal name, but not "CHINA NEXT-GEN" inside "Amundi FTSE China A50 Index ETF").

**`_search(index: StockNameIndex, name: str) -> Optional[str]`** (step 3, behind `lookup_stock()`):

```
variants = _name_variants(name)
exact = {e.stock_code for e in index.names if set(e.variants) & set(variants)}
if exact:
    candidates = index.names; codes = exact
else:
    candidates = [] if not variants
                 else every index.names_by_latin_prefix[w[:3]] for each 3+-letter word w of each variant   if Latin
                 else every non-Latin entry of index.names
    score each candidate: the highest _score(q, n) over q in variants, n in its variants (None: no match)
    codes = the stock codes holding the highest score
code = the one code if len(codes) == 1 else None      # a tie between stock codes is a miss
log lookup_stock_completed (below) with candidates_scanned = len(candidates)
return code
```

`_score(q, n)` returns a tuple compared element by element, or `None`:

```
both Latin (qw, nw = word lists of q and n):
    name_hits  = count of words x in nw where some y in qw has y == x, or len(x) >= 3 and y.startswith(x)
    query_hits = count of words y in qw where some x in nw has x == y, or len(y) >= 3 and x.startswith(y)
    name_hits == len(nw) and total letters of nw >= 3   -> (4, name_hits / len(qw), len(nw))    # whole name in query
    query_hits == len(qw)                               -> (3, query_hits / len(nw), -len(n))  # whole query in name
    otherwise None
one Latin, one not -> None
both CJK:
    len(q) < 2                                          -> None
    neither set(q) <= set(n) nor set(n) <= set(q)       -> None
    len(q) <= len(n) and q is an ordered subsequence of n, tightest span [start, end):
                                                        -> (5, len(q) / (end - start), int(start == 0), len(q) / len(n), -len(n))
    3 <= len(n) < len(q) and n is an ordered subsequence of q
                                                        -> (4, len(n) / len(q), len(n))
    otherwise None
```

The tightest span is the earliest-ending match, started as late as that end allows.

#### Function: `lookup_stock(name: str) -> Optional[Stock]`

```
Purpose : Name search over the HK Stock List: step 3 of EntityAnalysisSkill._verify_entity() (§8.2),
          for an extracted entity whose code is missing or does not check out.
Params  : name — the extracted entity's company_name as the LLM wrote it
Returns : Stock on a unique best match, None on a miss (no match, or a tie between companies).
```

```python
def lookup_stock(name: str) -> Optional[Stock]:
    hk_stock_list_cache = get_hk_stock_list_cache()
    code = _search(hk_stock_list_cache.name_index, name)
    return hk_stock_list_cache.entries[code] if code else None
```

No cache parameter: it reads `.entries` and `.name_index` off one `get_hk_stock_list_cache()` instance. It always searches by name: codes are checked in `_verify_entity()` step 1, which handles code normalization (system-design.md §9.2).

#### Performance verification — `lookup_stock_completed` log event

`_search()` runs synchronously on the event loop shared by all four layers (§16). Pre-traffic measurements are in §18 I-1. It times itself for the real-traffic check:

```python
_SLOW_CALL_THRESHOLD_MS = 20   # WARNING instead of INFO above this; not env-configurable

# at the end of _search(), start = time.perf_counter() taken on entry:
latency_ms = (time.perf_counter() - start) * 1000
level = logging.WARNING if latency_ms > _SLOW_CALL_THRESHOLD_MS else logging.INFO
logger.log(level, "lookup_stock_completed", name=name, candidates_scanned=len(candidates),
           latency_ms=latency_ms, found=code is not None)
```

`name` in the log line identifies which entity triggered a slow call. `logger` is the shared structlog logger (`app/logger.py`).

#### Sources

```python
_ACTIVESTOCK_EN_URL = "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json"
_ACTIVESTOCK_ZH_URL = "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_c.json"
_LIST_OF_SECURITIES_URL = (
    "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
)
```

Parsing the spreadsheet needs `openpyxl` (runtime dependency, alongside `httpx`).

#### Parsing — `_parse_hk_stock_list(en_content, zh_content, securities_content) -> dict[str, Stock]`

Synchronous and side-effect free; `_fetch_and_filter_hk_stock_list()` (below) runs it in `asyncio.to_thread` (~0.4 s of CPU, which would otherwise block the event loop every layer and route shares, §16).

```python
_STOCK_CODE_HEADER = "Stock Code"
_CATEGORY_HEADER = "Category"
_EQUITY_CATEGORY = "Equity"

def _parse_names(content: bytes) -> dict[str, str]:
    """{stock_code: name} from one activestock JSON file."""
    return {entry["c"]: entry["n"] for entry in json.loads(content)}

def _parse_equity_codes(content: bytes) -> set[str]:
    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    try:
        sheet = workbook.worksheets[0]
        sheet.reset_dimensions()   # read-only mode trusts the file's recorded size, which HKEX's
                                   # file understates: without this, only ~8 of ~17,300 rows come back
        rows = sheet.iter_rows(values_only=True)
        for row in rows:           # title rows come first; columns are located by header name
            if _STOCK_CODE_HEADER in row and _CATEGORY_HEADER in row:
                code_col = row.index(_STOCK_CODE_HEADER)
                category_col = row.index(_CATEGORY_HEADER)
                break
        else:
            raise ParseError("List of Securities header row not found", category=ErrorCategory.STOCK_LIST)
        return {str(row[code_col]) for row in rows if row[category_col] == _EQUITY_CATEGORY}
    finally:
        workbook.close()

def _is_extra_counter(code: str, zh_name: str, en_name: str) -> bool:
    """An RMB counter (80700 騰訊控股－Ｒ) or a trading-only line (－Ｔ / -T) of a company listed under another code."""
    return (code.startswith("8") and zh_name.endswith("Ｒ")) or zh_name.endswith("Ｔ") or en_name.endswith("-T")

def _parse_hk_stock_list(en_content: bytes, zh_content: bytes, securities_content: bytes) -> dict[str, Stock]:
    """Join the two name files by stock code; keep equities only, without extra counters."""
    en_names = _parse_names(en_content)
    zh_names = _parse_names(zh_content)
    entries: dict[str, Stock] = {}
    for code in _parse_equity_codes(securities_content):
        if code not in en_names and code not in zh_names:
            continue                                       # an equity in neither file is skipped
        zh_name = zh_names.get(code, "")                   # a name missing from one file is ""
        en_name = en_names.get(code, "")
        if _is_extra_counter(code, zh_name, en_name):
            continue                                       # rationale: system-design.md §9.2.1
        entries[code] = Stock(stock_code=code, exchange="HKEX", company_name_zh=zh_name, company_name_en=en_name)
    return entries
```

#### Function: `_fetch_and_filter_hk_stock_list(http_client: httpx.AsyncClient) -> dict[str, Stock]`

```
Purpose : Fetch the three sources above and return {native_code: Stock(...)} for equities only.
Raises  : Every failure is translated into one of two exceptions, both category=STOCK_LIST:
            - ServiceUnavailableError (COMMON-5002, HTTP 503): any httpx.HTTPError
              (HKEX unreachable, slow, or non-2xx), after _get()'s connection retries.
            - ParseError (COMMON-6000, HTTP 500): any exception while parsing a successful
              response (schema change, changed "List of Securities" format). Deliberately
              `except Exception`.
```

Private; pure fetch+filter with no cache side effects, so it is testable with a mock `http_client`. `refresh_hk_stock_list()` (below) is its only caller. Each GET goes through `_get()`, which retries a failed connection (rationale: system-design.md §9.2.1):

```python
_CONNECT_RETRY_WAITS_S = (1, 2)   # one wait before each retry: 3 attempts in all

async def _get(http_client: httpx.AsyncClient, url: str) -> httpx.Response:
    """Retries httpx.ConnectError only. A timeout or non-2xx is raised at once: retrying a slow
    HKEX would multiply HTTP_TIMEOUT_S."""
    for attempt, wait_s in enumerate(_CONNECT_RETRY_WAITS_S, start=1):
        try:
            return await http_client.get(url)
        except httpx.ConnectError:
            logger.warning("hkex_connect_retry", url=url, attempt=attempt, wait_s=wait_s)
            await asyncio.sleep(wait_s)
    return await http_client.get(url)

async def _fetch_and_filter_hk_stock_list(http_client: httpx.AsyncClient) -> dict[str, Stock]:
    try:
        en_resp = await _get(http_client, _ACTIVESTOCK_EN_URL)
        en_resp.raise_for_status()
        zh_resp = await _get(http_client, _ACTIVESTOCK_ZH_URL)
        zh_resp.raise_for_status()
        securities_resp = await _get(http_client, _LIST_OF_SECURITIES_URL)
        securities_resp.raise_for_status()
    except httpx.HTTPError as exc:
        # Resource issue: COMMON-5002 (upstream unreachable), not the COMMON-5001 default (§15.1).
        raise ServiceUnavailableError.upstream_unavailable(
            f"HKEXnews unreachable: {exc!r}", category=ErrorCategory.STOCK_LIST
        ) from exc

    try:
        return await asyncio.to_thread(
            _parse_hk_stock_list, en_resp.content, zh_resp.content, securities_resp.content
        )
    except ParseError:
        raise                       # already translated (header row not found)
    except Exception as exc:
        # Code issue: fetch succeeded but the response shape was wrong. Deliberately `except Exception`:
        # nothing escapes untranslated.
        raise ParseError(
            f"HK Stock List response shape mismatch: {exc!r}", category=ErrorCategory.STOCK_LIST
        ) from exc
```

#### Function: `refresh_hk_stock_list() -> int`

```
Purpose : Fetch the HK Stock List from HKEXnews and load it into the process-wide
          HKStockListCache, with its StockNameIndex. The first call registers a new
          HKStockListCache as the singleton; later calls reload() the existing instance.
          Called by app/main.py's eager startup load (system-design.md §9.2.1) and by
          POST /admin/hk-stock-list-sync (§15.5).
On fetch failure : Any exception installs an empty HKStockListCache if none is registered
          yet, then re-raises, so get_hk_stock_list_cache() stays usable and GET /health
          reports not_ready until a later call succeeds. An existing cache is left untouched.
          A fetch that yields zero equities is a failure too (ParseError): HKEX always lists
          equities, so zero means a changed cell value (e.g. a renamed category), and loading
          it would wipe a loaded cache.
Raises  : Whichever exception _fetch_and_filter_hk_stock_list() raised (§4.2), or the
          zero-equities ParseError. app/main.py's startup logs hk_stock_list_startup_load_failed
          and continues (§16); the sync route lets it propagate as HTTP 503 COMMON-5002 or
          HTTP 500 COMMON-6000 (§15.1). While the cache is empty, CleanedNewsConsumer holds
          messages unACKed (§11.2/§11.4).
Returns : entries_loaded count (len(new_entries)) on success.
```

```python
async def refresh_hk_stock_list() -> int:
    global _hk_stock_list_cache
    try:
        new_entries = await _fetch_and_filter_hk_stock_list(get_http_client())
        if not new_entries:
            raise ParseError("HK Stock List parsed to zero equities", category=ErrorCategory.STOCK_LIST)
        if _hk_stock_list_cache is None:
            _hk_stock_list_cache = await asyncio.to_thread(HKStockListCache, new_entries)  # index off the loop
        else:
            await _hk_stock_list_cache.reload(new_entries)
    except Exception:
        # Deliberately bare: the process must never crash over this dependency. Always re-raises;
        # only the fallback-cache install is unconditional.
        if _hk_stock_list_cache is None:
            _hk_stock_list_cache = HKStockListCache({})
        raise

    return len(new_entries)
```

Zero-argument (§5.1's pattern): it reaches `get_http_client()` itself, so `main.py`'s lifespan and the route handler call it identically.

### 4.3 Logging — `app/logger.py`

Every module logs through structlog: one JSON object per line on stdout, filtered at `INFO`.

#### Function: `configure_logging() -> None`

```
Purpose : Configure structlog once, at import of app/main.py. Each record carries
          `event`, `level`, an ISO-8601 UTC `timestamp`, and — when logged with
          exc_info — a structured `exception` traceback.
```

#### Function: `get_logger(name: str) -> structlog.typing.FilteringBoundLogger`

```
Purpose : Return a logger bound with `module=<name>`; callers pass their own __name__
          and keep it at module level (`logger = get_logger(__name__)`). The key is
          `module`, not `logger`: structlog's get_logger() reserves `logger` for its
          own argument.
```

Call sites log an event name plus keyword fields, e.g. `logger.warning("health_check_failed", component="database", exc_info=True)`.

#### Processor: `add_sapi_error_fields(logger, method_name, event_dict) -> EventDict`

Runs in `configure_logging()`'s chain just before `dict_tracebacks` (which replaces `exc_info`). When a record's `exc_info` is a `SAPIError` (§15.1) — given as `True` (the exception being handled), the exception itself, or a `sys.exc_info()` tuple — it adds the error's `category` and `error_code` to the record, so every exception-logging call carries them without the call site passing them. Any other exception is logged unchanged.

---

## 5. Database Layer — `app/db/`

### 5.1 `app/db/connection.py`

#### Purpose

Exposes a `DatabaseClient` class encapsulating the `asyncpg` pool and retry logic. Business code calls methods on it; the pool and config are never passed around. Mirrors SADI's `DatabaseClient` (`stock-assistant-data-ingestion/docs/implementation.md` §5.1); retry/exception contract: system-design.md §12.1.

**Module-level singleton.** This is the shape of every process-wide resource in SAPI (`DatabaseClient`, `RedisStreamClient`/`RedisStateClient` §6, `LLMAdapter` §7.3, `HKStockListCache` §4.2, `EventAggregationStore` §12.5, `CacheStore` §10.4, `NewsClient`/`AdminClient` §9, the repositories §10) and of each layer's `Service` (§11.1/§12.1/§13.1/§14.1): `create_X()` constructs and registers it once, and consumers call `get_X()` directly, like `get_config()`. `main.py` (§16) decides construction order where data dependencies exist. Skills and `WorkerPool` instances are layer-owned: constructed in `main.py` and passed to their layer's `Consumer` (§16).

Every `get_X()` raises `ServiceUnavailableError` (`app/common/errors.py`) if called before its `create_X()` counterpart has run — reusing the same exception type a genuinely unreachable DB/Redis raises (§5.2, §15.1), since both mean "this resource is not currently usable" and a caller has no way to (or need to) tell the two apart.

#### Class: `DatabaseClient`

```python
class DatabaseClient:
    def __init__(self, pool: asyncpg.Pool, config: DatabaseConfig):
        self._pool = pool
        self._config = config
```

#### Function: `get_db_client() -> DatabaseClient`

```
Purpose : Return the process's one DatabaseClient instance.
Raises  : ServiceUnavailableError if called before create_db_client() has
          run — should only happen if something ran ahead of app/main.py's
          lifespan startup, or a test forgot to initialize/override it.
```

```python
_db_client: Optional[DatabaseClient] = None

def get_db_client() -> DatabaseClient:
    if _db_client is None:
        raise ServiceUnavailableError("database client is not initialized")
    return _db_client
```

Used directly — `from app.db.connection import get_db_client` — by any module that needs DB access, routes included (§5.4).

---

#### Method: `execute(query: str, *args) -> None`

```
Purpose : Execute a write query (INSERT / UPDATE / UPSERT) with transient-failure
          retry and exponential backoff, via _db_write_with_retry() (§5.2). Use
          this for all single-statement writes.
Params  : query — SQL string with positional placeholders ($1, $2, ...)
          *args — query parameter values in positional order
Raises  : UniqueConstraintError (app.db.errors) on a unique-constraint
          violation, DatabaseError (app.db.errors) on other data-integrity
          failures, or ServiceUnavailableError (app.common.errors) on
          a connection failure / after all retries exhausted.
```

Retry backoff and exception classification: §5.2. Only this module imports `asyncpg`.

> **Unique-constraint violations are expected** (system-design.md §12.1): a duplicate `(source_url, stock_code)` in `entity_events` or `(event_id, entity_event_id)` in `event_entity_map` is an idempotent no-op. These writes use `INSERT ... ON CONFLICT ... DO NOTHING`, so they never raise `UniqueConstraintError`.

---

#### Method: `execute_returning(query: str, *args) -> Optional[asyncpg.Record]`

```
Purpose : Execute a write query with a RETURNING clause (e.g. an
          INSERT ... ON CONFLICT DO NOTHING RETURNING <col>), with the same
          retry-on-transient-failure and exception-translation semantics as
          execute().
Returns : The first returned row, or None if the statement affected no rows
          (an ON CONFLICT DO NOTHING no-op — e.g. EntityAnalysisService detects a
          (source_url, stock_code) retry/redelivery this way and skips
          publishing an entity_event_id that was never actually written).
Raises  : Same as execute() above.
```

---

#### Method: `fetch_one(query: str, *args) -> Optional[asyncpg.Record]`

```
Purpose : Execute a SELECT query and return the first matching row, or None if not found.
          No retry logic — reads are safe to re-issue by the caller if needed.
Raises  : DatabaseError or ServiceUnavailableError (same translation as
          execute(), minus the retry loop).
```

#### Method: `fetch_all(query: str, *args) -> list[asyncpg.Record]`

```
Purpose : Execute a SELECT query and return all matching rows. No retry logic.
Returns : List of asyncpg.Record; empty list if no rows found
```

---

#### Method: `transaction() -> AsyncContextManager[TransactionConnection]`

```
Purpose : Acquire a dedicated pool connection and open a transaction on it
          (BEGIN), yielding a thin TransactionConnection wrapper exposing
          execute()/execute_returning() bound to that single connection.
          Commits on clean exit; rolls back on any exception.
          Opening — acquire + BEGIN — goes through _db_write_with_retry() (§5.2);
          statements inside the open transaction get the same exception
          translation but no retry (system-design.md §12.1).
Used by : EventRepository's persist_events() (§10.2) — Upserting every active
          Redis Event to `events` and writing all of its entity_event_map
          rows must commit together as one unit, so a mid-flush failure
          never leaves a partially-persisted Event.
          EntityEventRepository's persist_entities() (§10.1) — all of one
          article's entity_events rows commit together, so an article is
          either fully persisted or not at all (system-design.md §3.4).
```

```python
async with db.transaction() as tx:
    await tx.execute(_UPSERT_EVENT_SQL, ...)
    for entity_event_id in group.entity_event_ids:
        await tx.execute(_UPSERT_EVENT_ENTITY_MAP_SQL, ...)
# commits here; raises and rolls back if any statement inside failed all retries
```

---

#### Function: `create_db_client(config: DatabaseConfig) -> DatabaseClient`

```
Purpose : Create the asyncpg pool, wrap it in a DatabaseClient, and register
          it as this module's singleton (get_db_client() serves it from
          then on). This is the only place asyncpg.create_pool() is called.
Called  : Once in the FastAPI lifespan startup handler (app/main.py).
Returns : The instance get_db_client() now serves; main.py uses it for local wiring and shutdown.
```

```python
_db_client: Optional[DatabaseClient] = None

async def create_db_client(config: DatabaseConfig) -> DatabaseClient:
    global _db_client
    pool = await asyncpg.create_pool(
        dsn=config.url,
        min_size=2,
        max_size=config.pool_size,
    )
    _db_client = DatabaseClient(pool, config)
    return _db_client
```

#### Method: `close(timeout_s: float) -> None`

```
Purpose : Gracefully close the underlying connection pool, bounded by timeout_s
          (config.db.close_timeout_s): asyncio.wait_for(pool.close(), timeout_s). On timeout
          asyncpg terminates the pool itself; close() logs WARNING db_pool_close_timed_out
          and returns (system-design.md §7.1).
Called  : In the FastAPI lifespan shutdown, registered on its exit stack (§16).
```

### 5.2 `_db_write_with_retry()` — `app/db/connection.py`

All DB writes go through `_db_write_with_retry()` (system-design.md §12.1), a private module-level helper beside `DatabaseClient` in `connection.py`, called only by `DatabaseClient`'s write methods (§5.1): `execute()`/`execute_returning()` wrap a whole pooled statement in it, `transaction()` wraps opening its connection. The retry policy is defined once.

#### Function: `_db_write_with_retry[T](config: DatabaseConfig, write: Callable[[], Awaitable[T]]) -> T`

```
Purpose : Run write(), retrying it while it fails in a way a fresh attempt can
          clear, and translate every asyncpg error into app.db.errors /
          app.common.errors.
Params  : config — DatabaseConfig (max_retry, retry_base_wait_ms)
          write  — the attempt to run: acquire a connection and run one
                   statement, or (transaction()) acquire one and BEGIN
Returns : write()'s result.
Raises  : ServiceUnavailableError once max_retry attempts have failed or the
          database is unreachable; UniqueConstraintError / DatabaseError on any
          other database failure, without retry.
```

Backoff `wait_ms = config.retry_base_wait_ms × 2^(attempt-1)`, a WARNING `db_write_retrying` per retried attempt. Classification:

| asyncpg error | Handling |
|---|---|
| `TooManyConnectionsError`, `PostgresConnectionError` (incl. `ConnectionDoesNotExistError`), `CannotConnectNowError`, `OSError` (refused/reset TCP) | retried; `ServiceUnavailableError` once `max_retry` attempts failed |
| `InterfaceError` (closed pool/connection) | `ServiceUnavailableError`, no retry |
| `UniqueViolationError` | `UniqueConstraintError(constraint_name)`, no retry |
| any other `PostgresError` (`DataError`, `NotNullViolationError`, …) | `DatabaseError`, no retry |

The same translation, without the retry, applies to `fetch_one()`/`fetch_all()` and to statements inside an open `transaction()`.

### 5.3 `app/db/errors.py`

```python
from app.common.errors import SAPIError   # §15.1

class UniqueConstraintError(SAPIError):
    """Raised on a unique-constraint violation not already absorbed by ON CONFLICT DO NOTHING."""
    error_type = ErrorType.STORAGE
    error_code = error_codes.INTERNAL_ERROR

    def __init__(self, constraint_name: str):
        super().__init__(f"unique constraint violated: {constraint_name}")
        self.constraint_name = constraint_name

class DatabaseError(SAPIError):
    """Raised on any other data-integrity failure."""
    error_type = ErrorType.STORAGE
    error_code = error_codes.INTERNAL_ERROR
```

Both subclass `SAPIError` (§15.1) so a catch-all `except Exception` can't swallow them as unanticipated bugs.

`ServiceUnavailableError` lives in `app/common/errors.py` (§15.1) because it is also raised outside the DB layer; every infra `get_X()` reuses it (§5.1).

### 5.4 Infra access in routes

Routes call each owning module's no-argument `get_X()` in the handler body; infra is never a handler parameter or a `Depends()` target. Handler parameters are only request inputs (path/query parameters, body) and, where a route sets its own status code, FastAPI's `response: Response`.

```python
from app.db.connection import get_db_client

async def get_cleaned_news(cleaned_id: UUID):
    row = await get_db_client().fetch_one("SELECT ... WHERE cleaned_id = $1", cleaned_id)
```

Unit tests replace a getter by monkeypatching it on the route's module.

---

## 6. Redis Layer — `app/redis/`

Per system-design.md §1.4 / §11.1, SAPI opens **two independent Redis connections**, even when both point at the same physical Redis instance in MVP: `RedisStreamClient` for all `stream:*` keys, `RedisStateClient` for everything else. Business code never calls `redis-py` directly — always go through one of these two wrappers.

### 6.1 `app/redis/stream_client.py`

#### Stream Name Constants

```python
# app/redis/stream_client.py

STREAM_RAW_NEWS_CLEANED         = "stream:raw_news_cleaned"          # produced by SADI; consumed by Entity Analysis Layer
STREAM_ENTITY_EVENT_COMPLETED   = "stream:entity_event_completed"    # produced by Entity Analysis Layer; consumed by Aggregation Layer
STREAM_EVENT_AGGREGATED         = "stream:event_aggregated"          # produced by Aggregation Layer; consumed by Scoring Layer
STREAM_EVENT_SCORED             = "stream:event_scored"              # produced by Scoring Layer; consumed by Cache Layer

STREAM_ENTITY_ANALYSIS_DEAD_LETTER          = "stream:entity_analysis_dead_letter"
STREAM_AGGREGATION_DEAD_LETTER  = "stream:aggregation_dead_letter"
STREAM_SCORING_DEAD_LETTER      = "stream:scoring_dead_letter"
STREAM_CACHE_DEAD_LETTER        = "stream:cache_dead_letter"

CONSUMER_GROUP_ENTITY_ANALYSIS          = "sapi-entity-analysis"
CONSUMER_GROUP_AGGREGATION  = "sapi-aggregation"
CONSUMER_GROUP_SCORING      = "sapi-scoring"
CONSUMER_GROUP_CACHE        = "sapi-cache"
```

| Constant | Producer | Consumer Group |
|---|---|---|
| `STREAM_RAW_NEWS_CLEANED` | SADI | `sapi-entity-analysis` |
| `STREAM_ENTITY_EVENT_COMPLETED` | Entity Analysis Layer | `sapi-aggregation` |
| `STREAM_EVENT_AGGREGATED` | Aggregation Layer | `sapi-scoring` |
| `STREAM_EVENT_SCORED` | Scoring Layer | `sapi-cache` |

> **Message shape check — `v` and the id field (system-design.md §2.3).** Every published message carries `v` (current value: `"1"`) and the layer's id field (`cleaned_id`, `entity_event_id` or `event_id`, each a UUID). Each layer's consumer checks both immediately after `read_from()`/`autoclaim()` returns and before any processing: a message whose `v` is missing or unrecognised, or whose id field is missing or not a valid UUID, is routed straight to that layer's Dead Letter Stream (§11–§14's per-layer error handling) and ACKed without a processing attempt — it can never succeed, so retrying it would only spend delivery attempts. It uses the same dead-letter payload as the Reclaim and Dead Letter pattern below; on the `read_from()` path, `delivery_count` is `"1"` — a first delivery.

**Module-level singleton, same pattern as `DatabaseClient`** (§5.1). `create_stream_client()` constructs and registers it; every consumer calls `get_redis_stream_client()` directly rather than taking `redis_stream` as a constructor parameter.

#### Class: `RedisStreamClient`

```python
class RedisStreamClient:
    def __init__(self, client: redis.asyncio.Redis, reconnect_interval_s: int): ...
```

**Constructor:** accepts an already-connected `redis.asyncio.Redis` instance and `REDIS_RECONNECT_INTERVAL_S` (`publish()`'s retry interval, below). Use `create_stream_client()` instead of calling this directly.

#### Data Structures: `StreamMessage`, `ReclaimedMessage`

`read_from()`/`autoclaim()` return these for attribute access (`message.id`, `message.fields`). `fields` stays `dict[str, str]` because its keys depend on the stream's producer.

```python
@dataclass
class StreamMessage:
    id: str
    fields: dict[str, str]

@dataclass
class ReclaimedMessage(StreamMessage):
    delivery_count: int   # XAUTOCLAIM's native redelivery counter — system-design.md §12.1's "Retry Count via delivery_count"
```

#### Function: `get_redis_stream_client() -> RedisStreamClient`

Same shape as `get_db_client()` (§5.1): raises `ServiceUnavailableError` if called before `create_stream_client()` has run.

#### Function: `create_stream_client(redis_url: str, reconnect_interval_s: int) -> RedisStreamClient`

```
Purpose : Create a Redis connection pool, verify connectivity with PING,
          wrap it in a RedisStreamClient, and register it as this module's
          singleton (get_redis_stream_client() serves it from then on).
Called  : Once in the FastAPI lifespan startup handler (app/main.py).
Raises  : ConnectionError if Redis is unreachable.
```

Implementation is identical to SADI's `create_stream_client()` (`decode_responses=True`, PING to force a real connection) — see `stock-assistant-data-ingestion/docs/implementation.md` §6.1 — plus the same `_redis_stream_client = ...; return _redis_stream_client` registration `create_db_client()` (§5.1) uses.

#### Method: `publish(stream: str, fields: dict) -> str`

```
Purpose : Write a message to a Redis Stream (XADD). All fields must be strings
          — convert UUIDs, floats, etc. before calling. Callers always include
          "v": "1" alongside the business fields. While the stream Redis is
          unreachable (redis.ConnectionError / redis.TimeoutError), logs WARNING
          redis_stream_publish_retrying, sleeps reconnect_interval_s and retries
          the same XADD — indefinitely, until it is written (cancellable).
Returns : The Redis message ID.
```

#### Method: `read_from(stream, group, consumer, count: Optional[int], block_ms) -> list[StreamMessage]`

```
Purpose : Read messages from a stream as part of a consumer group (XREADGROUP).
          Returns as soon as any messages are available (up to count; count=None
          omits COUNT and returns everything pending — the Cache Layer, §14.2); block_ms
          applies only while the stream is empty. So it bounds a batch's size and
          the wait for its first message — it does not wait for a batch to fill
          (system-design.md §2.3). A caller that needs larger batches under a
          trickle coalesces several reads itself (Cache, §14.2).
Returns : List of StreamMessage; empty list if block_ms elapses with no
          messages (this is also each layer's "queue empty" / flush trigger
          signal — see §12.2, §14.2).
```

#### Method: `ack(stream: str, group: str, message_id: str) -> None`

```
Purpose : Acknowledge a processed message (XACK). Call ONLY after every write
          this message's processing implies has committed (DB write(s),
          downstream stream publish). ACKing before that risks silent data
          loss on a crash between ACK and the writes it was gating.
```

#### Method: `autoclaim(stream, group, consumer, min_idle_ms, count) -> list[ReclaimedMessage]`

```
Purpose : Reclaim messages pending (unACKed) longer than min_idle_ms (XAUTOCLAIM).
Returns : List of ReclaimedMessage. delivery_count is XAUTOCLAIM's native
          redelivery counter — system-design.md §12.1's "Retry Count via delivery_count"
          policy reads this directly, no separate Redis state is needed to
          track attempts.
```

#### Method: `create_group_if_not_exists(stream: str, group: str) -> None`

```
Purpose : XGROUP CREATE ... MKSTREAM at id 0, so a new group starts with the stream's
          first message. A group that already exists (BUSYGROUP) keeps its position and
          is logged at INFO consumer_group_exists with stream and group.
Called  : Once per stream/group pair, at each layer service's startup.
```

#### Method: `close() -> None`

```
Purpose : Close the Redis connection. Called in the service shutdown handler.
```

#### Method: `ping() -> None`

```
Purpose : Re-verify connectivity on demand — the same PING check
          create_stream_client() already runs once at construction (above),
          exposed as a public method so a caller can re-check liveness
          later, not just at startup. Used by GET /v1/health (§15.2), whose
          own spec calls for a live PING per request.
Raises  : Whatever the underlying redis.asyncio.Redis.ping() raises
          (redis.ConnectionError / redis.TimeoutError) — untranslated, since
          the caller (§15.2) wants the raw signal to classify itself rather
          than a pre-classified SAPI exception.
```

```python
async def ping(self) -> None:
    await self._client.ping()
```

#### Reconnect behaviour

Per system-design.md §12.1 "RedisStreamClient Reconnect": `read_from()`/`ack()`/`autoclaim()` let `redis.ConnectionError` and `redis.TimeoutError` through untranslated. Each layer's consumer loop catches them around its own calls, logs a WARNING (`entity_analysis_redis_stream_unreachable` in §11.2), sleeps `REDIS_RECONNECT_INTERVAL_S` and loops again — indefinitely. `publish()` is the exception and retries in place (above; rationale: system-design.md §12.1). Consumer Group position is preserved server-side across the outage; processing resumes from the last ACKed message once the connection returns.

#### Reclaim and Dead Letter pattern

`autoclaim()` alone (§6.1 above) is not sufficient — something has to call it periodically, decide when a reclaimed message has exhausted its retries, and route it to that layer's Dead Letter Stream. All four layers (§11–§14) implement the *same* pattern, specified once here rather than four times:

- **`config.consumer_name`** — each layer's own config (`EntityAnalysisConfig`/`AggConfig`/`ScoringConfig`/`CacheConfig`, §4.1) carries a `consumer_name` field, sourced from the `HOSTNAME` env var with a layer-specific fallback string for local/docker-compose runs (§4.1's note), so it's automatically unique per pod under horizontal scaling with no operator action required. Passed as the `consumer` argument to every `read_from()`/`autoclaim()` call that layer makes.
- **`_reclaim_loop()`** — a coroutine each layer's `start()` runs alongside its main consumer loop via `asyncio.gather()` (**except Cache Layer — see §14.2's note; its reclaim step runs inside the single serial loop, not as a separate coroutine**). Wakes every `STREAM_CLAIM_TIMEOUT_MS` milliseconds and calls `autoclaim(stream, group, config.consumer_name, min_idle_ms=STREAM_CLAIM_TIMEOUT_MS, count=<layer's batch size>)`. The loop body is wrapped in `except Exception` (not bare `except:`, so `asyncio.CancelledError` still propagates for shutdown) that logs a warning and does not re-raise; `run_supervised()` is the backstop.
- **Per-message isolation, in both loops.** Each message from a `read_from()` or `autoclaim()` batch is handled — shape check, retry-exhaustion check, routing — inside its own `except Exception` that logs a warning with `exc_info` and leaves that message un-ACKed, so one message's failure never abandons the rest of its batch (on the reclaim path, every message in the batch has already had its `delivery_count` incremented by `autoclaim()`, so an abandoned batch would push healthy messages toward dead-lettering unprocessed).
- **Retry-exhaustion check, per reclaimed message, before anything else:**
  ```python
  if reclaimed.delivery_count >= config.max_retry:   # ENTITY_ANALYSIS_MAX_RETRY / AGG_MAX_RETRY / SCORING_MAX_RETRY / CACHE_MAX_RETRY
      await redis_stream.publish(STREAM_<LAYER>_DEAD_LETTER, {
          "message_id": reclaimed.id, "fields": json.dumps(reclaimed.fields),
          "delivery_count": str(reclaimed.delivery_count), "failed_at": datetime.now(timezone.utc).isoformat(),
          "v": "1",
      })
      await redis_stream.ack(stream, group, reclaimed.id)
      continue   # never enters the layer's normal processing path
  ```
  This is system-design.md §12.1's "Retry Count via delivery_count" policy — `delivery_count` comes from `autoclaim()` natively, so no separate Redis state tracks attempt counts.
- **Below the retry-exhaustion check, a reclaimed message that still has budget left is routed through *the exact same synchronous check-then-insert entry point* as a freshly `read_from()`'d message** — never a second, separately-written processing path. This is not a style preference: the in-memory Set each layer uses to detect an already-in-flight duplicate (§11.2's `processing_cleaned_ids`, §12.2's `_enqueued`, §13.2's `_in_process`) is only a correctness guarantee if there is exactly one code path where a message can be marked "in progress," with no `await` between checking and marking it. A reclaimed message calling into a *different* function — even one that "does the same thing" — reopens exactly the race the guard exists to close. See each layer's own section for its specific entry-point method and why a duplicate delivery is safe.

#### Layer Supervision — `app/common/supervised.py` — `run_supervised()`

Every layer's `start()` runs its main consumer loop alongside one secondary loop (`_reclaim_loop()` for three layers, `orphan_cleanup_loop()` for Cache, §14.2). A bare `asyncio.gather()` propagates the first exception but doesn't cancel the sibling, so `main.py`'s Coroutine Monitoring (§16) would restart `start()` while the first loop is still running. For Cache Layer, `CacheService`'s build lock (§14.9) keeps the two loops' builds from overlapping, but every build would run twice.

`run_supervised()` replaces `asyncio.gather()` in each `start()` (§11.2, §12.2, §13.2, §14.2) and guarantees no sibling survives a failure:

```python
# app/common/supervised.py
import asyncio
from typing import Coroutine

async def run_supervised(*coros: Coroutine) -> None:
    """Runs coroutines as one unit. If any raises, cancels and awaits the rest before
    re-raising, so a layer-Task restart (system-design.md §12.1) starts clean."""
    tasks = [asyncio.create_task(c) for c in coros]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for t in done:
        if (exc := t.exception()) is not None:
            raise exc
```

The loops themselves catch anticipated per-iteration failures and log a warning (§14.6 for `orphan_cleanup_loop()`); `run_supervised()` is the backstop for unanticipated bugs.

### 6.2 `app/redis/state_client.py`

Wraps a second, independent `redis.asyncio.Redis` connection (constructed from `config.redis.state_url`) for every non-stream key in system-design.md §11.2–§11.4.

**A thin, generic wrapper with no business logic.** Key-specific logic lives with its owner: `EventAggregationStore` (§12.5), `get_source_weights()`/`count_active_sources()` (§13.5), `CacheStore` (§10.4). The HK Stock List (§4.2) doesn't use Redis.

#### Class: `RedisStateClient`

```python
class RedisStateClient:
    def __init__(self, client: redis.asyncio.Redis): ...

    async def get(self, key: str) -> Optional[str]: ...
    async def set(self, key: str, value: str) -> None: ...
    async def incr(self, key: str) -> int: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...
    async def expire(self, key: str, ttl_s: int) -> None: ...
    async def rename(self, src: str, dst: str) -> None: ...
    async def hget(self, key: str, field: str) -> Optional[str]: ...
    async def hgetall(self, key: str) -> Optional[dict[str, str]]: ...
    async def mget(self, keys: list[str]) -> list[Optional[str]]: ...
    async def hmget(self, key: str, fields: list[str]) -> dict[str, Optional[str]]: ...
    async def hset(self, key: str, fields: dict[str, str]) -> None: ...
    async def hlen(self, key: str) -> int: ...
    async def scan(self, pattern: str) -> list[str]: ...
    async def multi_exec(self, build: Callable[[redis.asyncio.client.Pipeline], None]) -> None: ...
    async def ping(self) -> None: ...
    async def close(self) -> None: ...
```

**Module-level singleton** (§5.1's pattern), constructed via `create_state_client(redis_url: str) -> RedisStateClient` (PING on startup, same as `RedisStreamClient`); `get_redis_state_client()` is what `EventAggregationStore` (§12.5), `CacheStore` (§10.4), `get_source_weights()`/`count_active_sources()` (§13.5), and any other consumer call directly.

| Method | Wraps | Notes |
|---|---|---|
| `get`/`set`/`incr`/`delete`/`exists`/`expire`/`rename` | `GET`/`SET`/`INCR`/`DEL`/`EXISTS`/`EXPIRE`/`RENAME` | — |
| `mget` | `MGET` | One atomic read of several keys, in order; `None` for a missing key |
| `hget`/`hgetall`/`hmget`/`hset`/`hlen` | `HGET`/`HGETALL`/`HMGET`/`HSET`/`HLEN` | `hgetall` returns `None` if the key doesn't exist (vs. redis-py's empty-dict default) so callers can distinguish "no such key" from "empty hash" |
| `scan(pattern)` | `SCAN` (cursor loop, collected) | Returns the full matching key list — every current caller (§12.5, §10.4) consumes the whole result anyway, so there's no streaming/cursor-exposing variant |
| `multi_exec(build)` | `MULTI`/`EXEC` | `build` receives a `redis.asyncio.client.Pipeline` and queues commands on it (`pipe.set(...)`, `pipe.delete(...)`, `pipe.rename(...)`, ...) — nothing is sent to Redis until `EXEC`, so every queued command commits atomically together. This is the primitive `CacheStore.write_cache_version()` (§10.4) builds its atomic swap on |
| `ping()` | `PING` | Live connectivity check for `GET /v1/health` (§15.2); raises redis errors untranslated |

---

## 7. LLM Adapter — `app/llm/`

### 7.1 `app/llm/adapter.py` — Standard Data Structures and Interface

Per system-design.md §8, all LLM interactions go through a provider-agnostic `LLMAdapter`. Skill code (Section 8) never imports `google.genai` directly — only `app.llm.adapter`.

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Generic, Optional, TypeVar
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"

@dataclass(frozen=True)
class GenerationConfig:
    """Per-call generation settings. Each Skill passes its own constant (§8's table) on
    every call, so the values live with the Skill that owns them, not in the Adapter."""
    model: str
    temperature: float
    max_output_tokens: int

@dataclass
class Message:
    role: Role
    content: str

@dataclass
class StructuredLLMResponse(Generic[T]):
    """Return shape for generate_structured(): the validated output plus per-call metrics."""
    output: T
    input_tokens: int
    output_tokens: int           # visible output plus thinking tokens — both are billed as output (§7.3)
    latency_ms: int


class LLMAdapter(ABC):
    """Provider-agnostic interface. Switching providers means a new subclass, no Skill changes."""

    @abstractmethod
    async def generate_structured(self, messages: list[Message], output_schema: type[T],
                                  generation_config: GenerationConfig) -> StructuredLLMResponse[T]:
        """
        Single LLM call whose response is constrained to output_schema, used by all three Skills.
        Returns the validated output plus input_tokens/output_tokens/latency_ms, measured here (§7.3).
        Raises: LLMRateLimitError, LLMProviderError, LLMAuthenticationError,
        LLMSchemaViolationError.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release underlying provider client resources."""
        ...
```

### 7.2 `app/llm/errors.py`

`SAPIError`s (§15.1) with per-class `error_code`/`log_level` defaults. `VertexAIAdapter` (§7.3) raises them with the provider's error detail as `dev_message` and `category=ErrorCategory.LLM`, and every Skill's `run()` (§8.1) catches them all internally.

```python
import logging

from app.common import error_codes
from app.common.errors import ErrorType, SAPIError   # §15.1

class LLMError(SAPIError):
    """Common base for the typed exceptions below."""
    error_type = ErrorType.LLM
    error_code = error_codes.LLM_ERROR

class LLMRateLimitError(LLMError):
    """Provider rate limit hit. Retryable — triggers Worker Pool backoff (system-design.md §12.1)."""
    error_code = error_codes.LLM_RATE_LIMITED
    log_level = logging.WARNING

class LLMProviderError(LLMError):
    """Provider-side error: 5xx, network timeout, or LLM_API_TIMEOUT_S exceeded. Retryable."""
    error_code = error_codes.LLM_PROVIDER_ERROR
    log_level = logging.WARNING

class LLMRequestError(LLMError):
    """Provider rejected the request itself (any other 4xx: malformed request, unknown model
    or location). Not self-healing: a code or config fix is needed."""
    error_code = error_codes.LLM_REQUEST_REJECTED

class LLMAuthenticationError(LLMError):
    """Provider rejected the call as unauthenticated/unauthorized (401/403). Retryable at the
    message level but not self-healing, so it pages."""
    error_code = error_codes.LLM_AUTHENTICATION_FAILED
    log_level = logging.CRITICAL

class LLMSchemaViolationError(LLMError):
    """The response did not parse into the output schema (e.g. JSON cut off at
    max_output_tokens). Not retried at this layer — the calling Skill applies its own
    fallback policy (§5.9, §8.4–8.5)."""
    error_code = error_codes.LLM_SCHEMA_VIOLATION
```

Rate-limit and provider errors are outside outages that need no code or config change, so they log at WARNING; a rejected request needs a code or config fix, so it logs at ERROR; an authentication failure needs an operator to fix the credentials, so it logs at CRITICAL.

The shared `except` block that turns these into an `LlmErrorEntry` is in `SkillBase.run()` (§8.1).

### 7.3 `app/llm/vertex_adapter.py` — `VertexAIAdapter(LLMAdapter)`

Pinned SDK: `google-genai==2.33.0`.

```python
class VertexAIAdapter(LLMAdapter):
    def __init__(self, config: LLMConfig):
        # genai.Client(vertexai=True, project=config.vertex_project,
        #              location=config.vertex_location,
        #              http_options=HttpOptions(timeout=config.api_timeout_s * 1000))
        ...
```

`HttpOptions.timeout` (milliseconds) is the call timeout: the SDK has no default timeout, so without it a call can hang indefinitely. `retry_options` stays unset, so the SDK makes exactly one attempt per call; retries happen at the message level (redelivery) and through `WorkerPool` backoff (§11.3), never inside the Adapter.

**Module-level singleton** (§5.1). `VertexAIAdapter.__init__` stays directly constructible for tests; registration is a thin factory:

```python
_llm_adapter: Optional[LLMAdapter] = None

def get_llm_adapter() -> LLMAdapter:
    if _llm_adapter is None:
        raise ServiceUnavailableError("LLM adapter is not initialized")
    return _llm_adapter

def create_llm_adapter(config: LLMConfig) -> LLMAdapter:
    global _llm_adapter
    _llm_adapter = VertexAIAdapter(config)
    return _llm_adapter
```

Each Skill calls `get_llm_adapter()` in its `__init__`. `main.py` calls `create_llm_adapter(config.llm)` once at startup.

#### Method: `generate_structured(messages, output_schema, generation_config) -> StructuredLLMResponse[T]`

```
Purpose : One client.aio.models.generate_content call. The SYSTEM message becomes
          system_instruction; the remaining messages become contents (USER → role "user",
          ASSISTANT → role "model"). GenerateContentConfig carries
          generation_config.temperature/max_output_tokens, response_mime_type="application/json"
          and response_json_schema=output_schema.model_json_schema(); model=generation_config.model.
          Gemini enforces the schema at the API level; the response text is then parsed with
          output_schema.model_validate_json(response.text).
Metrics : input_tokens  = usage_metadata.prompt_token_count
          output_tokens = usage_metadata.candidates_token_count + (usage_metadata.thoughts_token_count or 0)
          latency_ms    = wall-clock around the call
          thoughts_token_count is reported apart from candidates_token_count, is None when the
          model does not think, and is billed as output and counted against max_output_tokens.
Raises  : LLMRateLimitError on google.genai.errors.APIError with code 429.
          LLMAuthenticationError on APIError with code 401/403.
          LLMRequestError on APIError with any other 4xx code.
          LLMProviderError on any other APIError, network error, or timeout (the SDK reports
          its own timeout as a 504 DEADLINE_EXCEEDED APIError) — the catch-all for anything
          not classified above, so nothing escapes as an unclassified exception.
          LLMSchemaViolationError when model_validate_json fails (truncated or malformed JSON).
```

Each call contributes one `llm_calls[]` entry (system-design.md §9.8) to the Skill's log. The Adapter returns the measurements; the Skill adds `call_index`/`purpose` (§8.2–§8.4).

#### Method: `close() -> None`

Releases the underlying `google-genai` client's HTTP session.

---

## 8. LLM Skills — `app/entity_analysis/`, `app/scoring/`

Three Skills (rationale: system-design.md §9.1), each owning its prompt, output schema, and business-constraint validation. Skill modules are the single place prompt engineering happens. **Copy each system prompt (§8.2/§8.3/§8.4) verbatim into the module's `_SYSTEM_PROMPT` constant.**

| Skill | Module | Model | Temperature | Max Tokens |
|---|---|---|---|---|
| EntityAnalysisSkill | `app/skills/entity_analysis_skill.py` | `gemini-3.1-flash-lite` | 0 | 16384 |
| BriefSummarySkill | `app/skills/brief_summary_skill.py` | `gemini-3.1-flash-lite` | 0.1 | 4096 |
| EventLLMScoringSkill | `app/skills/event_scoring_skill.py` | `gemini-3.1-flash-lite` | 0 | 2048 |

Every Skill makes `generate_structured` calls only. Each module holds its row as a constant, `_GENERATION_CONFIG = GenerationConfig(model=..., temperature=..., max_output_tokens=...)` (§7.1), and passes it on every Adapter call it makes; a model change is a Skill version bump (system-design.md §9.1.1).

EntityAnalysisSkill's row is measured on real SADI articles: its largest output was 13,917 tokens (a 121-company weekly calendar), every other article stayed under 4,400, and `gemini-3.1-flash-lite` does not think by default. The other two rows are starting values (I-3, §18).

Each module defines a version constant (`ENTITY_SKILL_VERSION`, `BRIEF_SKILL_VERSION`, `SCORING_SKILL_VERSION`, all `"v1.0.0"`) per system-design.md §9.1.1 and sets it as `skill_log.skill_version` (§8.6). Callers read the version from `result.skill_log.skill_version` (§11.1, §13.4) and never re-import the constant.

### 8.1 `app/skills/skill_base.py` — Skill Base Types

Everything shared across Skills lives in this one file: `SkillInput`, `SkillResult`, `LlmCallMetric`/`LlmErrorEntry`, and `SkillBase`.

`SkillInput`/`SkillResult` are empty marker bases; each Skill subclasses them in its own module (§8.2–§8.4).

`LlmCallMetric`/`LlmErrorEntry` are the element types of `SkillBase._llm_calls`/`_llm_errors`, shared by all three completion logs (§8.2–§8.4, system-design.md §9.8).

Each Skill instance is constructed fresh per `run()` call (§11.1, §13.1), so `input` is stored on `self._input`. `SkillBase.run()` is a template method: `try: return await llm_process()`, `except` the typed LLM exceptions (§7.2): append an `LlmErrorEntry`, then `build_fail_result(err)`. It doesn't log the error: the result carries it, and the consumer that finally catches the re-raised error logs it (§11.2, §13.2). `llm_process()` returns the final `SkillResult` directly on success. A subclass `__init__` must call `super().__init__(input)`.

`failure_reason` is the caught `LLMError` itself. Callers (`EntityAnalysisService` §11.1, `ScoringService` §13.1) re-raise it; their consumers log it and only distinguish `LLMRateLimitError` (triggers `WorkerPool.trigger_backoff()`) from everything else (no ACK).

> **`LLMAuthenticationError` is its own type** because it isn't self-healing: every call fails until an operator fixes the credentials, so its CRITICAL `log_level` (§7.2) pages immediately when the consumer logs it. Deduping the CRITICAL log across a sustained outage is deferred.

```python
@dataclass
class SkillInput:
    pass

class SkillResult(BaseModel):
    pass

@dataclass
class LlmCallMetric:
    call_index: int
    purpose: str              # "structured_output"
    input_tokens: int
    output_tokens: int
    latency_ms: int

@dataclass
class LlmErrorEntry:
    call_index: int
    error_type: str           # "LLMRateLimitError" | "LLMProviderError" | "LLMRequestError" | "LLMAuthenticationError" | "LLMSchemaViolationError" | "BusinessValidationError"
    error_detail: str
    retried: bool

class SkillBase(ABC):
    def __init__(self, input: SkillInput):
        self._input = input
        self._llm_calls: list[LlmCallMetric] = []
        self._llm_errors: list[LlmErrorEntry] = []

    async def run(self) -> SkillResult:
        try:
            return await self.llm_process()
        except LLMError as err:
            self._llm_errors.append(LlmErrorEntry(call_index=len(self._llm_calls) + 1,
                                                   error_type=type(err).__name__,
                                                   error_detail=str(err), retried=False))
            return self.build_fail_result(err)

    @abstractmethod
    async def llm_process(self) -> SkillResult:
        """Runs the Skill's LLM call(s) from self._input and returns the final SkillResult
        directly. Raises one of the typed LLM exceptions (§7.2) on failure; run()
        appends the LlmErrorEntry, so build_fail_result() only builds the result from
        self + err."""
        ...

    @abstractmethod
    def build_fail_result(self, err: LLMError) -> SkillResult:
        ...
```

---

### 8.2 `app/skills/entity_analysis_skill.py` — EntityAnalysisSkill

Identifies the stock entities mentioned in one cleaned article and classifies each into an event, in one structured-output call: the model lists every company it finds with the code the article prints (or the HKEX code it knows) and its name, plus `event_type_primary`/`event_type_secondary`, `sentiment_label`/`sentiment_score`, `headline`, and `entity_summary`. Code then verifies each listed company against the HK Stock List (`_verify_entity()`, below); only verified companies make it into the result — unverified ones are dropped, not guessed at. Design rationale: system-design.md §9.2–§9.4. `EntityAnalysisService` (§11.1) is its only caller, constructing a fresh instance per `run()` call.

#### Input Schema

```python
@dataclass
class EntityAnalysisInput(SkillInput):
    title: str
    body: str
```

#### System Prompt

Design rationale for the extraction rules and edge cases below: system-design.md §9.4.

*Part 1 — Role and Task Definition:*
```
你是香港股票市場新聞分析助手。你的任務是從財經新聞文章中提取所有提及的上市公司實體，
並為每個實體分析對應的事件類型、市場情緒與事件摘要。

規則：
- 列出文章提及的每一家公司，包括只以簡稱、英文名或在行情列表中出現的公司，即使文章未標示股票代碼
- 每個實體填寫 company_name（文章中的公司名稱）及 stock_code（文章標示的代碼；未標示時填你所知的港股代碼，不知道則留空）
- 不必自行判斷是否於 HKEX 上市：系統會以 HKEX 股票主列表核實每個實體，非 HKEX 上市者會被自動剔除
```

*Part 2 — Analysis Dimensions:*
```
對每個實體，輸出以下分析：

【event_type_primary】主要事件類型，從以下選項中選擇最符合的一個：
EARNINGS / BUYBACK / MA / REGULATORY / MANAGEMENT_CHANGE /
ANALYST_RATING / DIVIDEND / GENERAL_ANNOUNCEMENT

【event_type_secondary】次要事件類型（可為空），與 primary 不同的第二相關類型

【sentiment_label】情緒方向：POSITIVE / NEUTRAL / NEGATIVE
- POSITIVE：對該股票具明確正面影響
- NEGATIVE：對該股票具明確負面影響
- NEUTRAL：影響不明確或屬常規公告

【sentiment_score】情緒強度 [0.0, 1.0]
- 0.0-0.3：弱信號（措辭謹慎、影響有限）
- 0.4-0.6：中等信號
- 0.7-1.0：強信號（明確財務數據、重大事件）

【headline】繁體中文事件標題，≤20字，概括該實體最重要的事件

【entity_summary】針對該實體在本文中相關內容的摘要，繁體中文，≤100字
- 只包含與該實體直接相關的事件、數據、管理層表態
- 不包含其他實體的信息或與該實體無關的市場背景
```

*Part 3 — Few-shot Examples:* the four examples below, each written as `範例 N：` on its own line followed by the example's block. `_SYSTEM_PROMPT` is Parts 1, 2, 3 and 4 in that order, with a blank line between parts and between examples.

*Part 4 — Edge Case Rules:*
```
1. 文章未提及任何 HKEX 上市公司 → 輸出 output_items: []
2. 文章同時提及 A 股和 H 股：只提取 HKEX 上市的 H 股實體；stock_code 填 H 股的港股代碼
3. 文章提及公司但確認非 HKEX 上市（純 A 股、美股、未上市）→ 從輸出中排除，不體現於 output_items
4. event_type_secondary 只在確實存在明確次要事件時填寫，不確定時留空優於猜測
5. 同一 stock_code 在文章中對應多個事件時，只能輸出一筆記錄：最重要的事件填入 event_type_primary，次重要的填入 event_type_secondary；不得為同一 stock_code 輸出多筆記錄
```

The user message is `標題：{title}\n內文：{body}`, with `body` cut to `ENTITY_ANALYSIS_MAX_INPUT_CHARS` (§4.1).

**Few-shot Example 1 — Multi-entity, different event types:**
```
INPUT:
標題：騰訊控股公佈第四季業績，廣告收入按年增長32%；阿里巴巴電商業務遭監管調查
內文：騰訊控股（00700.HK）第四季廣告收入錄得強勁增長，超出市場預期。
     監管機構宣佈就阿里巴巴（09988.HK）電商業務展開正式調查。

OUTPUT:
output_items: [
  {
    stock_code: "00700", company_name: "騰訊控股",
    event_type_primary: "EARNINGS", event_type_secondary: null,
    sentiment_label: "POSITIVE", sentiment_score: 0.85,
    headline: "騰訊Q4廣告收入超預期32%",
    entity_summary: "騰訊控股第四季廣告收入按年增長32%，大幅超出市場預期，顯示廣告業務強勁復甦。"
  },
  {
    stock_code: "09988", company_name: "阿里巴巴",
    event_type_primary: "REGULATORY", event_type_secondary: null,
    sentiment_label: "NEGATIVE", sentiment_score: 0.78,
    headline: "監管機構調查阿里電商業務",
    entity_summary: "監管機構宣佈就阿里巴巴電商業務展開正式調查，涉及市場競爭行為，調查範圍及時間表未明。"
  }
]
```

**Few-shot Example 2 — Implicit negative (cautious management language):**
```
INPUT:
標題：碧桂園管理層表示對下半年市場「審慎樂觀」，將控制新項目推進節奏
內文：碧桂園（02007.HK）行政總裁表示，鑑於市場環境存在不確定性，
     集團將採取更為審慎的擴張策略，暫緩部分非核心城市項目。

OUTPUT:
output_items: [
  {
    stock_code: "02007", company_name: "碧桂園",
    event_type_primary: "GENERAL_ANNOUNCEMENT", event_type_secondary: null,
    sentiment_label: "NEGATIVE", sentiment_score: 0.62,
    headline: "碧桂園審慎擴張暫緩新項目",
    entity_summary: "碧桂園管理層以市場不確定性為由，宣佈採取審慎擴張策略並暫緩非核心城市項目，措辭反映隱性負面展望。"
  }
]
```

**Few-shot Example 3 — Routine boilerplate, neutral:**
```
INPUT:
標題：港交所公佈例行董事會會議日期安排
內文：香港交易所（00388.HK）公佈本年度董事會例行會議時間表，與往年安排基本一致。

OUTPUT:
output_items: [
  {
    stock_code: "00388", company_name: "香港交易所",
    event_type_primary: "GENERAL_ANNOUNCEMENT", event_type_secondary: null,
    sentiment_label: "NEUTRAL", sentiment_score: 0.10,
    headline: "港交所公佈例行董事會日期",
    entity_summary: "港交所公佈本年度例行董事會會議時間表，與往年安排一致，不含任何實質業務或財務信息。"
  }
]
```

**Few-shot Example 4 — Conflicting signals:**
```
INPUT:
標題：舜宇光學季度收入增長但毛利率下滑，管理層下調全年指引
內文：舜宇光學（02382.HK）季度收入按年增長15%，惟毛利率由28%收窄至23%，
     管理層同步下調全年盈利指引。

OUTPUT:
output_items: [
  {
    stock_code: "02382", company_name: "舜宇光學",
    event_type_primary: "EARNINGS", event_type_secondary: null,
    sentiment_label: "NEGATIVE", sentiment_score: 0.58,
    headline: "舜宇光學收入增長惟毛利率收窄",
    entity_summary: "舜宇光學季度收入按年增長15%，但毛利率由28%跌至23%，管理層下調全年盈利指引，forward-looking信號主導負面判斷。"
  }
]
```

#### Output Schemas

`ExtractedEntity` is the schema the structured-output call returns; `_verify_entity()` turns each verified one into an `AnalysisOutcome` (defined in `app/models/entity_event.py`, §11.1, because `EntityEvent.analysis_outcome` wraps it).

```python
from pydantic import BaseModel
from app.models.entity_event import AnalysisOutcome, EventTypeEnum, SentimentEnum   # §3.1/§11.1

class ExtractedEntity(BaseModel):
    stock_code: str | None             # the code the article prints, else the HKEX code the model knows, else null
    company_name: str                  # as written in the article
    event_type_primary: EventTypeEnum
    event_type_secondary: EventTypeEnum | None
    sentiment_label: SentimentEnum
    sentiment_score: float             # [0.0, 1.0]
    headline: str                      # <=20 Traditional Chinese characters
    entity_summary: str                # <=100 Traditional Chinese characters

class LLMOutput(BaseModel):
    output_items: list[ExtractedEntity]    # may be empty

@dataclass
class VerifiedEntity:
    """One verified company: its analysis and the listed Stock it was verified against."""
    stock: Stock                       # §4.2
    outcome: AnalysisOutcome

class EntityAnalysisResult(SkillResult):   # base: app/skills/skill_base.py, §8.1
    """run()'s entire return value (§11.1 reads skill_log/failure_reason
    directly off this)."""
    model_config = {"arbitrary_types_allowed": True}   # failure_reason: an LLMError isn't natively Pydantic-validatable

    entity_events: Optional[list[VerifiedEntity]]    # None unless failure_reason is None — verified items only
    skill_log: EntityAnalysisSkillLog                          # always populated, every outcome (§8.2, below)
    failure_reason: Optional[LLMError] = None                  # None on success; else the caught
                                                                 # exception itself (§8.1 — no
                                                                 # separate classification type)
```

#### Skill Log Schema

`LlmCallMetric`/`LlmErrorEntry` live in `skill_base.py` (§8.1); these are specific to this Skill:

```python
@dataclass
class UnverifiedEntity:
    stock_code: Optional[str]
    company_name: str
    reason: str                # "not_in_hk_stock_list"

@dataclass
class EntityAnalysisSkillLog:
    skill_version: str
    verified_entities: list[str]                 # verified stock_codes
    unverified_entities: list[UnverifiedEntity]
    input_truncated: bool                        # body was cut at ENTITY_ANALYSIS_MAX_INPUT_CHARS
    llm_calls: list[LlmCallMetric]
    total_input_tokens: int
    total_output_tokens: int
    total_latency_ms: int
    llm_errors: list[LlmErrorEntry]
    timestamp: datetime
```

#### Class: `EntityAnalysisSkill`

```python
class EntityAnalysisSkill(SkillBase):   # SkillBase — app/skills/skill_base.py, §8.1
    def __init__(self, input: EntityAnalysisInput, config: EntityAnalysisConfig):
        super().__init__(input)   # sets self._input/_llm_calls/_llm_errors (§8.1)
        self._adapter = get_llm_adapter()               # §7.3
        self._hk_stock_list_cache = get_hk_stock_list_cache()  # §4.2
        self._config = config
        # Safe on self for the same reason as SkillBase's own state (§8.1, §11.1):
        # fresh instance per run() call.
        self._verified_entities: list[str] = []   # verified stock codes, in output order
        self._unverified: list[UnverifiedEntity] = []
        self._input_truncated = False
```

`run()` is inherited from `SkillBase` (§8.1) — not redefined here.

#### `llm_process()`

```
body = self._input.body[:self._config.max_input_chars]
self._input_truncated = len(body) < len(self._input.body)
messages = [Message(Role.SYSTEM, _SYSTEM_PROMPT), Message(Role.USER, f"標題：{self._input.title}\n內文：{body}")]

structured = generate_structured(messages, output_schema=LLMOutput, generation_config=_GENERATION_CONFIG)
append LlmCallMetric(call_index=1, purpose="structured_output", ...from structured) to self._llm_calls

article_text = self._input.title + body
entities = []
FOR each entity in structured.output.output_items:
    stock = _verify_entity(entity, article_text)
    if stock is None:
        append UnverifiedEntity(entity.stock_code, entity.company_name, reason="not_in_hk_stock_list") to self._unverified
        continue
    outcome = AnalysisOutcome(stock_code=stock.stock_code, **entity's analysis fields)
    apply Business Constraint Validation (table below) to outcome
    if outcome.stock_code in self._verified_entities:
        log BusinessValidationError (duplicate stock_code, dropped)  # does not set failure_reason — same
        continue                                                          # non-fatal treatment as every other row
    self._verified_entities.append(outcome.stock_code)
    entities.append(VerifiedEntity(stock, outcome))
# keep first occurrence per stock_code — the LLM is instructed not to repeat one
# (system-design.md §9.4 Edge Case Rule 5), and two entities can verify to the same code

return EntityAnalysisResult(entities, skill_log=self._create_skill_log(), failure_reason=None)
```

Raises one of the typed exceptions (§7.2) on any failure above — `SkillBase.run()` (§8.1) catches them, not this method.

#### `_verify_entity(entity, article_text) -> Optional[Stock]`

Resolves one `ExtractedEntity` against `self._hk_stock_list_cache` (rationale: system-design.md §9.2):

```
code = entity.stock_code normalized per system-design.md §9.2 (trim, drop ".HK", zero-pad 2–5 digits to 5;
       anything else → no code)
entries = self._hk_stock_list_cache.entries
if code in entries:
    stock = entries[code]
    if name_matches_stock(entity.company_name, stock) or _code_printed(code, article_text):
        return stock
return lookup_stock(entity.company_name)   # §4.2 name search
```

`name_matches_stock()` (§4.2) uses the same normalization as the name search.

`_code_printed(code, article_text)`: true when the article prints the code in a code-shaped form — `(0700)`, `（00700）`, `00700.HK`, `Stock Code: 700`, `股份代號：700` — with leading zeros optional. A bare number elsewhere in the text never counts, and a bracketed number counts only with at least 3 digits, so a list marker such as `(1)` can't confirm a wrong code like `00001`.

`_create_skill_log()` maps `self`'s accumulated state onto `EntityAnalysisSkillLog` — called from both `llm_process()` (success) and `build_fail_result()` (failure).

```python
def _create_skill_log(self) -> EntityAnalysisSkillLog:
    return EntityAnalysisSkillLog(
        skill_version=ENTITY_SKILL_VERSION,
        verified_entities=self._verified_entities,
        unverified_entities=self._unverified,
        input_truncated=self._input_truncated,
        llm_calls=self._llm_calls,
        total_input_tokens=sum(c.input_tokens for c in self._llm_calls),
        total_output_tokens=sum(c.output_tokens for c in self._llm_calls),
        total_latency_ms=sum(c.latency_ms for c in self._llm_calls),
        llm_errors=self._llm_errors,
        timestamp=datetime.now(timezone.utc),
    )

def build_fail_result(self, err: LLMError) -> EntityAnalysisResult:
    return EntityAnalysisResult(entity_events=None, skill_log=self._create_skill_log(), failure_reason=err)
```

#### Business Constraint Validation (post-verification)

| Field | Constraint | On violation |
|---|---|---|
| `sentiment_score` | [0.0, 1.0] | Clip to boundary; log `BusinessValidationError` |
| `headline` | ≤20 chars | Truncate; log `BusinessValidationError` |
| `entity_summary` | ≤100 chars | Truncate; log `BusinessValidationError` |
| `event_type_secondary` | must differ from `event_type_primary` | Set to `null`; log `BusinessValidationError` |
| `stock_code` | must resolve through `_verify_entity()` | Dropped before validation, recorded in `unverified_entities` — not a truncation case |
| `stock_code` | no duplicates within one article | Keep the first occurrence, drop the rest; log `BusinessValidationError` |

Error handling (which exceptions get caught where, and what `EntityAnalysisService` does with each `failure_reason`) is covered once, in §8.5's cross-Skill table and §11.2's flowchart callouts — not repeated here.

---

### 8.3 `app/skills/brief_summary_skill.py` — BriefSummarySkill

Generates the Traditional-Chinese Morning Brief summary for one `Event`: from the event's constituent `EntityEvent` records, it selects and caps a subset of their `entity_summary` text (per-source and total-character caps, "Input construction" below) and makes a single LLM call producing `summary_short` (≤30 chars), `summary_full` (≤150 chars), and up to 3 `key_numbers`. Always returns a fully-populated result — schema violations fall back to an extractive, non-LLM summary rather than failing outright. `ScoringService` (§13) is its only caller.

#### Input Schema

```python
@dataclass
class BriefSummaryInput(SkillInput):   # base: app/skills/skill_base.py, §8.1
    event: Event
    constituent_entity_events: list[EntityEvent]
    source_weights: dict[str, float]   # authority weight per source_name (§13.5) — orders input
                                       # construction and picks the fallback headline; Skills do no I/O
```

#### System Prompt

Design rationale for the input-selection rules below ("Input construction") and the edge cases: system-design.md §9.5.

*Part 1 — Role and Task Definition:*
```
你是香港股票市場新聞摘要專家。你的任務是為已聚合的股票事件生成結構化摘要，
供專業投資者在晨報中快速閱讀。

所有輸出必須使用繁體中文，無論來源文章語言為何。
摘要必須基於輸入內容，不得添加輸入中未提及的信息或數據。
```

*Part 2 — Output Specification:*
```
【summary_short】≤30繁體中文字；包含核心事件+最重要數據（如有）；不得為純描述性語句
【summary_full】≤150繁體中文字；涵蓋事件背景、核心數據、潛在影響；整合多來源主要觀點
【key_numbers】最多3個；格式：數字+單位+方向（如適用）；例："廣告收入+32% YoY"
              只提取與 event_type_primary 直接相關的數據；無明確數字時輸出 []
```

*Part 3 — Few-shot Examples:* 3 examples covering financial data events, regulatory events without numbers, and conflicting signals (not reproduced here — same style as §8.2's four EntityAnalysisSkill examples).

*Part 4 — Edge Case Rules:*
```
1. 輸入內容無明確數字 → key_numbers 輸出 []，不得估算或捏造
2. 多來源報導角度存在明顯差異 → 在 summary_full 中整合主要觀點，不偏向單一來源
3. 來源文章為英文 → 所有輸出仍使用繁體中文，數字和專有名詞保持原格式
```

#### Output Schemas

`BriefSummary` is imported from `app.models.event` (§3.3); it is also the `output_schema` passed to `generate_structured()`.

```python
class BriefSummaryResult(SkillResult):   # base: app/skills/skill_base.py, §8.1
    model_config = {"arbitrary_types_allowed": True}   # failure_reason: an LLMError isn't natively Pydantic-validatable

    output: Optional[BriefSummary]   # None only when failure_reason is set
    skill_log: BriefSummarySkillLog   # always populated, every outcome (below)
    failure_reason: Optional[LLMError] = None          # LLMRateLimitError, LLMProviderError,
                                                         # or LLMAuthenticationError; None on
                                                         # success or fallback (§8.1)
```

#### Class: `BriefSummarySkill`

```python
class BriefSummarySkill(SkillBase):   # base: app/skills/skill_base.py, §8.1
    def __init__(self, input: BriefSummaryInput):
        super().__init__(input)   # sets self._input/_llm_calls/_llm_errors (§8.1)
        self._adapter = get_llm_adapter()   # §7.3
        self._input_summary, self._messages = build_prompt()   # Input construction, below — reads self._input
```

`run()` is inherited from `SkillBase` (§8.1) — not redefined here.

#### `llm_process()`

Only `LLMSchemaViolationError` is caught here, to run this Skill's own fallback (below) instead of failing — `LLMRateLimitError`/`LLMProviderError`/`LLMAuthenticationError` are left to propagate; `SkillBase.run()` (§8.1) catches those and builds the same classification/logging every Skill gets.

```
try:
    structured = generate_structured(self._messages, output_schema=BriefSummary, generation_config=_GENERATION_CONFIG)
    append LlmCallMetric(purpose="structured_output", ...from structured) to self._llm_calls
    output = structured.output
    apply Business Constraint Validation (table below) to output
    fallback, fallback_reason = False, None
except LLMSchemaViolationError as err:
    append LlmErrorEntry(call_index=len(self._llm_calls) + 1, error_type="LLMSchemaViolationError",
                          error_detail=str(err), retried=False) to self._llm_errors
    output = fallback_output()                            # Fallback, below — reads self._input
    fallback, fallback_reason = True, "schema_violation"
    log warning brief_summary_fallback

return BriefSummaryResult(output, skill_log=self._create_skill_log(fallback, fallback_reason), failure_reason=None)
```

#### `_create_skill_log()` / `build_fail_result()`

```python
def _create_skill_log(self, fallback: bool, fallback_reason: Optional[str]) -> BriefSummarySkillLog:
    return BriefSummarySkillLog(
        event_id=self._input.event.event_id,
        skill_version=BRIEF_SKILL_VERSION,
        fallback=fallback,
        fallback_reason=fallback_reason,
        input_summary=self._input_summary,
        llm_calls=self._llm_calls,
        total_input_tokens=sum(c.input_tokens for c in self._llm_calls),
        total_output_tokens=sum(c.output_tokens for c in self._llm_calls),
        total_latency_ms=sum(c.latency_ms for c in self._llm_calls),
        llm_errors=self._llm_errors,
        timestamp=datetime.now(timezone.utc),
    )

def build_fail_result(self, err: LLMError) -> BriefSummaryResult:
    return BriefSummaryResult(output=None, skill_log=self._create_skill_log(False, None), failure_reason=err)
```

#### Data Structures

```python
@dataclass
class InputSummary:
    entity_summary_count: int
    entity_summary_total_chars: int
    truncated_by_source_cap: bool
    truncated_by_char_cap: bool

@dataclass
class BriefSummarySkillLog:
    event_id: UUID
    skill_version: str
    fallback: bool
    fallback_reason: Optional[str]
    input_summary: InputSummary
    llm_calls: list[LlmCallMetric]
    total_input_tokens: int
    total_output_tokens: int
    total_latency_ms: int
    llm_errors: list[LlmErrorEntry]
    timestamp: datetime
```

**Input construction** — `build_prompt()`, called from `__init__()` above (design rationale: system-design.md §9.5). 【實體信息】: `stock_code`, `event_type_primary`, `event_type_secondary` from `input.event`; `company_name` from `input.constituent_entity_events[0].stock.company_name` (every constituent shares the Event's `stock_code`, §12.3). 【各文章實體摘要】 selection:

```
1. Per-source cap: retain at most 3 articles per source_name;
   when exceeded, keep the 3 with latest published_at, discard the rest.
2. Total character cap: sort retained articles by input.source_weights
   descending (P1 -> P2 -> P3; a source missing from it weighs 0.0;
   same weight by latest published_at first);
   accumulate entity_summary strings; discard an entry entirely once
   including it would push the total over 200 characters.
```

`input_summary` fields (`entity_summary_count`, `entity_summary_total_chars`, `truncated_by_source_cap`, `truncated_by_char_cap`) are recorded during this construction and included in the `brief_summary_completed` log event (system-design.md §9.8).

#### Business Constraint Validation (post-call)

Schema validation (§7.3) enforces only `BriefSummary`'s field types; the length/count limits are business rules enforced here (as in §8.2, §8.4):

| Field | Constraint | On violation |
|---|---|---|
| `summary_short` | ≤30 chars | Truncate to 30 chars; log `BusinessValidationError` |
| `summary_full` | ≤150 chars | Truncate to 150 chars; log `BusinessValidationError` |
| `key_numbers` | Max 3 items | Retain first 3; discard remainder; log `BusinessValidationError` |

Applied to the schema-validated `BriefSummary` right after `generate_structured()` returns.

#### Fallback (system-design.md §5.9, §9.7)

`fallback_output()`, called from `llm_process()` above:

```
summary_short = headline of the constituent EntityEvent whose source_name has the
                highest weight in input.source_weights (missing = 0.0); ties
                broken by latest published_at
summary_full   = summary_short
key_numbers    = []
return BriefSummary(summary_short, summary_full, key_numbers)
```

Processing continues — `llm_process()` returns normally with `skill_log.fallback=True` and a real (fallback) `output`, `failure_reason=None`, rather than raising.

---

### 8.4 `app/skills/event_scoring_skill.py` — EventLLMScoringSkill

#### Input Schema

```python
@dataclass
class EventLLMScoringInput(SkillInput):   # base: app/skills/skill_base.py, §8.1
    event: Event
    stock: Stock                     # §4.2 — constituent_entity_events[0].stock (§13.1 step 7); Event has no company name of its own
    brief_output: VersionedMetadata[BriefSummary]
    input_from_fallback: bool
```

#### System Prompt

Rationale (why direction is derived here rather than from `sentiment_label`, why this Skill reads `BriefSummarySkill`'s output instead of raw article text): system-design.md §9.6/§5.7.

*Part 1 — Role and Task Definition:*
```
你是香港股票市場影響力評估專家。你的任務是評估一則財經新聞事件對特定上市公司股價的潛在影響方向與幅度。
你的評估必須獨立於任何預設情緒判斷，直接基於事件內容對股價的實際影響進行分析。
```

*Part 2 — Scoring Reference:*
```
輸出 stock_impact_score，範圍 [-5, +5]：
+5：重大正面事件（顯著超預期業績、高溢價被收購）
+3：明確正面事件（派息增加、分析師升評、業績符合預期）
+1：輕微正面、影響有限
 0：常規公告、影響不明確、正負信號相互抵消
-1：輕微負面、影響有限
-3：明確負面事件（盈利預警、監管調查）
-5：存亡威脅（清盤風險、重大欺詐指控）

評分優先原則：
1. Forward-looking 信號（管理層指引、展望）優先於 backward-looking 數據
2. 明確財務數據優先於定性描述
3. key_numbers 中多個數據點方向一致時，信號強度相應提升
```

*Part 3 — Few-shot Examples:* 5 examples covering earnings beat, implicit negative guidance, conflicting signals, neutral boilerplate, and regulatory investigation (not reproduced here — same style as §8.2's four EntityAnalysisSkill examples).

*Part 4 — Output Constraints:*
```
adjustment_reason 要求：
- 繁體中文，≤80字
- 必須說明評分方向的主要依據
- 若存在衝突信號，必須說明哪個信號主導及理由
- 不得重複輸入內容，需提供判斷性陳述
```

#### Output Schemas

`LLMScore` is imported from `app.models.event_score` (§13.1); it is also the `output_schema` passed to `generate_structured()`.

```python
class EventLLMScoringResult(SkillResult):   # base: app/skills/skill_base.py, §8.1
    model_config = {"arbitrary_types_allowed": True}   # failure_reason: an LLMError isn't natively Pydantic-validatable

    output: Optional[LLMScore]   # None only when failure_reason is set
    skill_log: EventLLMScoringSkillLog   # always populated, every outcome (below)
    failure_reason: Optional[LLMError] = None          # LLMRateLimitError, LLMProviderError,
                                                         # or LLMAuthenticationError; None on
                                                         # success or fallback (§8.1)
```

#### Class: `EventLLMScoringSkill`

```python
class EventLLMScoringSkill(SkillBase):   # base: app/skills/skill_base.py, §8.1
    def __init__(self, input: EventLLMScoringInput):
        super().__init__(input)   # sets self._input/_llm_calls/_llm_errors (§8.1)
        self._adapter = get_llm_adapter()   # §7.3
        self._messages = build_prompt()   # Input construction, below — reads self._input
```

`run()` is inherited from `SkillBase` (§8.1). The Skill reads `input.brief_output` rather than raw article text (rationale: system-design.md §9.6).

#### `llm_process()`

Two distinct triggers reach this Skill's own fallback (below), both handled here rather than in `SkillBase.run()`: `LLMSchemaViolationError` (caught, like `BriefSummarySkill`, §8.3), and a non-error case — `adjustment_reason` coming back empty, which type-only schema validation doesn't catch. `LLMRateLimitError`/`LLMProviderError`/`LLMAuthenticationError` are left to propagate to `SkillBase.run()`.

```
try:
    structured = generate_structured(self._messages, output_schema=LLMScore, generation_config=_GENERATION_CONFIG)
    append LlmCallMetric(purpose="structured_output", ...from structured) to self._llm_calls
    output = structured.output
    apply Business Constraint Validation (table below) to output   # clip/truncate in place

    if output.adjustment_reason is empty:
        output = fallback_output()                        # Fallback, below
        fallback, fallback_reason = True, "empty_adjustment_reason"
        log warning event_scoring_fallback
    else:
        fallback, fallback_reason = False, None
except LLMSchemaViolationError as err:
    append LlmErrorEntry(call_index=len(self._llm_calls) + 1, error_type="LLMSchemaViolationError",
                          error_detail=str(err), retried=False) to self._llm_errors
    output = fallback_output()                            # Fallback, below
    fallback, fallback_reason = True, "schema_violation"
    log warning event_scoring_fallback

return EventLLMScoringResult(output, skill_log=self._create_skill_log(fallback, fallback_reason), failure_reason=None)
```

#### `_create_skill_log()` / `build_fail_result()`

```python
def _create_skill_log(self, fallback: bool, fallback_reason: Optional[str]) -> EventLLMScoringSkillLog:
    return EventLLMScoringSkillLog(
        event_id=self._input.event.event_id,
        skill_version=SCORING_SKILL_VERSION,
        fallback=fallback,
        fallback_reason=fallback_reason,
        input_from_fallback=self._input.input_from_fallback,
        llm_calls=self._llm_calls,
        total_input_tokens=sum(c.input_tokens for c in self._llm_calls),
        total_output_tokens=sum(c.output_tokens for c in self._llm_calls),
        total_latency_ms=sum(c.latency_ms for c in self._llm_calls),
        llm_errors=self._llm_errors,
        timestamp=datetime.now(timezone.utc),
    )

def build_fail_result(self, err: LLMError) -> EventLLMScoringResult:
    return EventLLMScoringResult(output=None, skill_log=self._create_skill_log(False, None), failure_reason=err)
```

#### Data Structures

```python
@dataclass
class EventLLMScoringSkillLog:
    """system-design.md §9.8's event_scoring_completed record."""
    event_id: UUID
    skill_version: str
    fallback: bool
    fallback_reason: Optional[str]
    input_from_fallback: bool
    llm_calls: list[LlmCallMetric]
    total_input_tokens: int
    total_output_tokens: int
    total_latency_ms: int
    llm_errors: list[LlmErrorEntry]
    timestamp: datetime
```

**Input construction** — `build_prompt()`, called from `__init__()` above (system-design.md §9.6): `stock_code`, `event_type_primary`, `event_type_secondary` from `input.event`; `company_name` from `input.stock.company_name`; `summary_full` and `key_numbers` from `input.brief_output.output`. `input.input_from_fallback` is recorded (not used in the prompt) purely for the `event_scoring_completed` log event, flagging a potentially-degraded input for monitoring.

#### Business Constraint Validation

| Field | Constraint | On violation |
|---|---|---|
| `stock_impact_score` | [-5.0, +5.0] | Clip to boundary; log `BusinessValidationError` |
| `adjustment_reason` | ≤80 chars | Truncate; log `BusinessValidationError` |
| `adjustment_reason` | non-empty | Trigger fallback (below); log warning |

The clip/truncate rows are applied to the schema-validated `LLMScore` right after `generate_structured()` returns. The third row is this Skill's fallback trigger (see `llm_process()`).

#### Fallback (system-design.md §5.9)

`fallback_output()`, called from `llm_process()` above:

```
stock_impact_score = 0
adjustment_reason  = ""
return LLMScore(stock_impact_score, adjustment_reason)
```

`direction` is **not** set here — it is derived later in Score Fusion (§13.4) from Sentiment Aggregation's `weighted_signed_score` whenever `stock_impact_score == 0` and `result.skill_log.fallback == True` (system-design.md §5.7's `llm_fallback`). Processing continues — `llm_process()` returns normally with `skill_log.fallback=True`, `failure_reason=None`, rather than raising.

---

### 8.5 Skill Error Handling Summary (system-design.md §9.7)

All three Skills catch the typed LLM exceptions internally and return a typed result (`EntityAnalysisResult`/`BriefSummaryResult`/`EventLLMScoringResult`, §8.2–§8.4), each subclassing `SkillResult` (§8.1). `EntityAnalysisResult` carries `skill_log`/`failure_reason` directly; the other two also wrap their schema-validated output in an `output` field. Callers inspect `failure_reason`/`fallback`, never a `try`/`except` around the call.

| Skill | `LLMRateLimitError` | `LLMProviderError` / `LLMRequestError` / `LLMAuthenticationError` | `LLMSchemaViolationError` |
|---|---|---|---|
| EntityAnalysisSkill | `failure_reason=err` (`LLMRateLimitError`), `output=None`; caller: backoff, no ACK | `failure_reason=err`, `output=None`; caller: no ACK | `failure_reason=err` (`LLMSchemaViolationError`), `output=None` (no fallback exists, system-design.md §9.7); caller: no ACK |
| BriefSummarySkill | `failure_reason=err` (`LLMRateLimitError`), `output=None`; caller: backoff, no ACK | `failure_reason=err`, `output=None`; caller: no ACK | Fallback applied; `output` set, `failure_reason=None`; continues |
| EventLLMScoringSkill | `failure_reason=err` (`LLMRateLimitError`), `output=None`; caller: backoff, no ACK | `failure_reason=err`, `output=None`; caller: no ACK | Fallback applied; `output` set, `failure_reason=None`; continues |

`LLMAuthenticationError` shares the `LLMProviderError` column because callers treat them identically (no ACK, no backoff); `failure_reason` still holds the actual error. It is a distinct type because its `log_level` is CRITICAL (§7.2).

### 8.6 Skill Completion Log Events

Each Skill builds a structured completion-log record into its result on every invocation (success, fallback, or `failure_reason` set); the calling Service logs it from `result.skill_log` via `app/logger.py`. `EntityAnalysisService` (§11.1) logs `entity_analysis_completed`, and `ScoringService` (§13) logs `brief_summary_completed`/`event_scoring_completed`. Field specs are each Skill's `*SkillLog` dataclass (§8.2–§8.4), sharing `LlmCallMetric`/`LlmErrorEntry` (§8.1); rationale: system-design.md §9.8. `lookup_stock` (§4.2) logs its own `lookup_stock_completed` independently.

---

## 9. Clients — `app/clients/`

Outbound HTTP clients to SADI (`NewsClient`) and Admin (`AdminClient`), using the module-level singleton + getter shape (§5.1).

#### `translate_client_errors` — `app/common/http_client.py`

Every client method that calls an upstream service is decorated with `@translate_client_errors(upstream="SADI", category=ErrorCategory.NEWS_CLIENT)`, so its body is straight-line code with no try/except and callers never import `httpx`:

- `httpx.HTTPError` (a network failure, a timeout, or the `raise_for_status()` error for a status the method doesn't handle itself) → `ServiceUnavailableError.upstream_unavailable(f"{upstream} request failed: {exc!r}", category=category)`: `COMMON-5002`, WARNING, since an outside outage needs no code or config change.
- pydantic `ValidationError` (a successful response without the expected shape) → `ParseError(f"{upstream} response shape mismatch: {exc!r}", category=category)`: `COMMON-6000`, ERROR, since the contract changed.

There is no per-client error class.

### 9.1 `app/clients/news_client.py` — `NewsClient`

```python
class NewsClient:
    def __init__(self, http_client: httpx.AsyncClient, base_url: str): ...

    @translate_client_errors(upstream="SADI", category=ErrorCategory.NEWS_CLIENT)
    async def fetch_cleaned_news(self, cleaned_id: UUID) -> Optional[CleanedNews]:
        """
        GET {base_url}/v1/cleaned_news/{cleaned_id} (docs/api.md §2.4).
        Returns None on HTTP 404 (COMMON-4004) — not an error, see system-design.md §3.6.
        Raises ServiceUnavailableError (COMMON-5002) on a network failure or any other
        non-404 error status, or ParseError (COMMON-6000) if the body doesn't match
        CleanedNews — the caller's error table (§11.4) treats both as a generic
        "SADI API failure".
        """
```

#### Data Structure: `CleanedNews` — `app/clients/news_client.py`

SAPI's model of a `GET /v1/cleaned_news/{cleaned_id}` response (docs/api.md §2.4). Validation aliases map `title_cleaned`/`body_cleaned` onto `title`/`body`. `cleaned_id` is carried so `find_entity_events()` can set `EntityEvent.source_id` (§11.1), which `persist_entities()` writes into `entity_events.metadata` (§10.1). `raw_id`/`created_at` are excluded. Unrelated to SADI's own `CleanedNews` class.

```python
class CleanedNews(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    cleaned_id: UUID
    title: str = Field(validation_alias="title_cleaned")
    body: str = Field(validation_alias="body_cleaned")
    source_url: str
    source_name: str
    published_at: Optional[AwareDatetime]   # null on the wire if unavailable in source (docs/api.md §2.4); a timestamp without a UTC offset is rejected
```

`fetch_cleaned_news()` returns `CleanedNews.model_validate_json(response.content)`; a null or wrong-typed field fails validation (→ `ParseError`, above).

`NewsClient` is a singleton + getter (§5.1):

```python
_news_client: Optional[NewsClient] = None

def get_news_client() -> NewsClient:
    if _news_client is None:
        raise ServiceUnavailableError("news client is not initialized")
    return _news_client

def create_news_client(http_client: httpx.AsyncClient, base_url: str) -> NewsClient:
    global _news_client
    _news_client = NewsClient(http_client, base_url)
    return _news_client
```

`create_news_client()` is called from `main.py` (§16) with the shared `http_client` (§4.2) and `config.entity_analysis.sadi_api_url`. No per-client timeout: it uses `HttpConfig.timeout_s` (§4.1), as does `AdminClient`.

### 9.2 `app/clients/admin_client.py` — `AdminClient`

```python
class AdminClient:
    def __init__(self, http_client: httpx.AsyncClient, base_url: str): ...

    @translate_client_errors(upstream="Admin", category=ErrorCategory.ADMIN_CLIENT)
    async def fetch_sources(self) -> list[dict]:
        """GET {base_url}/v1/sources (docs/api.md §4.3). Returns
        [{"source_name": ..., "authority_weight": ...}, ...] for all active sources.
        Raises ServiceUnavailableError (COMMON-5002) on a network failure or a non-2xx
        response, per §9's translate_client_errors."""
```

`AdminClient` is a singleton + getter, like `NewsClient` (§9.1):

```python
_admin_client: Optional[AdminClient] = None

def get_admin_client() -> AdminClient:
    if _admin_client is None:
        raise ServiceUnavailableError("admin client is not initialized")
    return _admin_client

def create_admin_client(http_client: httpx.AsyncClient, base_url: str) -> AdminClient:
    global _admin_client
    _admin_client = AdminClient(http_client, base_url)
    return _admin_client
```

`ScoringService`'s own use of `get_source_weights()`/`count_active_sources()` built on top of this client is covered in §13.5, alongside `compute_rule_score()` — the sole consumer of both.

---

## 10. Repository — `app/persistent/`

### 10.1 `app/persistent/entity_event_repository.py` — `EntityEventRepository`

#### Class: `EntityEventRepository`

Persistence split out of `EntityAnalysisService` (the same split as Service/Consumer, §11.1). A singleton + getter (§5.1), like `NewsClient`/`AdminClient`.

```python
class EntityEventRepository:
    def __init__(self, db: DatabaseClient):
        self._db = db
```

```python
_entity_event_repository: Optional[EntityEventRepository] = None

def get_entity_event_repository() -> EntityEventRepository:
    if _entity_event_repository is None:
        raise ServiceUnavailableError("entity event repository is not initialized")
    return _entity_event_repository

def create_entity_event_repository() -> EntityEventRepository:
    global _entity_event_repository
    _entity_event_repository = EntityEventRepository(get_db_client())
    return _entity_event_repository
```

#### Method: `persist_entities(entities: list[EntityEvent]) -> list[EntityEvent]`

Takes the `EntityEvent` list above, not `EntityAnalysisResult` — `EntityAnalysisSkill`'s own internal result type (§8.2) is never named here.

```
Purpose :
async with db.transaction() as tx:   # one article's rows commit together (§5.1, system-design.md §3.4)
  for entity in entities:
    entity.entity_event_id, entity.created_at = uuid.uuid4(), datetime.now(timezone.utc)
    inserted = await tx.execute_returning(_INSERT_ENTITY_EVENT_SQL, ...)
        # INSERT ... ON CONFLICT (source_url, stock_code) DO NOTHING RETURNING entity_event_id (§5.1)
        # no intermediate row object, values passed straight through
        # (same convention as EventRepository.persist_events()'s _UPSERT_EVENT_SQL call, §10.2)
    if inserted is None:
        entity.entity_event_id, entity.created_at = None, None
        # conflict — not published, dropped from the result

Returns : entities where entity_event_id is not None (i.e. not no-op'd by the conflict clause).
```

Module-level SQL constant `_INSERT_ENTITY_EVENT_SQL` lives at the top of `entity_event_repository.py` — not inlined inside the method body (same convention as SADI's `_INSERT_RAW_NEWS_SQL`).

For each `EntityEvent` in `entities`, the row is inserted as:

```python
new_id = uuid.uuid4()
now = datetime.now(timezone.utc)
llm = entity.analysis_outcome.output   # AnalysisOutcome — stock_code/event_type_primary/sentiment_label/sentiment_score/headline/entity_summary all live here, not on EntityEvent itself
additional_outcome_payload = {
    "version": entity.analysis_outcome.version,
    "output": {
        # these five keys only — llm's other four fields
        # (stock_code, event_type_primary, sentiment_label, sentiment_score)
        # already has its own flat column below, so it isn't repeated here
        "company_name_zh": entity.stock.company_name_zh,   # §4.2 — never from the LLM (system-design.md §9.4/§9.6);
        "company_name_en": entity.stock.company_name_en,   # read back by _row_to_entity_event() (below) to rebuild Stock
        "event_type_secondary": llm.event_type_secondary,
        "headline": llm.headline,
        "entity_summary": llm.entity_summary,
    },
}
values = {
    "entity_event_id": new_id,
    "exchange": entity.stock.exchange,
    "source_url": entity.source_url,
    "source_name": entity.source_name,
    "published_at": entity.published_at,
    "stock_code": entity.stock.stock_code,
    "event_type_primary": llm.event_type_primary,
    "sentiment_label": llm.sentiment_label,
    "sentiment_score": llm.sentiment_score,
    "headline": llm.headline,
    "entity_summary": llm.entity_summary,
    "additional_outcome": additional_outcome_payload,
    "metadata": {"cleaned_id": str(entity.source_id)},
    "created_at": now,
    "updated_at": now,
}
inserted = await tx.execute_returning(_INSERT_ENTITY_EVENT_SQL, *values.values())   # tx — the transaction opened above
```

`values`' key order must match `_INSERT_ENTITY_EVENT_SQL`'s `$1...$15` (asyncpg binds positionally).

`entity.analysis_outcome` is a `VersionedMetadata[AnalysisOutcome]` (§11.1), persisted under `additional_outcome`; `llm` above is its `.output`. `additional_outcome_payload` is hand-built (§3.2): a narrower projection than `AnalysisOutcome`'s seven fields, plus `company_name_zh`/`company_name_en`. `event_type_secondary` is the only field with no flat column; `exchange`/`stock_code` come from `entity.stock` (§4.2). The version comes from `entity.analysis_outcome.version`, set by `find_entity_events()` from `result.skill_log.skill_version`.

On success (`inserted` is not `None`): `entity.entity_event_id = new_id` and `entity.created_at = now`, and `entity` is kept in the returned list; on conflict (`inserted is None`), `entity` is dropped from the returned list and its `entity_event_id`/`created_at` stay unset. The conflict clause stays as a backstop only — a redelivered article never reaches this method once its rows exist (`find_entity_events()`'s step 3, §11.1).

#### Method: `fetch_entity_event_ids_by_source_url(source_url: str) -> list[UUID]`

`EntityAnalysisService.find_entity_events()`'s (§11.1) redelivery check — the `entity_event_id` of every row already persisted for one article. Returns ids only, not `EntityEvent`s: republishing needs nothing else. Served by `uq_entity_events_source_url_stock_code`'s leading column (§17), so no extra index.

```python
_SELECT_ENTITY_EVENT_IDS_BY_SOURCE_URL_SQL = "SELECT entity_event_id FROM entity_events WHERE source_url = $1"

async def fetch_entity_event_ids_by_source_url(self, source_url: str) -> list[UUID]:
    rows = await self._db.fetch_all(_SELECT_ENTITY_EVENT_IDS_BY_SOURCE_URL_SQL, source_url)
    return [row["entity_event_id"] for row in rows]
```

#### Method: `fetch_entity_event(entity_event_id: UUID) -> Optional[EntityEvent]`

The per-id read path into `entity_events`; `AggregationService.process_entity_event()` (§12.1) is its only caller.

```python
_SELECT_ENTITY_EVENT_SQL = "SELECT * FROM entity_events WHERE entity_event_id = $1"

def _row_to_entity_event(row: asyncpg.Record) -> EntityEvent:
    return EntityEvent(
        source_id=UUID(row["metadata"]["cleaned_id"]),
        source_url=row["source_url"],
        source_name=row["source_name"],
        published_at=row["published_at"],
        # AnalysisOutcome(**additional_outcome["output"]) would fail: that dict holds only five keys.
        # Rebuild field-by-field: flat columns for stock_code/event_type_primary/sentiment_*; only
        # event_type_secondary is JSONB-only; company_name_* feed `stock` below.
        analysis_outcome=VersionedMetadata(
            version=row["additional_outcome"]["version"],
            output=AnalysisOutcome(
                stock_code=row["stock_code"],
                event_type_primary=row["event_type_primary"],
                event_type_secondary=row["additional_outcome"]["output"]["event_type_secondary"],
                sentiment_label=row["sentiment_label"],
                sentiment_score=row["sentiment_score"],
                headline=row["headline"],
                entity_summary=row["entity_summary"],
            ),
        ),
        stock=Stock(   # §4.2 — rebuilt from the row alone, never from the live HKStockListCache (§11.1's EntityEvent notes)
            stock_code=row["stock_code"],
            exchange=row["exchange"],
            company_name_zh=row["additional_outcome"]["output"]["company_name_zh"],
            company_name_en=row["additional_outcome"]["output"]["company_name_en"],
        ),
        entity_event_id=row["entity_event_id"],
        created_at=row["created_at"],
    )

async def fetch_entity_event(self, entity_event_id: UUID) -> Optional[EntityEvent]:
    row = await self._db.fetch_one(_SELECT_ENTITY_EVENT_SQL, entity_event_id)
    return _row_to_entity_event(row) if row is not None else None
```

`source_id` comes from the `metadata` JSONB (`{"cleaned_id": ...}`); `entity_events` has no `source_id` column. `fetch_entity_events_for_event()` below shares `_row_to_entity_event()`.

> **`published_at` is returned as stored, `NULL` included** (nullable, system-design.md §10.1). The fallback to `created_at` belongs to `merge_or_create()` (§12.4), the one place it feeds business logic (rule 9).

#### Method: `fetch_entity_events_for_event(event_id: UUID) -> list[EntityEvent]`

`ScoringService.score_event()` (§13.1)'s `constituent_entity_events` — every `EntityEvent` an `event_id` was ever merged from, via the `event_entity_map` join. Reuses `_row_to_entity_event()` above; only the query differs.

```python
_SELECT_ENTITY_EVENTS_FOR_EVENT_SQL = """
    SELECT ee.* FROM entity_events ee
    JOIN event_entity_map m ON m.entity_event_id = ee.entity_event_id
    WHERE m.event_id = $1
"""

async def fetch_entity_events_for_event(self, event_id: UUID) -> list[EntityEvent]:
    rows = await self._db.fetch_all(_SELECT_ENTITY_EVENTS_FOR_EVENT_SQL, event_id)
    return [_row_to_entity_event(row) for row in rows]
```

### 10.2 `app/persistent/event_repository.py` — `EventRepository`

#### Class: `EventRepository`

Persistence for `events`/`event_entity_map`, split out of `AggregationService` (§10.1's rationale).

```python
class EventRepository:
    def __init__(self, db: DatabaseClient):
        self._db = db
```

```python
_event_repository: Optional[EventRepository] = None

def get_event_repository() -> EventRepository:
    if _event_repository is None:
        raise ServiceUnavailableError("event repository is not initialized")
    return _event_repository

def create_event_repository() -> EventRepository:
    global _event_repository
    _event_repository = EventRepository(get_db_client())
    return _event_repository
```

#### Method: `persist_events(active: list[Event]) -> list[Event]`

Takes the `Event` (Redis-active stage, §3.3) that `EventAggregationStore.scan_all()` (§12.5) returns, and returns the fully-populated `Event`, like `persist_entities()` (§10.1). Field treatments:
- `created_at` — never written by this UPSERT at all; the row's original `INSERT` is the only thing that ever sets it.
- `aggregation_updated_at` — written as given by `merge_or_create()` (§12.4); the UPSERT never computes it.
- `updated_at` — always self-calculated as `now()` on every write that actually happens (an unchanged row isn't written at all — below) — a generic "last touched by any write" audit column, unrelated to `aggregation_updated_at`'s specific staleness-detection meaning.
- `event_type_secondary`/`source_list` — `COALESCE`d: the input value if not `NULL`, else computed by a subquery against `entity_events` (below). `AggregationService.flush()` always passes `None`. `Event.source_count` is `len(source_list)` (§3.3); there is no column (§17).

```
Purpose : One DB transaction per call: UPSERT every given Event into
          `events` (field treatments above), INSERT its entity_event_ids
          into `event_entity_map`, and return the fully-populated Event
          rows actually written — a new row, or an existing one whose
          stored aggregation_updated_at is older than the input's. An Event
          whose aggregation_updated_at is unchanged — or older than what's
          stored — is a no-op and is not returned. AggregationService (§12.1) decides *when* to call this
          and what to publish afterward — this method has no opinion on
          timing or on what happens after commit.
```

```python
_SELECT_EVENT_SQL = "SELECT * FROM events WHERE event_id = $1"

def _row_to_event(row: asyncpg.Record) -> Event:
    return Event(
        event_id=row["event_id"],
        exchange=row["exchange"],
        stock_code=row["stock_code"],
        event_type_primary=row["event_type_primary"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        event_type_secondary=row["event_type_secondary"],
        source_list=[SourceListItem(**item) for item in row["source_list"]],   # source_count derives from this at read time too — no row["source_count"] column exists (§3.3)
        brief_summary=VersionedMetadata(version=row["brief_summary"]["version"], output=BriefSummary(**row["brief_summary"]["output"]))
                     if row["brief_summary"] is not None else None,
        brief_summary_aggregation_updated_at=datetime.fromisoformat(row["brief_summary"]["aggregation_updated_at"])
                     if row["brief_summary"] is not None else None,
        brief_summary_fallback=row["brief_summary"]["fallback"] if row["brief_summary"] is not None else None,
        aggregation_updated_at=row["aggregation_updated_at"],
        updated_at=row["updated_at"],
        created_at=row["created_at"],
        # entity_event_ids stays None — nothing reads it back off a persisted Event (§3.3)
    )

async def persist_events(self, active: list[Event]) -> list[Event]:
    flushed_events = []
    now = datetime.now(timezone.utc)
    async with self._db.transaction() as tx:
        for e in active:
            values = {
                "event_id": e.event_id,
                "exchange": e.exchange,
                "stock_code": e.stock_code,
                "event_type_primary": e.event_type_primary,
                "first_seen_at": e.first_seen_at,
                "last_seen_at": e.last_seen_at,
                "aggregation_updated_at": e.aggregation_updated_at,
                "event_type_secondary": e.event_type_secondary,
                "source_list": [dataclasses.asdict(item) for item in e.source_list] if e.source_list is not None else None,
                "entity_event_ids": e.entity_event_ids,
            }
            row = await tx.execute_returning(_UPSERT_EVENT_SQL, *values.values())   # None: input not newer than the stored row, no write
            for entity_event_id in e.entity_event_ids:
                mapping = EventEntityMap(event_id=e.event_id, entity_event_id=entity_event_id, created_at=now)   # §3.4
                await tx.execute(_UPSERT_EVENT_ENTITY_MAP_SQL, mapping.event_id, mapping.entity_event_id, mapping.created_at)
            if row is not None:
                flushed_events.append(_row_to_event(row))
    return flushed_events
```

`values`' key order must match `_UPSERT_EVENT_SQL`'s `$1...$10` (§10.1). `_UPSERT_EVENT_SQL` and `_UPSERT_EVENT_ENTITY_MAP_SQL` are module-level constants. `_UPSERT_EVENT_SQL` is `INSERT ... ON CONFLICT (event_id) DO UPDATE SET ... WHERE events.aggregation_updated_at < EXCLUDED.aggregation_updated_at RETURNING *`: only newer state wins, so an unchanged Event or an older snapshot returns no row (system-design.md §4.5). `SET`: `aggregation_updated_at = $n` (from `merge_or_create()`); `updated_at = now()`; `event_type_secondary = COALESCE($n, <computed>)` and `source_list = COALESCE($n, <computed>)`, where both subqueries filter `entity_events WHERE entity_event_id = ANY($n)`, with `$n` bound to `values["entity_event_ids"]` and **not** a join through `event_entity_map`: the `events` UPSERT runs before the map INSERTs (the map's FK needs the `events` row first, §17), so on a first flush a join would see zero rows. `created_at` is absent from `SET`, so re-flushes preserve it. `RETURNING *` lets `_row_to_event()` build the `Event` without a follow-up `SELECT`.

`event_type_secondary`'s `<computed>` aggregation (PRD §OQ-8, Closed): every entity in the group shares the same `event_type_primary` — that's the merge key (`GroupCoroutine`'s `group_key`, §12.3) — so `entity.event_type_primary` is never a secondary-type candidate; only each constituent `entity_events.additional_outcome.output.event_type_secondary` (nullable, at most one per entity, system-design.md §9.4/OQ-8) is. Count frequency of each distinct non-`NULL` value among the matched rows, take the top 2. OQ-8 covers per-article ties (none possible — one value per entity) but not an Event-level frequency tie between two distinct types with only one slot left; that residual case is resolved here as most-recent `published_at` wins.

`source_list`'s `<computed>` aggregation: one entry per distinct `source_name` among the matched rows (§3.3's own definition). When the same source reports more than one constituent entity, its entry carries that source's most-recent `published_at`/`source_url` — the freshest report from that source, not the first.

> **Idempotency across flushes** (system-design.md §11.2's note): `entity_event_ids` in Redis is never cleared after a flush, so the same `(event_id, entity_event_id)` pair is re-attempted on every subsequent flush of that still-active group — `INSERT ... ON CONFLICT DO NOTHING` on `event_entity_map` absorbs this by design.

#### Method: `fetch_event(event_id: UUID) -> Optional[Event]`

`ScoringService.score_event()` (§13.1)'s only read of an `Event` row — reuses `_row_to_event()` above, the same row-shape reconstruction `persist_events()` uses after its own UPSERT.

```python
async def fetch_event(self, event_id: UUID) -> Optional[Event]:
    row = await self._db.fetch_one(_SELECT_EVENT_SQL, event_id)
    return _row_to_event(row) if row is not None else None
```

#### Method: `fetch_events(event_ids: list[UUID]) -> list[Event]`

`CacheService.build_new_version()`'s (§14.1) batch read of the `Event` half of each candidate, kept separate from `EventScoreRepository.fetch_scores()` (§10.3) so each repository stays inside its own table (rule 9). It fetches exactly the `event_id`s given; there is deliberately no full-scan mode.

```python
_SELECT_EVENTS_SQL = "SELECT * FROM events WHERE event_id = ANY($1)"

async def fetch_events(self, event_ids: list[UUID]) -> list[Event]:
    rows = await self._db.fetch_all(_SELECT_EVENTS_SQL, event_ids)
    return [_row_to_event(row) for row in rows]
```

#### Method: `fetch_aggregation_updated_at(event_id: UUID) -> Optional[datetime]`

The single-column re-fetch `ScoringService._is_stale()` (§13.4) uses at each staleness checkpoint — cheaper than `fetch_event()` above for a check that only ever looks at one column.

```python
_SELECT_AGGREGATION_UPDATED_AT_SQL = "SELECT aggregation_updated_at FROM events WHERE event_id = $1"

async def fetch_aggregation_updated_at(self, event_id: UUID) -> Optional[datetime]:
    row = await self._db.fetch_one(_SELECT_AGGREGATION_UPDATED_AT_SQL, event_id)
    return row["aggregation_updated_at"] if row is not None else None
```

#### Method: `fetch_unpublished_events(event_ids: list[UUID]) -> list[Event]`

`AggregationService._persist_and_publish()`'s (§12.1) outbox read — among the given `event_ids` only, the rows whose `aggregation_updated_at` hasn't been published yet. Never a table-wide read: the caller always passes the Events it just persisted. Faithful filter on two columns; deciding to publish them is the caller's. Served by the primary key.

```python
_SELECT_UNPUBLISHED_EVENTS_SQL = """
    SELECT * FROM events
    WHERE event_id = ANY($1)
      AND (published_aggregation_updated_at IS NULL
           OR published_aggregation_updated_at < aggregation_updated_at)
"""

async def fetch_unpublished_events(self, event_ids: list[UUID]) -> list[Event]:
    rows = await self._db.fetch_all(_SELECT_UNPUBLISHED_EVENTS_SQL, event_ids)
    return [_row_to_event(row) for row in rows]
```

#### Method: `mark_published(event_id: UUID, aggregation_updated_at: datetime) -> None`

Records that `aggregation_updated_at`'s version of `event_id` was published. Only moves forward, so marking an older version never un-marks a newer one, and a newer write that landed after the caller's read stays pending.

```python
_MARK_PUBLISHED_SQL = """
    UPDATE events SET published_aggregation_updated_at = $2
    WHERE event_id = $1
      AND (published_aggregation_updated_at IS NULL OR published_aggregation_updated_at < $2)
"""

async def mark_published(self, event_id: UUID, aggregation_updated_at: datetime) -> None:
    await self._db.execute(_MARK_PUBLISHED_SQL, event_id, aggregation_updated_at)
```

#### Method: `fetch_mapped_event_id(entity_event_id: UUID) -> Optional[UUID]`

The `event_id` an EntityEvent has already been flushed into, or `None` — `AggregationService.process_entity_event()`'s duplicate check (§12.1) reads it; deciding what a non-`None` result means is that caller's business, not this method's (rule 9). Served by `uq_event_entity_map_entity_event_id` (§17).

```python
_SELECT_MAPPED_EVENT_ID_SQL = "SELECT event_id FROM event_entity_map WHERE entity_event_id = $1"

async def fetch_mapped_event_id(self, entity_event_id: UUID) -> Optional[UUID]:
    row = await self._db.fetch_one(_SELECT_MAPPED_EVENT_ID_SQL, entity_event_id)
    return row["event_id"] if row is not None else None
```

#### Method: `update_brief_summary(event_id: UUID, brief_summary: VersionedMetadata[BriefSummary], aggregation_updated_at: datetime, fallback: bool) -> None`

`ScoringService.score_event()` step 5 (§13.1). Unconditional: once `BriefSummarySkill` succeeds the write happens, regardless of what `_is_stale()` (step 6) finds.

```python
_UPDATE_BRIEF_SUMMARY_SQL = "UPDATE events SET brief_summary = $2, updated_at = now() WHERE event_id = $1"

async def update_brief_summary(self, event_id: UUID, brief_summary: VersionedMetadata[BriefSummary],
                               aggregation_updated_at: datetime, fallback: bool) -> None:
    payload = {
        "version": brief_summary.version,
        "aggregation_updated_at": aggregation_updated_at.isoformat(),   # which Event version this summary describes
        "fallback": fallback,                                           # read back as Event.brief_summary_fallback
        "output": {
            "summary_short": brief_summary.output.summary_short,
            "summary_full": brief_summary.output.summary_full,
            "key_numbers": brief_summary.output.key_numbers,
        },
    }
    await self._db.execute(_UPDATE_BRIEF_SUMMARY_SQL, event_id, payload)
```

### 10.3 `app/persistent/event_score_repository.py` — `EventScoreRepository`

#### Class: `EventScoreRepository`

Persistence for `event_scores`, split out of `ScoringService` (§10.1's rationale). `persist_score_if_not_stale()` tells `score_event()` (§13.1) whether the write landed.

```python
class EventScoreRepository:
    def __init__(self, db: DatabaseClient):
        self._db = db
```

```python
_event_score_repository: Optional[EventScoreRepository] = None

def get_event_score_repository() -> EventScoreRepository:
    if _event_score_repository is None:
        raise ServiceUnavailableError("event score repository is not initialized")
    return _event_score_repository

def create_event_score_repository() -> EventScoreRepository:
    global _event_score_repository
    _event_score_repository = EventScoreRepository(get_db_client())
    return _event_score_repository
```

#### Method: `persist_score_if_not_stale(score: EventScore, expected_aggregation_updated_at: datetime) -> Optional[EventScore]`

Takes and returns `EventScore` (§13.1), like `persist_events()` (§10.2). A genuine INSERT-or-UPDATE: `event_scores` has no row until an Event's first score, and every later re-score (Aggregation republishes whenever an Event's state changes, §12.1/§12.2) goes through the same call. `stock_impact_score` is its own column (§17), promoted out of `llm_score_detail.output`.

`expected_aggregation_updated_at` is a separate parameter: it is a concurrency guard on the caller's read of `events`, not part of the score. `updated_at`/`created_at` on `score` are never read; the SQL computes its own `now()`.

```
Purpose : INSERT a new event_scores row, or UPDATE the existing one on
          conflict, guarded either way by events.aggregation_updated_at
          still matching expected_aggregation_updated_at (system-design.md §5.5's
          optimistic concurrency check). score.score_id/scored_at are
          used as given on the INSERT branch; score_id is left out of
          the ON CONFLICT DO UPDATE SET clause entirely, so a re-score
          preserves the row's original score_id — same treatment
          EventRepository.persist_events() (§10.2) gives events.created_at.
          scored_at *does* update on conflict (EXCLUDED.scored_at) — a
          re-score genuinely happened at a new time, unlike score_id
          which names a row that already exists.
Returns : The written EventScore (inserted or updated) — caller publishes
          stream:event_scored, then returns normally. None if nothing was
          written — expected_aggregation_updated_at no longer matches
          (the Event was re-aggregated mid-scoring); caller does not
          publish, does not raise, just returns normally (§13.1) — both
          outcomes are ACKed identically by ScoringConsumer (§13.2).
```

```python
_UPSERT_EVENT_SCORE_SQL = """
    INSERT INTO event_scores (score_id, event_id, base_rule_score, abs_final_score,
        stock_impact_score, direction, llm_fallback, rule_score_detail, llm_score_detail,
        scored_at, scored_aggregation_updated_at, updated_at, created_at)
    SELECT $9, events.event_id, $2, $3, $4, $5, $6, $7, $8, $10, $11, now(), now()
    FROM events
    WHERE events.event_id = $1 AND events.aggregation_updated_at = $11
    ON CONFLICT (event_id) DO UPDATE SET
        base_rule_score = EXCLUDED.base_rule_score,
        abs_final_score = EXCLUDED.abs_final_score,
        stock_impact_score = EXCLUDED.stock_impact_score,
        direction = EXCLUDED.direction,
        llm_fallback = EXCLUDED.llm_fallback,
        rule_score_detail = EXCLUDED.rule_score_detail,
        llm_score_detail = EXCLUDED.llm_score_detail,
        scored_at = EXCLUDED.scored_at,
        scored_aggregation_updated_at = EXCLUDED.scored_aggregation_updated_at,
        updated_at = now()
    RETURNING *
"""

def _row_to_event_score(row: asyncpg.Record) -> EventScore:
    return EventScore(
        score_id=row["score_id"],
        event_id=row["event_id"],
        abs_final_score=row["abs_final_score"],
        base_rule_score=row["base_rule_score"],
        stock_impact_score=row["stock_impact_score"],
        direction=row["direction"],
        llm_fallback=row["llm_fallback"],
        rule_score_detail=VersionedMetadata(version=row["rule_score_detail"]["version"], output=RuleScore(**row["rule_score_detail"]["output"])),
        llm_score_detail=VersionedMetadata(version=row["llm_score_detail"]["version"], output=LLMScore(**row["llm_score_detail"]["output"])),
        scored_at=row["scored_at"],
        scored_aggregation_updated_at=row["scored_aggregation_updated_at"],
        updated_at=row["updated_at"],
        created_at=row["created_at"],
    )

async def persist_score_if_not_stale(self, score: EventScore, expected_aggregation_updated_at: datetime) -> Optional[EventScore]:
    values = {
        "event_id": score.event_id,
        "base_rule_score": score.base_rule_score,
        "abs_final_score": score.abs_final_score,
        "stock_impact_score": score.stock_impact_score,
        "direction": score.direction,
        "llm_fallback": score.llm_fallback,
        "rule_score_detail": {
            "version": score.rule_score_detail.version,
            "output": {
                "event_type_score": score.rule_score_detail.output.event_type_score,
                "source_authority_score": score.rule_score_detail.output.source_authority_score,
                "sentiment_strength_score": score.rule_score_detail.output.sentiment_strength_score,
                "source_heat_score": score.rule_score_detail.output.source_heat_score,
            },
        },
        "llm_score_detail": {
            "version": score.llm_score_detail.version,
            "output": {
                "stock_impact_score": score.llm_score_detail.output.stock_impact_score,
                "adjustment_reason": score.llm_score_detail.output.adjustment_reason,
            },
        },
        "score_id": score.score_id,
        "scored_at": score.scored_at,
        "expected_aggregation_updated_at": expected_aggregation_updated_at,
    }
    row = await self._db.execute_returning(_UPSERT_EVENT_SCORE_SQL, *values.values())
    return _row_to_event_score(row) if row is not None else None
```

`values`' key order matches `_UPSERT_EVENT_SCORE_SQL`'s `$1...$11` (§10.1). The staleness gate works through `execute_returning()` (§5.1): when `events.aggregation_updated_at` no longer matches, the `SELECT ... FROM events WHERE ...` feeding the `INSERT` yields zero rows, nothing is written, and `RETURNING` yields `None`.

#### Method: `fetch_scored_aggregation_updated_at(event_id: UUID) -> Optional[datetime]`

The version of the Event the stored score was computed for, or `None` if the Event has never been scored. `ScoringService.score_event()`'s already-scored check (§13.1 step 1) reads it; deciding what a match means is the caller's business (rule 9). Single-column read, served by `uq_event_scores_event_id` (§17).

```python
_SELECT_SCORED_AGGREGATION_UPDATED_AT_SQL = "SELECT scored_aggregation_updated_at FROM event_scores WHERE event_id = $1"

async def fetch_scored_aggregation_updated_at(self, event_id: UUID) -> Optional[datetime]:
    row = await self._db.fetch_one(_SELECT_SCORED_AGGREGATION_UPDATED_AT_SQL, event_id)
    return row["scored_aggregation_updated_at"] if row is not None else None
```

#### Method: `fetch_scores(event_ids: Optional[list[UUID]] = None, page_size: int = 1000) -> list[EventScore]`

`CacheService`'s (§14.1) read of which Events have been scored: `event_ids=None` is the full fetch (only `_build_full()`), and a list is the incremental fetch (`build_new_version()`; `page_size` unused). Reads only `event_scores` (no join); `CacheService` fetches the `Event` rows via `EventRepository.fetch_events()` (§10.2). Reuses `_row_to_event_score()`.

The full fetch is keyset-paginated on `event_id` (indexed via `uq_event_scores_event_id`, §17): `event_scores` grows over the service's lifetime, and keyset pagination costs the same on every page, unlike `OFFSET`. `CacheService` passes `self._config.full_fetch_page_size` (`CACHE_FULL_FETCH_PAGE_SIZE`, §4.1); the default is `1000`.

```python
_SELECT_EVENT_SCORES_BY_IDS_SQL = "SELECT * FROM event_scores WHERE event_id = ANY($1)"
_SELECT_EVENT_SCORES_PAGE_SQL = "SELECT * FROM event_scores WHERE event_id > $1 ORDER BY event_id LIMIT $2"

async def fetch_scores(self, event_ids: Optional[list[UUID]] = None, page_size: int = 1000) -> list[EventScore]:
    if event_ids is not None:
        rows = await self._db.fetch_all(_SELECT_EVENT_SCORES_BY_IDS_SQL, event_ids)
        return [_row_to_event_score(row) for row in rows]

    results = []
    last_id = UUID(int=0)   # smaller than any real UUID — the first page starts here
    while True:
        page = await self._db.fetch_all(_SELECT_EVENT_SCORES_PAGE_SQL, last_id, page_size)
        results.extend(_row_to_event_score(row) for row in page)
        if len(page) < page_size:
            break
        last_id = page[-1]["event_id"]
    return results
```

---

### 10.4 `app/persistent/cache_store.py` — `CacheStore`

Owns the `morning_brief:*` key names, built on `RedisStateClient`'s generic primitives (§6.2). It lives in `app/persistent/` as a data-access component like the DB repositories; `CacheService` (§14.1) is its only consumer.

```python
_VERSION_COUNTER_KEY = "morning_brief:version_counter"
_CURRENT_VERSION_KEY = "morning_brief:current_version"
_LAST_UPDATED_KEY = "morning_brief:last_updated"

def _version_key(version: int) -> str:
    return f"morning_brief:v{version}"

@dataclass(frozen=True)
class CachePointer:
    """The current cache version and the time it was built, read together (get_current_pointer())."""
    version: int
    last_updated: datetime

class CacheStore:
    def __init__(self):
        self._state_client = get_redis_state_client()   # §6.2

    async def get_current_version(self) -> Optional[int]:
        """GET morning_brief:current_version; None before the first successful cache build."""
        raw = await self._read(_CURRENT_VERSION_KEY)
        return int(raw) if raw is not None else None

    async def _read(self, key: str) -> Optional[str]:
        """GET, with Redis connection failures translated to ServiceUnavailableError
        (§15.1) — so GET /v1/morning-brief answers 503 COMMON-5001 (api.md §3.3), not a
        500 from an untranslated redis exception. Same translation DatabaseClient applies
        to asyncpg (§5.2). The single-key read methods go through it; get_current_pointer()
        applies the same translation to its MGET."""
        try:
            return await self._state_client.get(key)
        except (redis.ConnectionError, redis.TimeoutError) as err:
            raise ServiceUnavailableError(f"Redis unreachable reading {key}: {err}") from err

    async def incr_version_counter(self) -> int:
        """Atomic INCR morning_brief:version_counter; returns the new version number."""
        return await self._state_client.incr(_VERSION_COUNTER_KEY)

    async def write_cache_version(self, version: int, events_json: str, updated_at: datetime, old_version: Optional[int]) -> None:
        """
        Atomic pipeline (multi_exec, §6.2): SET morning_brief:v{version},
        SET current_version, DEL morning_brief:v{old_version} (omitted if
        old_version is None — first-ever build), SET last_updated. This is
        the "Atomic Pipeline" step of system-design.md §6.5 — readers never observe a
        version between "new written" and "pointer switched", and v{old} is
        never visible as deleted before the pointer has already moved off it.
        """
        def _build(pipe):
            pipe.set(_version_key(version), events_json)
            pipe.set(_CURRENT_VERSION_KEY, str(version))
            if old_version is not None:
                pipe.delete(_version_key(old_version))
            pipe.set(_LAST_UPDATED_KEY, updated_at.isoformat())
        await self._state_client.multi_exec(_build)

    async def read_cache_version(self, version: int) -> Optional[str]:
        """GET morning_brief:v{version} — raw JSON string, parsed by the caller."""
        return await self._read(_version_key(version))

    async def get_current_pointer(self) -> Optional[CachePointer]:
        """MGET current_version + last_updated in one atomic read — they're written in the same
        MULTI/EXEC, so the pair always matches. None before the first successful build. The
        API's read path (§15.3); get_current_version() stays for the build, which has no
        timestamp to report."""
        try:
            version_raw, updated_raw = await self._state_client.mget([_CURRENT_VERSION_KEY, _LAST_UPDATED_KEY])
        except (redis.ConnectionError, redis.TimeoutError) as err:
            raise ServiceUnavailableError(f"Redis unreachable reading the cache pointer: {err}") from err
        if version_raw is None:
            return None
        return CachePointer(version=int(version_raw), last_updated=datetime.fromisoformat(updated_raw))

    async def scan_orphaned_versions(self, current_version: int) -> list[str]:
        """Version keys strictly older than current_version — feeds the periodic orphan
        cleanup coroutine (§14.6, system-design.md §12.1). Two guards:
        - the pattern requires a digit after "v", so morning_brief:version_counter (which
          morning_brief:v* would also match) is never returned;
        - only versions *below* the current_version the caller read are returned. A build
          can commit between that read and this scan, making a newer version current;
          versions only increase, so anything at or above the value read may be current
          and is left for a later sweep."""
        keys = await self._state_client.scan("morning_brief:v[0-9]*")
        return [k for k in keys if int(k.removeprefix("morning_brief:v")) < current_version]
```

`CacheStore` is process-wide infrastructure (built on `RedisStateClient`, no constructor args of its own) — module-level singleton + getter (§5.1), same shape as `EventAggregationStore` (§12.5):

```python
_cache_store: Optional[CacheStore] = None

def get_cache_store() -> CacheStore:
    if _cache_store is None:
        raise ServiceUnavailableError("cache store is not initialized")
    return _cache_store

def create_cache_store() -> CacheStore:
    global _cache_store
    _cache_store = CacheStore()
    return _cache_store
```

> `write_cache_version()` takes `old_version` explicitly; `_write_new_version()` (§14.5) passes it.

---

## 11. Entity Analysis Layer — `app/entity_analysis/`

### 11.1 `app/entity_analysis/entity_analysis_service.py` — `EntityAnalysisService`

`EntityAnalysisService` is pure business logic: `find_entity_events()` only. `CleanedNewsConsumer` (§11.2) owns consumption of `stream:raw_news_cleaned` (consumer/reclaim loops, dedup guard, `WorkerPool` dispatch, ACK/backoff/dead-letter) and reacts to how `find_entity_events()` returns: normally (ACK); `LLMRateLimitError` (no ACK, backoff); any other `LLMError` (no ACK, logged at its own level); anything else (no ACK, unanticipated bug, logged ERROR, §11.2).

Failures are exceptions rather than a status value because `find_entity_events()` can't enumerate every failure. Each Skill `failure_reason` is re-raised as itself (`raise result.failure_reason`), so the consumer distinguishes `LLMRateLimitError` (needs `trigger_backoff()`) from other `LLMError`s and logs it. A non-`LLMError` (SADI fetch failure, DB write failure) is not handled here.

#### Class: `EntityAnalysisService`

```python
class EntityAnalysisService:
    def __init__(self, config: EntityAnalysisConfig):
        self._news_client = get_news_client()               # §9.1
        self._redis_stream = get_redis_stream_client()     # §6.1 — publish only, never consumes here
        self._repo = get_entity_event_repository()          # §10.1
        self._config = config

    async def find_entity_events(self, cleaned_id: UUID) -> list[EntityEvent]:
        """Pure business logic for one cleaned_id — see Purpose below."""
```

`EntityAnalysisService` is a singleton + getter (§5.1):

```python
_entity_analysis_service: Optional[EntityAnalysisService] = None

def get_entity_analysis_service() -> EntityAnalysisService:
    if _entity_analysis_service is None:
        raise ServiceUnavailableError("entity analysis service is not initialized")
    return _entity_analysis_service

def create_entity_analysis_service(config: EntityAnalysisConfig) -> EntityAnalysisService:
    global _entity_analysis_service
    _entity_analysis_service = EntityAnalysisService(config)
    return _entity_analysis_service
```

`create_entity_analysis_service()` is called from `main.py` (§16) with `config.entity_analysis`.

#### Method: `find_entity_events(cleaned_id: UUID) -> list[EntityEvent]`

```
Purpose : 
1. article = await news_client.fetch_cleaned_news (cleaned_id)

2. article is None: SADI 404 (silently omitted per api.md §2.5) — not an error (system-design.md §3.6); log warning; return [].

3. existing_ids = await self._repo.fetch_entity_event_ids_by_source_url(article.source_url) (§10.1). Non-empty: a redelivery after this article was already persisted (system-design.md §3.4) — skip the Skill, publish stream:entity_event_completed for each id in existing_ids, log entity_analysis_republished (below), return [] (nothing was newly persisted; CleanedNewsConsumer only needs a normal return to ACK, §11.2).

4. result = await EntityAnalysisSkill(EntityAnalysisInput(article.title, article.body), self._config).run() — a fresh instance per call (§8.2).

5. result.failure_reason set: log entity_analysis_completed (below, entity_event_ids=[]); raise result.failure_reason — the original exception, re-raised as itself in every case.

6. result.failure_reason is None (incl. entity_events=[]): build one EntityEvent per item (below), reading source_id/source_url/source_name/published_at off article (still a separate CleanedNews variable — never nested onto EntityEvent itself), and stock = item.stock, the listed Stock the Skill verified the item against (§8.2); {
    7. self._repo.persist_entities() (§10.1) writes all of them in one transaction, each via INSERT ... ON CONFLICT (source_url, stock_code)
       DO NOTHING RETURNING entity_event_id, setting entity_event_id onto each EntityEvent actually inserted and filtering out the rest;

    8. publish stream:entity_event_completed for each one inserted;

    9. log entity_analysis_completed; return that list.
}
```

> **`entity_events=[]` is a valid, ACKed outcome** (system-design.md §3.6) — no HK-listed entity mentioned in an article is not an error. `self._repo.persist_entities()` writes zero rows in this case; nothing is published to `stream:entity_event_completed`; `find_entity_events()` still returns normally (an empty list) and the article is still ACKed.

> **`entity_skill.run()` only ever raises something outside `LLMError`.** `SkillBase.run()`'s one `except` clause (§8.1) — `except LLMError` — catches every typed LLM failure and converts them to `result.failure_reason` instead of raising. Anything else escaping `llm_process()` is an unanticipated bug, and propagates straight out of `run()` uncaught, the same as a SADI fetch or DB write failure (§11.4).

> **Row-per-entity.** The `(source_url, stock_code)` unique index (§17) depends on `output_items` never carrying two items for one `stock_code`. The LLM is told not to (system-design.md §9.4 Edge Case Rule 5), and `EntityAnalysisSkill`'s Business Constraint Validation (§8.2) drops duplicates before any `EntityEvent` is built.

#### Data Structure: `EntityEvent` — `app/models/entity_event.py`

One shape for an `entity_events` row, on both sides: `find_entity_events()` builds one per verified entity by pairing each `VerifiedEntity`'s `stock` and `outcome` with `source_id`/`source_url`/`source_name`/`published_at` from `article` (`entity_event_id`/`created_at` unset); `EntityEventRepository.persist_entities()` (§10.1) sets those on a successful insert; `AggregationService.process_entity_event()` (§12.1) reads the row back into a fresh `EntityEvent`. `analysis_outcome` is wrapped in `VersionedMetadata` (§3.2) with `version=result.skill_log.skill_version`.

```python
from pydantic import BaseModel

from app.common.stock import Stock   # §4.2
# EventTypeEnum, SentimentEnum: defined above in this module (§3.1)

class AnalysisOutcome(BaseModel):
    """One verified entity from EntityAnalysisSkill (§8.2), built from a verified ExtractedEntity;
    wrapped by EntityEvent.analysis_outcome."""
    stock_code: str
    event_type_primary: EventTypeEnum
    event_type_secondary: EventTypeEnum | None
    sentiment_label: SentimentEnum
    sentiment_score: float             # [0.0, 1.0]
    headline: str                      # <=20 Traditional Chinese characters
    entity_summary: str                # <=100 Traditional Chinese characters

@dataclass
class EntityEvent:
    source_id: UUID              # CleanedNews.cleaned_id — which article this came from, nothing else
    source_url: str
    source_name: str
    published_at: Optional[datetime]   # nullable in entity_events (system-design.md §10.1) — CleanedNews.published_at is Optional too; merge_or_create() (§12.4) applies the created_at fallback, not this repository
    analysis_outcome: VersionedMetadata[AnalysisOutcome]   # §3.2/§8.2; persisted under additional_outcome — the other fields are read off .output
    stock: Stock                                    # §4.2 — write: the Stock verification matched (§8.2); read-back: rebuilt from the row (§10.1)
    entity_event_id: Optional[UUID] = None       # set by EntityEventRepository.persist_entities() only if this row's insert actually succeeds
    created_at: Optional[datetime] = None        # mirrors the row's created_at — same datetime.now(timezone.utc) value, set alongside entity_event_id
```

`exchange`/`stock_code` are read off `stock`, and `event_type_primary`/`sentiment_label`/`sentiment_score`/`headline`/`entity_summary` off `analysis_outcome.output`. The DB row has its own columns for all seven (system-design.md §10.1); the repository reads and writes them from those sources.

`stock` is snapshotted into the row at write time (`exchange`/`stock_code` columns; `company_name_zh`/`company_name_en` in `additional_outcome.output`, §10.1), and `_row_to_entity_event()` rebuilds it from that snapshot alone, never from the live `HKStockListCache`. Only Entity Analysis reads the cache. This keeps Aggregation and Scoring independent of the HK Stock List: a stock removed by a later sync, or an empty fallback cache (§4.2), would otherwise make read-backs fail. The snapshot is historical by design.

`metadata` is recoverable from `source_id`, and `updated_at` is never read, so neither is a field.

#### Logging: `entity_analysis_completed`

The full `entity_analysis_completed` log event (system-design.md §9.8) is logged directly — no intermediate type. `EntityAnalysisService.find_entity_events()` calls `logger.info("entity_analysis_completed", **dataclasses.asdict(result.skill_log), cleaned_id=cleaned_id, source_url=article.source_url, entity_event_ids=entity_event_ids)`, where `entity_event_ids` is `[]` for any `failure_reason` outcome (logged immediately, before `self._repo.persist_entities()` is ever reached) and `[e.entity_event_id for e in persisted]` on success, after `self._repo.persist_entities()` returns `persisted`. `result.skill_log` is unconditionally present either way, so this call needs no special-casing per outcome.

The redelivery path (step 3 above) never runs the Skill, so there's no `skill_log` to log — it logs `logger.info("entity_analysis_republished", cleaned_id=cleaned_id, source_url=article.source_url, entity_event_ids=existing_ids)` instead.

### 11.2 `app/entity_analysis/cleaned_news_consumer.py` — `CleanedNewsConsumer`

#### Class: `CleanedNewsConsumer`

Owns consumption of `stream:raw_news_cleaned` (consumer/reclaim loops, `WorkerPool` dispatch, dedup guard, ACK/backoff/dead-letter), calling `EntityAnalysisService.find_entity_events()` (§11.1) for the work. Split rationale: §11.1.

```python
class CleanedNewsConsumer:
    def __init__(self, worker_pool: WorkerPool, config: EntityAnalysisConfig, redis_config: RedisConfig):
        self._service = get_entity_analysis_service()       # §11.1
        self._redis_stream = get_redis_stream_client()     # §6.1 — consumes, reclaims, ACKs
        self._worker_pool = worker_pool
        self._config = config
        self._processing_cleaned_ids: set[UUID] = set()

    async def start(self) -> None:
        """Entry point — called once from main.py's lifespan (§16). Creates
        the consumer group, then runs _consumer_loop() and _reclaim_loop()
        concurrently via run_supervised() (§6.1) — a plain asyncio.gather()
        would leave one loop orphaned if the other raised."""
        await self._redis_stream.create_group_if_not_exists(STREAM_RAW_NEWS_CLEANED, CONSUMER_GROUP_ENTITY_ANALYSIS)
        await run_supervised(self._consumer_loop(), self._reclaim_loop())

    async def _consumer_loop(self) -> None:
        """Reads batches from stream:raw_news_cleaned and dispatches each
        message to worker_pool — see Flowchart below."""

    async def _reclaim_loop(self) -> None:
        """Every STREAM_CLAIM_TIMEOUT_MS, reclaims timed-out messages via
        autoclaim(), dead-lettering ones past ENTITY_ANALYSIS_MAX_RETRY —
        see below."""

    async def _process_one(self, cleaned_id: UUID, message_id: str) -> None:
        """The shared per-message entry point both loops dispatch into —
        gates on HK Stock List readiness and the dedup guard, calls
        EntityAnalysisService.find_entity_events(), and decides
        ACK/backoff — see Flowchart and Purpose block below."""
```

`_consumer_loop()` and `_reclaim_loop()` are described in their own subsections below; `start()` only launches them concurrently (§6.1).

> **Why `start()` is its own method.** Not on `EntityAnalysisService`: it decides *when* to call `find_entity_events()` (the Service/Consumer split). Not in `__init__()`: `main.py`'s Coroutine Monitoring (§16) restarts a crashed layer by calling `start()` again on the same instance, and `__init__()` can't `await`. Not inlined in `main.py`: the uniform restart wrapper (`_make_monitored`, §16) needs each layer's startup encapsulated in its own `start()`.

#### `_consumer_loop()` (see Flowchart, below)

Reads batches (`read_from()`) and dispatches each message to `worker_pool` (`WorkerPool.dispatch()`, §11.3); nodes `A`–`D` of the flowchart below are its entire body.

#### Method: `CleanedNewsConsumer._process_one(cleaned_id: UUID, message_id: str) -> None`

Both `_consumer_loop()` (above) and `_reclaim_loop()` (below) dispatch into this **same** method — the diagram below is system-design.md §3.2's flowchart, with implementation-level detail added; only how a message arrives at `worker_pool.dispatch(...)` differs between the two loops. Node `H` is the single call into `EntityAnalysisService.find_entity_events(cleaned_id)`, described above — its own internal steps aren't redrawn here.

```mermaid
flowchart TD
    A([_consumer_loop]) --> B[create_group_if_not_exists\nstream:raw_news_cleaned / sapi-entity-analysis]
    B --> C[read_from COUNT=ENTITY_ANALYSIS_BATCH_SIZE BLOCK=ENTITY_ANALYSIS_BATCH_TIMEOUT_MS\nconsumer=config.consumer_name]
    C -->|Empty| C
    C -->|Messages| D[gather worker_pool.dispatch over the batch\nSemaphore ENTITY_ANALYSIS_MAX_CONCURRENT\nnext read_from once all return]
    D --> R{len get_hk_stock_list_cache\n> 0?}
    R -->|No - not ready| S[Log warning hk_stock_list_not_ready\nDo not ACK - redeliver later]
    R -->|Yes| E{cleaned_id in\nprocessing_cleaned_ids Set?}
    E -->|Yes| F[Skip - already in flight\nleave unACKed for this delivery]
    E -->|No| G[Add to Set — no await between\nthis check and this insert]
    G --> H[service.find_entity_events cleaned_id\nEntityAnalysisService — see above]
    H -->|Returns normally\nsuccess or SADI 404| Q[Remove from Set\nACK]
    H -->|Raises LLMRateLimitError| L[worker_pool.trigger_backoff\nRemove from Set\nDo not ACK]
    H -->|Raises LLMProviderError, LLMAuthenticationError,\nor LLMSchemaViolationError| M[Remove from Set\nDo not ACK]
    F & Q & L & M & S --> C
```

> **`hk_stock_list_not_ready` gate:** checked before the in-flight Set and before `EntityAnalysisService`, as a synchronous side-effect-free read. Messages are **not ACKed**: an empty list would make every entity `found=false` and produce a permanently-wrong empty result. The reclaim loop (§6.1) redelivers after `STREAM_CLAIM_TIMEOUT_MS`.

> **`_process_one()` wraps everything from the Set-insert onward in `try`/`except LLMRateLimitError`/`except SAPIError`/`except Exception`** (in that order, since `LLMRateLimitError` subclasses `LLMError`, which subclasses `SAPIError`). `except LLMRateLimitError` calls `trigger_backoff()`, then logs the error like the next clause. `except SAPIError` covers every other SAPI error — any other `LLMError` re-raised by `find_entity_events()`, a SADI outage, a DB write failure: log it at its own `log_level` with its `dev_message`, `cleaned_id` and `message_id`, and leave the message unACKed. Only the last is unanticipated: log ERROR `f"Unhandled exception: {exc!r}"` with `exc_info`, remove `cleaned_id` from `processing_cleaned_ids` in a `finally`, leave the message unACKed, and don't re-raise. This limits an unanticipated bug to one message (Coroutine Monitoring, system-design.md §12.1, remains the fallback outside the wrapper); a repeating failure dead-letters at `delivery_count >= ENTITY_ANALYSIS_MAX_RETRY` (§6.1).

```
Purpose : The single entry point for dispatching one message, from either _consumer_loop()
          or _reclaim_loop() (§6.1). Implements the flowchart from the
          hk_stock_list_not_ready gate onward. The check-then-insert into
          processing_cleaned_ids has no `await` in between, which makes concurrent calls from
          both loops race-free and distinguishes a still-in-flight duplicate (entry present)
          from an orphaned one (entry absent, e.g. after a crash). Everything from the
          Set-insert onward runs inside the try/except above.
```

#### `_reclaim_loop()` (§6.1's Reclaim and Dead Letter pattern, applied here)

```
Purpose : while True: await asyncio.sleep(STREAM_CLAIM_TIMEOUT_MS / 1000);
          try: autoclaim(stream=STREAM_RAW_NEWS_CLEANED,
          group=CONSUMER_GROUP_ENTITY_ANALYSIS, consumer=config.consumer_name,
          min_idle_ms=STREAM_CLAIM_TIMEOUT_MS, count=ENTITY_ANALYSIS_BATCH_SIZE).
          For each reclaimed message: if delivery_count >= ENTITY_ANALYSIS_MAX_RETRY,
          publish to STREAM_ENTITY_ANALYSIS_DEAD_LETTER and ACK (never reaches
          _process_one()). Otherwise, extract cleaned_id from fields and
          dispatch via worker_pool.dispatch(lambda: self._process_one(cleaned_id, message_id))
          — the remaining messages gathered as one batch, identical to
          _consumer_loop() (§11.3).
          except Exception: log warning entity_analysis_reclaim_sweep_failed
          with exc_info, do not re-raise — retried next tick.
```

The sweep body is wrapped in `except Exception` as specified in §6.1.

#### Idempotency (system-design.md §3.4)

Two layers, both required, one per class:
1. **In-memory `Set[str]` `processing_cleaned_ids`, owned by `CleanedNewsConsumer`** — guards concurrent duplicate dispatch, whether the duplicate arrives from the same batch, a later `read_from()` batch, or `_reclaim_loop()`. Safe with no locking (single-threaded asyncio) precisely because check-and-insert has no `await` between them (see `_process_one()` above).
2. **DB composite unique index on `entity_events (source_url, stock_code)`, enforced inside `EntityEventRepository.persist_entities()`**, `INSERT ... ON CONFLICT (source_url, stock_code) DO NOTHING` — guards cross-batch/cross-restart redelivery, per-entity. This is the layer that actually saves you if a duplicate *does* get past the Set (e.g. a genuine crash-and-restart, where the Set was wiped) — the DB write, not the Set, is the real correctness guarantee, the same relationship the Set/DB-constraint pair always has.
3. **Redelivery after persist, handled by `EntityAnalysisService.find_entity_events()`'s step 3 (§11.1)** — an article whose rows already exist is republished rather than re-analysed (system-design.md §3.4); Aggregation absorbs the resulting duplicate signal (§12.1).

### 11.3 `app/entity_analysis/worker_pool.py` — `WorkerPool`

```python
class WorkerPool:
    def __init__(self, layer: str, max_concurrent: int, rate_limit_backoff_s: int): ...   # layer: "entity_analysis" | "scoring", for logs

    async def dispatch(self, coro_factory: Callable[[], Awaitable[None]]) -> None:
        """Acquire the semaphore, check rate_limit_until, run coro_factory().
        Returns only once coro_factory() has completed."""
        async with self._semaphore:
            while (remaining := self._rate_limit_until - asyncio.get_running_loop().time()) > 0:
                await asyncio.sleep(remaining)
            await coro_factory()

    def trigger_backoff(self) -> None:
        """Set self._rate_limit_until = now() + rate_limit_backoff_s, then log WARNING
        rate_limit_backoff_started with layer and backoff_until (UTC ISO 8601) —
        system-design.md §12.1. Called by the layer's Consumer when its Service call
        raises LLMRateLimitError (§11.2's _process_one())."""
```

Backoff is pool-level (system-design.md §12.1). `dispatch()` checks `self._rate_limit_until` *after* acquiring the semaphore, in a loop, so a caller already waiting on the semaphore still waits out the backoff, and a `trigger_backoff()` that extends the deadline mid-sleep is honoured. Work already running is not interrupted.

**`dispatch()` awaits the work**, so both loops (`_consumer_loop()`/`_reclaim_loop()`, §11.2) `asyncio.gather()` a whole batch of dispatches and read again only once all return (system-design.md §3.3). Both share the pool, so `max_concurrent` caps the layer. `WorkerPool` owns no Tasks; cancelling the layer Task cancels in-flight work. After each gathered batch, the loop logs INFO `entity_analysis_batch_completed` (Scoring: `scoring_batch_completed`, §13.2) with `batch_size` and `latency_ms` — the per-batch measure Q-1 tunes `*_BATCH_SIZE`/`*_BATCH_TIMEOUT_MS` against (§18).

> `RATE_LIMIT_BACKOFF_S * 1000` must stay below `STREAM_CLAIM_TIMEOUT_MS` — system-design.md §12.1 (claim-timeout constraint).

The same `WorkerPool` class is used, as independent instances, by Entity Analysis and Scoring (§13).

### 11.4 Error Handling (system-design.md §3.6 table — implementation cross-reference)

| Scenario | Where handled |
|---|---|
| LLM API call failure (`LLMProviderError`/`LLMRequestError`/`LLMAuthenticationError`) | Caught inside `SkillBase.run()` (§8.1, inherited by `EntityAnalysisSkill`) → `result.failure_reason=err`, never raised; `EntityAnalysisService.find_entity_events()` logs `entity_analysis_completed` from `result.skill_log` with `entity_event_ids=[]` (nothing was persisted) and re-raises the original exception directly (`raise result.failure_reason`); `CleanedNewsConsumer` does not ACK. `LLMAuthenticationError` is logged at CRITICAL, its own `log_level` (§7.2) |
| LLM schema violation | Same, `result.failure_reason` is the `LLMSchemaViolationError` (no fallback exists for this Skill, system-design.md §9.7) — never raised, same logging/outcome/no-ACK treatment |
| `LLMRateLimitError` | Caught inside `run()` → `result.failure_reason` is the `LLMRateLimitError`, never raised; `EntityAnalysisService.find_entity_events()` logs `entity_analysis_completed` and re-raises `LLMRateLimitError` itself; `CleanedNewsConsumer` calls `WorkerPool.trigger_backoff()`, does not ACK |
| All entities filtered out | Valid `output_items=[]`; no row written, nothing published, `find_entity_events()` returns normally, `CleanedNewsConsumer` ACKs |
| `cleaned_id` missing from SADI | `NewsClient.fetch_cleaned_news()` returns `None`; `EntityAnalysisService.find_entity_events()` logs warning, returns normally (not an error, system-design.md §3.6); `CleanedNewsConsumer` ACKs (retry can't help) |
| DB write failure | `EntityEventRepository.persist_entities()`'s transaction (§5.1) rolls back and raises `ServiceUnavailableError` (connection unusable / retries exhausted) or `DatabaseError`; `EntityAnalysisService.find_entity_events()` doesn't catch it — `CleanedNewsConsumer._process_one()`'s `except SAPIError` logs it at its own level, no ACK (§11.2) |
| SADI API failure | `NewsClient` raises `ServiceUnavailableError` (`COMMON-5002`) or, for an unexpected body, `ParseError` (§9.1); `EntityAnalysisService.find_entity_events()` doesn't catch this itself — propagates uncaught to `CleanedNewsConsumer`'s `except SAPIError`, which logs it at its own `log_level` (WARNING for `COMMON-5002`, ERROR for `ParseError`); no ACK |
| `RedisStreamClient` unreachable | `redis.ConnectionError`/`redis.TimeoutError` caught by `CleanedNewsConsumer._consumer_loop()` around its `read_from()`/`ack()` calls: WARNING `entity_analysis_redis_stream_unreachable`, sleep `REDIS_RECONNECT_INTERVAL_S`, read again (§6.1's reconnect behaviour) |
| HK Stock List not ready (`len(get_hk_stock_list_cache()) == 0`) | `CleanedNewsConsumer._process_one()`'s first check, before the in-flight Set and before `EntityAnalysisService` is called at all (§11.1); log warning `hk_stock_list_not_ready`; no ACK — never reaches `news_client.fetch_cleaned_news()` or `entity_skill.run()` at all, so no entity ever gets a permanently-wrong empty-result outcome from this cause |

---

## 12. Aggregation Layer — `app/aggregation/`

### 12.1 `app/aggregation/aggregation_service.py` — `AggregationService`

Split three ways: `AggregationConsumer` (§12.2) owns `stream:entity_event_completed` consumption (loops, `_enqueued` guard, ACK, flush timing); `AggregationService` owns all business logic (group routing, `merge_or_create()` §12.4, `EventAggregationStore` writes §12.5, flush); `GroupCoroutine` (§12.3) is a pure serial scheduler with no business logic.

ACK is deferred: a routed `EntityEvent` is ACKed when its `GroupCoroutine` reaches it (system-design.md §4.3), not when `process_entity_event()` returns. `AggregationConsumer._on_processed(entity_event_id, message_id)` (§12.2) ACKs and clears `_enqueued`; `_route_one()` wraps it in a zero-arg lambda for `process_entity_event()`'s `on_processed` parameter, since `message_id` has no other use there. `GroupCoroutine` only awaits the opaque `work` callable each queued item carries.

#### Class: `AggregationService`

```python
class AggregationService:
    def __init__(self, config: AggConfig):
        self._entity_event_repo = get_entity_event_repository()   # §10.1 — process_entity_event()'s read
        self._event_repo = get_event_repository()         # §10.2 — _persist_and_publish()'s write, _process()'s duplicate check
        self._redis_stream = get_redis_stream_client()    # §6.1 — publish only, never consumes here
        self._event_store = get_event_aggregation_store()   # §12.5
        self._config = config
        self._groups: dict[GroupKey, GroupCoroutine] = {}   # the Coroutine Manager (§12.3)
        self._group_semaphore = asyncio.Semaphore(config.max_concurrent)
        self._last_flushed: dict[UUID, datetime] = {}   # event_id → aggregation_updated_at this process last flushed

    async def process_entity_event(self, entity_event_id: UUID, on_processed: Callable[[], Awaitable[None]],
                                   on_failed: Callable[[], None]) -> None:
        """All business logic for one EntityEvent (GroupCoroutine, §12.3, runs none of it).
        Safe to call directly, concurrently. on_processed (zero-arg, bound by
        AggregationConsumer._route_one(), §12.2) ACKs; on_failed releases the caller's
        in-flight guard without ACKing so a redelivery retries (§12.2). A failure before
        the enqueue below propagates to the caller, which releases the guard itself."""
        entity_event = await self._entity_event_repo.fetch_entity_event(entity_event_id)   # §10.1 — same row EntityEventRepository.persist_entities() wrote
        if entity_event is None:
            # No row for this id — a redelivery can never make it appear, so ACK rather than retry (§12.6)
            logger.warning("aggregation_entity_event_missing", entity_event_id=entity_event_id)
            await on_processed()
            return
        group_key = GroupKey(entity_event.stock.exchange, entity_event.stock.stock_code, entity_event.analysis_outcome.output.event_type_primary)   # §10.1 — EntityEvent has no flat fields of its own for these
        group = self._groups.get(group_key)
        if group is None:
            # Not dict.setdefault(...) — that would build the GroupCoroutine
            # (and spawn its _run() Task, §12.3) unconditionally, even when
            # group_key is already present, orphaning a spurious task whose
            # eventual idle-timeout would pop the real, active group instead.
            await self._group_semaphore.acquire()   # one permit per live group, held for its lifetime — released by _on_idle
            group = self._groups.get(group_key)   # re-check: acquire() awaits, so another call may have
            if group is None:                     # created group_key's GroupCoroutine while this one was waiting
                def _on_idle(gk: GroupKey) -> None:
                    self._groups.pop(gk, None)
                    self._group_semaphore.release()
                group = GroupCoroutine(group_key, self._config, _on_idle)
                self._groups[group_key] = group
            else:
                self._group_semaphore.release()   # lost the race — give back the permit; group already exists

        async def _process() -> None:
            step = "get_active_event"
            try:
                active_event = await self._event_store.get_active_event(entity_event.stock.exchange, entity_event.stock.stock_code, entity_event.analysis_outcome.output.event_type_primary)
                # Duplicate signal (system-design.md §3.4/§4.4): already merged into the active
                # Event (not yet flushed, or flushed but still active), or already flushed into
                # some Event. Checked here, inside the group's serial queue, so it can't race
                # a concurrent merge of the same entity_event_id.
                step = "duplicate_check"
                if (active_event is not None and entity_event_id in active_event.entity_event_ids) \
                        or await self._event_repo.fetch_mapped_event_id(entity_event_id) is not None:
                    logger.info("aggregation_duplicate_skipped", entity_event_id=entity_event_id, group_key=group_key)
                    await on_processed()
                    return
                if active_event is not None and is_late(active_event, entity_event, self._config):   # §12.4
                    # Skipped for now, not merged elsewhere (system-design.md §4.4) — ACKed,
                    # since redelivery can't change the outcome
                    logger.warning("aggregation_late_entity_event_skipped", entity_event_id=entity_event_id,
                                   group_key=group_key, published_at=entity_event.published_at,
                                   active_event_id=active_event.event_id, active_last_seen_at=active_event.last_seen_at)
                    await on_processed()
                    return
                updated = merge_or_create(active_event, entity_event, self._config)
                if active_event is not None and updated.event_id != active_event.event_id:
                    # A new Event replaces the group's active one in the same Redis key —
                    # persist the replaced Event's final state first, or its unflushed
                    # merges are lost (system-design.md §4.5)
                    step = "persist_replaced_event"
                    await self._persist_and_publish([active_event])
                step = "upsert_active_event"
                await self._event_store.upsert_active_event(updated, ttl_for(updated, self._config))
                step = "on_processed"
                await on_processed()
            except Exception:
                # Never re-raised: one failed EntityEvent must not take down its group
                # (GroupCoroutine, §12.3). on_failed() releases the in-flight guard
                # without ACKing, so the reclaim loop retries it (§12.2).
                logger.error("aggregation_process_failed", entity_event_id=entity_event_id,
                             group_key=group_key, step=step, exc_info=True)
                on_failed()

        await group.enqueue(_process)

    async def flush(self) -> None:
        """Pure business logic:
            active = await self._event_store.scan_all()
            changed = [e for e in active
                       if self._last_flushed.get(e.event_id) != e.aggregation_updated_at]
            started = time.perf_counter()
            published = await self._persist_and_publish(changed)
            self._last_flushed = {e.event_id: e.aggregation_updated_at for e in active}
            log INFO aggregation_flush_completed with events_persisted=len(changed),
                events_published=published, latency_ms (since `started`)
        Only Events whose aggregation_updated_at moved since this process
        last flushed them are sent (system-design.md §4.5). _last_flushed
        starts empty, so the first flush after startup sends every active
        Event — crash recovery is unchanged. It's reassigned only after
        _persist_and_publish() returns, so a failed flush re-sends the same
        Events next time; rebuilding it from `active` also drops entries for
        keys that expired or were replaced. A key changed by _process()
        between scan_all() and the reassignment records its older scanned
        value, which differs from the newer one, so the next flush sends it.
        No consumer-loop concerns — AggregationConsumer decides *when* to
        call this (queue-empty, interval timer, or graceful shutdown,
        §12.2); this method just does it."""

    async def _persist_and_publish(self, events: list[Event]) -> int:
        """Transactional outbox (system-design.md §4.5); returns the number of Events published:
        1. self._event_repo.persist_events(events) (§10.2) — one transaction;
           only newer state is written, and a written row is pending publish
           by construction (its aggregation_updated_at is now newer than its
           published_aggregation_updated_at).
        2. pending = self._event_repo.fetch_unpublished_events(
           [e.event_id for e in events]) — only the Events given here, never
           the whole table. A publish lost to a crash is still retried:
           after a restart, flush()'s empty _last_flushed sends every active
           Event, and a publish that failed while running leaves
           _last_flushed unchanged, so the same Events come back next flush.
           Not recovered: an Event whose Redis key expired before either —
           a crash plus downtime longer than its TTL (system-design.md §13.2).
        3. For each pending Event: publish stream:event_aggregated (event_id,
           v="1"), then self._event_repo.mark_published(event_id,
           aggregation_updated_at) — the version just published, so a newer
           write landing in between stays pending.
        An Event whose aggregation_updated_at didn't advance is never
        pending, so Scoring never re-scores an unchanged Event. Delivery is
        at-least-once: a crash between publish and mark_published re-publishes
        the same version, which Scoring re-scores harmlessly. Shared by
        flush() and _process()'s replaced-Event branch."""

    async def close(self) -> None:
        """Cancels every live GroupCoroutine's Task (§12.3) and awaits them.
        Each group runs its own asyncio.Task, separate from the layer Task, so
        cancelling the layer Task alone leaves groups merging while the
        clients close under them. Called only from main.py's shutdown (§16),
        after the layer Tasks are cancelled and awaited (final flush done) and
        before clients close — never from AggregationConsumer.start()'s
        finally, which also runs on a crash-restart: cancelling groups there
        would strand their queued items in _enqueued (§12.2). Nothing is lost:
        work cut off before its ACK is redelivered on the next start, and
        anything merged after the final flush stays in Redis for the next
        startup's first flush."""
```

`AggregationService` is itself a module-level singleton + getter (§5.1), same shape as `EntityAnalysisService` (§11.1):

```python
_aggregation_service: Optional[AggregationService] = None

def get_aggregation_service() -> AggregationService:
    if _aggregation_service is None:
        raise ServiceUnavailableError("aggregation service is not initialized")
    return _aggregation_service

def create_aggregation_service(config: AggConfig) -> AggregationService:
    global _aggregation_service
    _aggregation_service = AggregationService(config)
    return _aggregation_service
```

`create_aggregation_service()` is called from `main.py` (§16) with `config.agg`.

### 12.2 `app/aggregation/aggregation_consumer.py` — `AggregationConsumer`

Owns `stream:entity_event_completed` consumption (loops, `_enqueued` guard, flush timing), calling `AggregationService.process_entity_event()`/`flush()` (§12.1). Split rationale: §12.1.

```python
class AggregationConsumer:
    def __init__(self, config: AggConfig):
        self._service = get_aggregation_service()          # §12.1
        self._redis_stream = get_redis_stream_client()    # §6.1 — consumes, reclaims, ACKs
        self._config = config
        self._enqueued: set[UUID] = set()

    async def start(self) -> None:
        """Entry point, called from main.py's lifespan (§16); rationale as
        CleanedNewsConsumer.start() (§11.2). Runs both loops under run_supervised() (§6.1);
        on exit, the final flush (system-design.md §4.5) runs in `finally`."""
        await self._redis_stream.create_group_if_not_exists(STREAM_ENTITY_EVENT_COMPLETED, CONSUMER_GROUP_AGGREGATION)
        try:
            await run_supervised(self._consumer_loop(), self._reclaim_loop())
        finally:
            await self._service.flush()

    async def _consumer_loop(self) -> None:
        """Reads batches from stream:entity_event_completed and routes
        each message — see Flowchart below."""

    async def _reclaim_loop(self) -> None:
        """Every STREAM_CLAIM_TIMEOUT_MS, reclaims timed-out messages via
        autoclaim(), dead-lettering ones past AGG_MAX_RETRY — see below."""
```

> **Graceful shutdown:** the `try`/`finally` in `start()` means `Task.cancel()` from `main.py`'s shutdown still runs one final `self._service.flush()` (system-design.md §4.5).

#### `_consumer_loop()` (see Flowchart, below)

Reads batches from `stream:entity_event_completed` (`read_from()`) and calls `_route_one()` for each message, plus the queue-empty/interval flush triggers — nodes `A`–`D` and `FLUSH` of the Flowchart below are this loop's entire body. Each trigger's `self._service.flush()` call is wrapped in `except Exception`: log warning `aggregation_flush_failed` with `exc_info`, don't re-raise — the next trigger retries from the same Redis state (§12.6), and the layer Task never crashes over a DB/Redis outage. `except Exception`, not bare `except:`, for the same `CancelledError` reason as `_reclaim_loop()` below. `start()`'s `finally: flush()` is not wrapped.

#### Flowchart (system-design.md §4.2 + §4.5, with the Coroutine Manager and flush triggers made explicit) — `AggregationConsumer._route_one()`

`_consumer_loop()` (above) and `_reclaim_loop()` (below) call `_route_one(entity_event_id, message_id)` — the diagram below is that shared method's body (steps `RTE` onward); the two loops differ only in how they obtain `(entity_event_id, message_id)` pairs to feed it. Node `D` is the call into `AggregationService.process_entity_event()`, described above — its own internal steps (fetch, route, enqueue) aren't redrawn here.

```mermaid
flowchart TD
    A([_consumer_loop]) --> B[create_group_if_not_exists]
    B --> C[read_from COUNT=AGG_BATCH_SIZE BLOCK=AGG_BATCH_TIMEOUT_MS\nconsumer=config.consumer_name]
    C -->|Empty - queue-empty trigger| FLUSH[service.flush]
    C -->|AGG_DB_FLUSH_INTERVAL_S elapsed\nsince last flush, checked each iteration| FLUSH
    C -->|Messages| RTE{entity_event_id in\n_enqueued Set?}
    RTE -->|Yes| SKIP[Skip - already in flight\nleave unACKed for this delivery]
    RTE -->|No| ADD[Add to _enqueued Set — no await\nbetween this check and this insert]
    ADD --> D[service.process_entity_event entity_event_id, on_processed\nAggregationService — see above]
    D --> C
    FLUSH --> C
```

> **`D` returns once the entity_event is enqueued onto its `GroupCoroutine`, not when it is processed.** The ACK happens later via `on_processed` → `self._on_processed(...)` (§12.3), so it isn't drawn here; `SKIP` is the only outcome `_route_one()` produces synchronously.

#### Method: `AggregationConsumer._on_processed(entity_event_id: UUID, message_id: str) -> None`

```python
async def _on_processed(self, entity_event_id: UUID, message_id: str) -> None:
    self._enqueued.discard(entity_event_id)
    await self._redis_stream.ack(STREAM_ENTITY_EVENT_COMPLETED, CONSUMER_GROUP_AGGREGATION, message_id)
```

An explicit-argument method, testable in isolation. `process_entity_event()` never sees it directly: `_route_one()` (below) wraps it in a per-call zero-arg lambda closing over `entity_event_id`/`message_id` (§12.1).

#### Method: `AggregationConsumer._route_one(entity_event_id: UUID, message_id: str) -> None`

```
Purpose : Shared entry point for routing one EntityEvent id, whether it
          arrived via _consumer_loop()'s read_from() or _reclaim_loop()'s
          autoclaim() (§6.1). Implements the flowchart above from the
          _enqueued check onward, then calls
          self._service.process_entity_event(entity_event_id,
          lambda: self._on_processed(entity_event_id, message_id),
          lambda: self._enqueued.discard(entity_event_id)) inside
          try/except Exception: a failure before the enqueue (e.g. the
          fetch_entity_event() DB read) discards entity_event_id from
          _enqueued, logs warning aggregation_route_failed with exc_info,
          and leaves the message unACKed — not re-raised, so one bad
          message never takes down the consumer loop.
Why failures release the guard : _enqueued must be cleared on every outcome; an entry left
          behind would make every redelivery SKIP until the message dead-letters at
          AGG_MAX_RETRY. On failure it is cleared without ACKing, so the reclaim loop retries.
Why the _enqueued guard exists : merge_or_create() (§12.4) appends entity_event_id with no
          dedup check, so it must not run twice for one id. A busy GroupCoroutine
          (system-design.md §4.3) can leave a message queued and unACKed past
          STREAM_CLAIM_TIMEOUT_MS; without the guard, _reclaim_loop() would re-route it and
          double-append. The check-then-insert has no `await` in between (same pattern as
          §11.2's processing_cleaned_ids and §13.2's _in_process). It covers duplicates
          arriving while the first copy is in flight; later duplicates are caught by
          _process()'s duplicate check (§12.1).
```

`_enqueued` entries are removed by `_on_processed()` when the unit of work completes, not on enqueue, so a message still in the group's backlog keeps reading as already enqueued.

#### `_reclaim_loop()` (§6.1's Reclaim and Dead Letter pattern, applied here)

```
Purpose : while True: await asyncio.sleep(STREAM_CLAIM_TIMEOUT_MS / 1000);
          try: autoclaim(stream=STREAM_ENTITY_EVENT_COMPLETED,
          group=CONSUMER_GROUP_AGGREGATION, consumer=config.consumer_name,
          min_idle_ms=STREAM_CLAIM_TIMEOUT_MS, count=AGG_BATCH_SIZE).
          For each reclaimed message: if delivery_count >= AGG_MAX_RETRY,
          publish to STREAM_AGGREGATION_DEAD_LETTER and ACK (never reaches
          _route_one()). Otherwise, extract entity_event_id from fields and
          call _route_one(entity_event_id, message_id) — identical call as
          _consumer_loop() uses.
          except Exception: log warning aggregation_reclaim_sweep_failed
          with exc_info, do not re-raise — retried next tick.
```

`except Exception` per §6.1; `start()`'s `finally: flush()` still runs on cancellation.

> **Tuning note:** `STREAM_CLAIM_TIMEOUT_MS` must be set comfortably above the worst realistic backlog depth × per-item processing time for the busiest group, not just above a single item's own processing latency — otherwise the reclaim loop steals legitimately in-progress (not crashed) work from a hot `GroupCoroutine` under normal load, which the `_enqueued` guard prevents from corrupting state but still wastes a redundant DB round-trip per false reclaim.

### 12.3 `app/aggregation/group_coroutine.py` — `GroupCoroutine`

```python
@dataclass(frozen=True)
class GroupKey:
    exchange: str
    stock_code: str
    event_type_primary: str

class GroupCoroutine:
    def __init__(self, group_key: GroupKey, config: AggConfig, on_idle: Callable[[GroupKey], None]):
        self._group_key = group_key
        self._config = config   # only for AGG_GROUP_IDLE_TIMEOUT_S — no merge/EventAggregationStore knowledge
        self._on_idle = on_idle
        self._queue: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue()
        self._task = asyncio.create_task(self._run())   # starts draining immediately — no separate start() call needed

    async def enqueue(self, work: Callable[[], Awaitable[None]]) -> None:
        """Put a fully self-contained unit of work — built by
        AggregationService.process_entity_event() (§12.1) — onto self._queue. The
        coroutine's own run loop processes its queue strictly serially —
        this is what prevents concurrent modification of one group's
        sliding-window state (system-design.md §4.3). GroupCoroutine never inspects
        what work does."""
        await self._queue.put(work)

    async def _run(self) -> None:
        """Purely a serial scheduler: no merge_or_create(), no EventAggregationStore,
        no ACK logic of its own; whatever work does (merge, DB write,
        on_processed call) is AggregationService's business, not this
        class's."""
        while True:
            try:
                work = await asyncio.wait_for(self._queue.get(), timeout=self._config.group_idle_timeout_s)
            except asyncio.TimeoutError:
                if not self._queue.empty():
                    # enqueue() landed an item in the gap between the timer
                    # firing and this handler running — go collect it instead
                    # of abandoning it into a queue nobody will ever drain.
                    continue
                # Empty queue for AGG_GROUP_IDLE_TIMEOUT_S — self-destruct:
                # remove this group from the Coroutine Manager's dict
                # (AggregationService._groups, §12.1) and let this Task end.
                self._on_idle(self._group_key)
                return
            try:
                await work()
            except Exception:
                # Backstop only — work() (_process(), §12.1) catches its own
                # failures, so this is a bug in that handling itself. This
                # Task is never awaited, so re-raising would only end it
                # silently and drop every item still queued. Log and move on:
                # only this one item is lost (its _enqueued entry stays set,
                # so it's dead-lettered at AGG_MAX_RETRY, §12.6). Exception,
                # not BaseException — CancelledError from close() (§12.1)
                # still ends the Task.
                logger.critical("aggregation_group_work_failed", group_key=self._group_key, exc_info=True)
```

`GroupKey` is `frozen=True` so it can be used as a `dict` key (`self._groups`, below) and passed around by value — the same triple as `Event.exchange`/`stock_code`/`event_type_primary` (§3.3), since a group's key and the `Event` it produces always share those three fields.

`work` is a per-call closure built by `AggregationService.process_entity_event()` (§12.1). `GroupCoroutine` takes no `EventAggregationStore`, `on_processed`, or `redis_stream`, so it is testable with no knowledge of merging or Redis; a generic serial scheduler can only take something opaque. `on_idle` is the one callback parameter, carrying lifecycle bookkeeping only (removing the dict entry, releasing the semaphore permit); `_run()` calls it on the idle timeout. An exception from `work()` doesn't end the group: `_run()` logs it and continues. The other way the Task ends is cancellation by `AggregationService.close()` (§12.1).

> **Invariant: this race-freedom depends on two things staying true that aren't visible from the code alone.** `self._queue` must stay unbounded (`asyncio.Queue()`, no `maxsize`) — an unbounded queue's `put()` never suspends, which is what makes a caller's "look up group → enqueue → put on queue" one uninterruptible stretch relative to `_run()`'s own "check `queue.empty()` → evict" stretch; either can run first safely, but only because neither can be interrupted mid-way. `_on_idle` (§12.1's `_on_idle`) must also stay fully synchronous — no `await` inside it — for the same reason: an `await` there would let another task interleave between the empty-check and the dict `pop()`, reopening the exact race this design closes. If `self._queue` ever gets a `maxsize` (e.g. for backpressure) or `_on_idle` ever needs to `await` something, this section's whole safety argument needs revisiting, not just a local tweak.

> **Trade-off: opaque `work` hides context from the stack trace.** A `work()` exception surfaces at `_run()`'s `await work()` with no indication of the step or `entity_event_id`, so `_process()` (§12.1) logs `aggregation_process_failed` with `entity_event_id`, `group_key` and the step itself, and doesn't re-raise; `_run()`'s `except` is the backstop.

The Coroutine Manager (`self._groups`, gated by `self._group_semaphore`, §12.1) lives directly in `AggregationService`; it is routing and lifecycle only, not a separate module.

### 12.4 `app/aggregation/sliding_window.py` — pure merge algorithm

#### Function: `merge_or_create(active_event: Optional[Event], entity_event: EntityEvent, config: AggConfig) -> Event`

```
Purpose : Given the group's current active Event (Redis-active stage, §3.3, or None) and one
          incoming EntityEvent, decide merge vs new Event and return the Event ready for
          EventAggregationStore.upsert_active_event(). Pure, no I/O.
          published_at fallback (system-design.md §4.4/§10.1): published_at is nullable, so
          this function falls back to entity_event.created_at for the merge-window/hard-cap
          math; entity_events.published_at itself stays NULL.
          aggregation_updated_at is set here (datetime.now(timezone.utc), both branches): it
          is Scoring's staleness key (system-design.md §10.2), meaning "aggregation state
          actually changed", which the periodic flush can't know.
```

Merge/hard-cap/TTL algorithm (rationale — out-of-order arrival, `published_at` fallback — in system-design.md §4.4):

```python
def merge_or_create(active_event: Optional[Event], entity_event: EntityEvent, config: AggConfig) -> Event:
    published_at = entity_event.published_at or entity_event.created_at
    aggregation_updated_at = datetime.now(timezone.utc)

    if active_event is None:
        return Event(
            event_id=uuid4(),
            exchange=entity_event.stock.exchange, stock_code=entity_event.stock.stock_code,
            event_type_primary=entity_event.analysis_outcome.output.event_type_primary,
            first_seen_at=published_at,
            last_seen_at=published_at,
            aggregation_updated_at=aggregation_updated_at,
            entity_event_ids=[entity_event.entity_event_id],
        )

    gap_hours = abs((published_at - active_event.last_seen_at).total_seconds()) / 3600
    span_hours = (max(published_at, active_event.last_seen_at) - min(published_at, active_event.first_seen_at)).total_seconds() / 3600
    remaining_lifespan = config.event_max_timespan_hours - span_hours

    if gap_hours <= config.sliding_window_hours and remaining_lifespan > 0:
        return Event(
            event_id=active_event.event_id,
            exchange=active_event.exchange, stock_code=active_event.stock_code,
            event_type_primary=active_event.event_type_primary,
            # max/min, never blind overwrite — out-of-order arrival (system-design.md §4.4)
            first_seen_at=min(active_event.first_seen_at, published_at),
            last_seen_at=max(active_event.last_seen_at, published_at),
            aggregation_updated_at=aggregation_updated_at,
            entity_event_ids=[*active_event.entity_event_ids, entity_event.entity_event_id],
        )

    # No merge: hard cap breached or outside sliding window -> new Event
    return Event(
        event_id=uuid4(),
        exchange=entity_event.stock.exchange, stock_code=entity_event.stock.stock_code,
        event_type_primary=entity_event.analysis_outcome.output.event_type_primary,
        first_seen_at=published_at,
        last_seen_at=published_at,
        aggregation_updated_at=aggregation_updated_at,
        entity_event_ids=[entity_event.entity_event_id],
    )
```

#### Function: `is_late(active_event: Event, entity_event: EntityEvent, config: AggConfig) -> bool`

True when `entity_event` is dated before `active_event.last_seen_at` by more than `SLIDING_WINDOW_HOURS` — the case where `merge_or_create()` would otherwise start an older Event and take over the group's active key, closing a live Event early (system-design.md §4.4). Same `published_at` fallback as `merge_or_create()`. Pure, no I/O.

```python
def is_late(active_event: Event, entity_event: EntityEvent, config: AggConfig) -> bool:
    published_at = entity_event.published_at or entity_event.created_at
    return (active_event.last_seen_at - published_at).total_seconds() / 3600 > config.sliding_window_hours
```

#### Function: `ttl_for(active_event: Event, config: AggConfig) -> int`

`remaining_lifespan = EVENT_MAX_TIMESPAN_HOURS - span`, TTL `= min(SLIDING_WINDOW_HOURS, remaining_lifespan)` (the formula in `merge_or_create()`), computed from the returned `Event`. `last_seen_at` stands in for the incoming `published_at`: it equals it for in-order arrivals and is the best available reference otherwise.

```python
def ttl_for(active_event: Event, config: AggConfig) -> int:
    span_hours = (active_event.last_seen_at - active_event.first_seen_at).total_seconds() / 3600
    remaining_lifespan_hours = config.event_max_timespan_hours - span_hours
    grace_s = 2 * config.db_flush_interval_s   # every key outlives at least one flush (system-design.md §11.2)
    return int(min(config.sliding_window_hours, remaining_lifespan_hours) * 3600) + grace_s
```

The grace period matters near the hard cap, where the remaining lifespan — and so the bare TTL — can be seconds, or round to `0` (`EXPIRE 0` deletes the key at once), letting an ACKed merge expire before any flush persists it. It doesn't affect merging: `merge_or_create()` decides from its own span check, so a key outliving the cap just makes the next EntityEvent start a new Event, with the old one flushed first (§12.1).

Called by `AggregationService.process_entity_event()`'s unit-of-work closure (§12.1) immediately after `merge_or_create()`, passed to `EventAggregationStore.upsert_active_event()`'s `ttl_s` parameter.

### 12.5 `app/aggregation/event_aggregation_store.py` — `EventAggregationStore`

Owns the `agg:event:*` key format and the `Event` (Redis-active stage, §3.3) ↔ hash encode/decode, built on `RedisStateClient`'s generic primitives (§6.2). `AggregationService` sees only typed `Event`s, and `GroupCoroutine` doesn't touch it (§12.3). The hash stores five fields (`event_id`, `first_seen_at`, `last_seen_at`, `aggregation_updated_at`, `entity_event_ids`); `aggregation_updated_at` must survive the round-trip to `EventRepository.persist_events()` (§10.2). Other `Event` fields come back `None` (§3.3).

```python
def _key(exchange: str, stock_code: str, event_type_primary: str) -> str:
    return f"agg:event:{exchange}:{stock_code}:{event_type_primary}"

def _decode(exchange: str, stock_code: str, event_type_primary: str, fields: dict) -> Event:
    return Event(
        event_id=UUID(fields["event_id"]),
        exchange=exchange, stock_code=stock_code, event_type_primary=event_type_primary,
        first_seen_at=datetime.fromisoformat(fields["first_seen_at"]),
        last_seen_at=datetime.fromisoformat(fields["last_seen_at"]),
        aggregation_updated_at=datetime.fromisoformat(fields["aggregation_updated_at"]),
        entity_event_ids=[UUID(i) for i in json.loads(fields["entity_event_ids"])],
    )

class EventAggregationStore:
    def __init__(self):
        self._state_client = get_redis_state_client()   # §6.2

    async def get_active_event(self, exchange, stock_code, event_type_primary) -> Optional[Event]:
        fields = await self._state_client.hgetall(_key(exchange, stock_code, event_type_primary))
        if fields is None:
            return None
        return _decode(exchange, stock_code, event_type_primary, fields)

    async def upsert_active_event(self, active_event: Event, ttl_s: int) -> None:
        key = _key(active_event.exchange, active_event.stock_code, active_event.event_type_primary)
        encoded = {
            "event_id": str(active_event.event_id),
            "first_seen_at": active_event.first_seen_at.isoformat(),
            "last_seen_at": active_event.last_seen_at.isoformat(),
            "aggregation_updated_at": active_event.aggregation_updated_at.isoformat(),
            "entity_event_ids": json.dumps([str(i) for i in active_event.entity_event_ids]),
        }
        await self._state_client.hset(key, encoded)
        await self._state_client.expire(key, ttl_s)   # always both together — system-design.md §4.4:
                                                        # reset TTL to min(SLIDING_WINDOW_HOURS,
                                                        # remaining_lifespan) on every merge

    async def scan_all(self) -> list[Event]:
        """Parses each Redis key back into its (exchange, stock_code, event_type_primary)
        components and decodes the rest via _decode(), for flush()'s consumption.
        The Coroutine Manager's in-process dict already tracks *which* groups are
        active, but Redis remains the source of truth for each group's actual
        field values at flush time."""
        result = []
        for key in await self._state_client.scan("agg:event:*"):
            fields = await self._state_client.hgetall(key)
            if fields is None:   # expired between scan() and this hgetall() — skip, not an error
                continue
            _, _, exchange, stock_code, event_type_primary = key.split(":")
            result.append(_decode(exchange, stock_code, event_type_primary, fields))
        return result
```

`EventAggregationStore` is process-wide infrastructure (built on `RedisStateClient`, no constructor args of its own) — module-level singleton + getter (§5.1):

```python
_event_aggregation_store: Optional[EventAggregationStore] = None

def get_event_aggregation_store() -> EventAggregationStore:
    if _event_aggregation_store is None:
        raise ServiceUnavailableError("event aggregation store is not initialized")
    return _event_aggregation_store

def create_event_aggregation_store() -> EventAggregationStore:
    global _event_aggregation_store
    _event_aggregation_store = EventAggregationStore()
    return _event_aggregation_store
```

### 12.6 Error Handling (system-design.md §4.7)

| Scenario | Implementation |
|---|---|
| DB read failure (EntityEvent fetch) | `EntityEventRepository.fetch_entity_event()` (§10.1) raises inside `AggregationService.process_entity_event()`, before anything is enqueued; caught by `AggregationConsumer._route_one()` (§12.2): discard from `_enqueued`, log warning `aggregation_route_failed`, leave unACKed — the reclaim loop retries |
| EntityEvent row not found | `fetch_entity_event()` returns `None` — `stream:entity_event_completed` is only published after `persist_entities()` commits (§11.1), so this means the row is gone, and no redelivery can fix it. `process_entity_event()` logs `aggregation_entity_event_missing` (warning) and calls `on_processed()` (ACK) without enqueueing |
| `RedisStateClient` unreachable, or any other failure inside the unit of work (duplicate-check read, replaced-Event persist, merge, upsert, ACK) | Caught by `_process()`'s own `try`/`except` (§12.1): log ERROR `aggregation_process_failed` with `entity_event_id`/`group_key`/`step`, call `on_failed()` (discard from `_enqueued`, no ACK), don't re-raise — the group keeps running and the reclaim loop retries |
| DB Flush failure | `EventRepository.persist_events()` (§10.2) rolls back and re-raises after `_db_write_with_retry()`'s retries are exhausted (§5.2); `AggregationService.flush()` propagates; `_consumer_loop()` (§12.2) logs warning `aggregation_flush_failed` and continues — the next trigger retries the same still-active Redis state, nothing is lost |
| `GroupCoroutine` failure | Only reachable by a bug in `_process()`'s own failure handling (§12.3's backstop): `_run()` logs CRITICAL `aggregation_group_work_failed` and continues with the group's next item — the group, the consumer loop, and the layer Task all keep running. Each group runs in its own never-awaited Task, so this never reaches Coroutine Monitoring (§16) or `layers.aggregation`. Only the failing item is lost: its `_enqueued` entry stays set, so redeliveries are skipped until it's dead-lettered at `AGG_MAX_RETRY` — acceptable for a genuine bug, which redelivery couldn't fix anyway |
| `RedisStreamClient` unreachable | Per §6.1's reconnect behaviour |

---

## 13. Scoring Layer — `app/scoring/`

### 13.1 `app/scoring/scoring_service.py` — `ScoringService`

`ScoringService` is pure business logic: `score_event()` only. `ScoringConsumer` (§13.2) owns `stream:event_aggregated` consumption and reacts to how `score_event()` returns: normally (ACK, scored or stale alike, §13.4); `LLMRateLimitError` (no ACK, backoff); any other `LLMError` (no ACK, already anticipated); anything else (no ACK, unanticipated bug, §13.2).

Exceptions rather than a status value, as in `find_entity_events()` (§11.1): each Skill `failure_reason` is re-raised as itself (`raise result.failure_reason`), and `ScoringConsumer` logs it and distinguishes `LLMRateLimitError` from other `LLMError`s. Staleness (§13.4) is a normal return, since the consumer treats it like success.

#### `EventScore` — `app/models/event_score.py`

Defined in `app/models/` alongside `Event` (§3.3) and documented here because `ScoringService` constructs it (step 10); `EventScoreRepository` (§10.3) imports it.

```python
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.versioned_metadata import VersionedMetadata   # §3.2

class LLMScore(BaseModel):
    """Schema-validated output of EventLLMScoringSkill.run() (§8.4); wrapped by EventScore.llm_score_detail."""
    stock_impact_score: float   # [-5.0, +5.0]
    adjustment_reason: str      # <=80 Traditional Chinese characters

@dataclass
class RuleScore:
    """compute_rule_score()'s (§13.3) return type, wrapped by event_scores.rule_score_detail.
    A plain dataclass: a pure function with no LLM schema validation."""
    event_type_score: float
    source_authority_score: float
    sentiment_strength_score: float
    source_heat_score: float

    @property
    def base_rule_score(self) -> float:
        # weights: event_type 0.30, source_authority 0.25, sentiment_strength 0.15, source_heat 0.10
        # sums to 0.80 -> base_rule_score in [0,8]; recency's 0.20 share is
        # added back by the Cache Layer (§14.3), never re-normalized here
        return (self.event_type_score * 0.30 + self.source_authority_score * 0.25 +
                self.sentiment_strength_score * 0.15 + self.source_heat_score * 0.10)

@dataclass
class EventScore:
    score_id: UUID                # Primary key; generate with uuid.uuid4() on first creation
    event_id: UUID                # FK -> events.event_id; unique constraint
    abs_final_score: float        # Score Fusion result excluding recency
    base_rule_score: float        # Unsigned weighted rule score excluding recency and direction (∈ [0,8] — see §5.6)
    stock_impact_score: float     # EventLLMScoringSkill output; [-5, +5]
    direction: int                # ±1; sign(stock_impact_score), or sentiment-derived fallback
    llm_fallback: bool            # True if EventLLMScoringSkill failed
    rule_score_detail: VersionedMetadata[RuleScore]
    llm_score_detail: VersionedMetadata[LLMScore]
    scored_at: datetime           # EventLLMScoringSkill completion time UTC
    scored_aggregation_updated_at: datetime   # the events.aggregation_updated_at this score was computed for — score_event()'s already-scored check (§13.1 step 1)
    updated_at: datetime
    created_at: datetime
```

> **Not `Optional`, unlike `Event.brief_summary` (§3.3).** An `EventScore` row is created once, by Score Fusion, after both Rule Score and the LLM score (or fallback) complete (§13.4), so both columns are `JSONB NOT NULL` (§17).

> **Both fields wrap their computation's return type directly.** `compute_rule_score()` (§13.3) returns `rule_score_detail` already wrapped and versioned; `ScoringService` (§13.4) wraps `llm_score_detail` itself, with `version` from `EventLLMScoringResult.skill_log.skill_version`. The API's `RuleScoreDetail`/`LLMScoreDetail` (§3.5) intentionally differ (e.g. the API nests `stock_impact_score` one level up).

#### Class: `ScoringService`

```python
class ScoringService:
    def __init__(self, config: ScoringConfig):
        self._event_repo = get_event_repository()             # §10.2 — Event reads/writes
        self._entity_event_repo = get_entity_event_repository() # §10.1 — constituent_entity_events
        self._event_score_repo = get_event_score_repository() # §10.3 — the final conditional write
        self._redis_stream = get_redis_stream_client()      # §6.1 — publish only, never consumes here
        self._redis_state = get_redis_state_client()        # §6.2
        self._admin_client = get_admin_client()              # §9.2
        self._config = config

    async def score_event(self, event_id: UUID) -> None:
        """Pure business logic: fetch the Event, its constituent EntityEvents, and the
        source weights (§13.5); start BriefSummarySkill (§8.3) as a background Task while
        counting active sources and computing Rule Score (§13.3), then run EventLLMScoringSkill (§8.4),
        which needs BriefSummarySkill's output. Each Skill is a fresh instance per call
        (§8.2). Check staleness at each checkpoint (§13.4), fuse the scores, write
        event_scores, and publish stream:event_scored. Returns normally on success or
        staleness; raises the Skill's failure_reason on an anticipated LLM failure (§11.1).
        Safe to call directly, concurrently. ScoringConsumer (§13.2) is its only caller."""
```

`ScoringService` is itself a module-level singleton + getter (§5.1), same shape as `EntityAnalysisService` (§11.1):

```python
_scoring_service: Optional[ScoringService] = None

def get_scoring_service() -> ScoringService:
    if _scoring_service is None:
        raise ServiceUnavailableError("scoring service is not initialized")
    return _scoring_service

def create_scoring_service(config: ScoringConfig) -> ScoringService:
    global _scoring_service
    _scoring_service = ScoringService(config)
    return _scoring_service
```

`create_scoring_service()` is called from `main.py` (§16) with `config.scoring`.

#### Method: `ScoringService.score_event()` internals

```
Purpose : 1. event = await self._event_repo.fetch_event(event_id) (§10.2).
             None: log warning scoring_event_missing; return (ACKed, §13.6).
             expected = event.aggregation_updated_at.
             Already scored? await self._event_score_repo.fetch_scored_aggregation_updated_at(
             event_id) == expected: this exact version was scored before — a duplicate
             or redelivered message, or a crash between the score write and its publish.
             Skip both Skills: publish stream:event_scored, log info scoring_already_scored,
             return (system-design.md §5.4). No row is written; the Event keeps its one score.
             constituent_entity_events
             = await self._entity_event_repo.fetch_entity_events_for_event(event_id)
             (§10.1). source_names = [s.source_name for s in event.source_list].
          2. source_weights = await get_source_weights(self._config, source_names)
             (§13.5) — fetched before the Skill starts, since BriefSummarySkill needs it
             for input ordering and its fallback (§8.3). One Redis read in the common case.
             Summary already made for this version? event.brief_summary is not None and
             event.brief_summary_aggregation_updated_at == expected — a retry after a
             later step failed: reuse it (brief_summary = event.brief_summary,
             brief_from_fallback = event.brief_summary_fallback), log info
             brief_summary_reused, start no Task, and skip steps 4–5 (system-design.md §5.4).
             Otherwise brief_task = asyncio.create_task(BriefSummarySkill(
             BriefSummaryInput(event, constituent_entity_events, source_weights)).run()) —
             fresh instance per call (§8.3), never raises. Started in the background and
             not gathered with step 3, so step 3 doesn't wait for it.
          3. total_active_sources = await count_active_sources(self._config)
             (§13.5); sentiment_agg = compute_sentiment_aggregation(
             constituent_entity_events, source_weights) (§13.3) — computed once, used
             by both Rule Score and Score Fusion (step 10); rule_score_detail =
             compute_rule_score(event, sentiment_agg, source_weights,
             total_active_sources, self._config) (§13.3) — already the versioned VersionedMetadata[RuleScore]
             that EventScore.rule_score_detail wraps. Synchronous and pure; runs while
             brief_task is underway.
             If step 3 raises while brief_task is running, don't abandon the Task:
             - an ordinary Exception (in practice count_active_sources()'s Redis read):
               await brief_task; if it succeeded, write it as step 5 does
               (update_brief_summary(event_id, ..., expected, fallback)) so the retry
               reuses it (step 2) — a failure of that write is logged as warning
               brief_summary_salvage_failed and doesn't replace the original error;
               then re-raise step 3's exception (unACKed, retried). If brief_task itself
               failed, there's nothing to store — it already logged its own
               brief_summary_completed.
             - asyncio.CancelledError (shutdown, layer-Task restart): cancel brief_task
               and re-raise, rather than holding up shutdown for an LLM call.
          4. (Not reused only.) result = await brief_task — picks up whatever step 2 already
             finished (the common case) or finishes waiting for it now.
             result.failure_reason set: log brief_summary_completed
             (entity_event_ids doesn't apply here — see §8.6); raise
             result.failure_reason — same as find_entity_events() (§11.1).
             On success: brief_summary = VersionedMetadata(version=result.skill_log.skill_version,
             output=result.output) — result.output (a BriefSummary) wrapped
             as-is, no field-by-field conversion (§3.3);
             brief_from_fallback = result.skill_log.fallback.
          5. (Not reused only.) self._event_repo.update_brief_summary(event_id, brief_summary,
             expected, brief_from_fallback) (§10.2); log brief_summary_completed — unconditional: the
             Skill genuinely succeeded (step 4), independent of whatever
             staleness turns out to be true next.
          6. _is_stale(event_id, expected) (§13.4)? return (ACKed the same
             as success, §13.2 — nothing further to do). Positioned here,
             not before step 5, so this checkpoint protects what actually
             needs protecting: the expensive EventLLMScoringSkill LLM call
             (step 7) about to run, not the cheap write just above it.
          7. result2 = await EventLLMScoringSkill(EventLLMScoringInput(event,
             constituent_entity_events[0].stock, brief_summary,
             brief_from_fallback)).run() — fresh instance per call (§8.4); every
             constituent shares the Event's stock_code (§12.3), so [0] is fine.
             scored_at = datetime.now(timezone.utc), captured as soon as run() returns.
          8. EventLLMScoringSkill's result.failure_reason set: log
             event_scoring_completed; raise result.failure_reason.
          9. _is_stale(event_id, expected) again? return.
          10. _score_fusion(rule_score_detail.output, result2.output, sentiment_agg)
              (§13.4) — unwrapping .output here since _score_fusion() (design
              rationale: system-design.md §5.7) works on the bare RuleScore value, not
              its VersionedMetadata wrapper;
              build an EventScore (above: score_id=uuid4(), event_id,
              scored_at (from step 7), llm_fallback=result2.skill_log.fallback,
              scored_aggregation_updated_at=expected, the fused fields —
              updated_at/created_at unused placeholders, ignored by
              persist_score_if_not_stale()); log event_scoring_completed;
              self._event_score_repo.persist_score_if_not_stale(score, expected)
              (§10.3). None (stale): return. EventScore (scored): publish
              stream:event_scored, then return — the caller doesn't need
              to tell these two apart (both already logged
              event_scoring_completed above), only whether to publish.
          Steps 1 onward have no outer try/except of their own — an
          unanticipated bug propagates uncaught to ScoringConsumer's own
          outer except Exception (see callout below), same split as
          EntityAnalysisService/CleanedNewsConsumer (§11.1/§11.2).
```

`LLMSchemaViolationError` never triggers a `raise` for either Skill — both have a defined fallback for it, caught in their own `llm_process()` (§8.3–§8.4), so that case is indistinguishable from success at this layer (`result.output` is set, `result.failure_reason` is `None`, `result.skill_log.fallback` is `True`) and steps 3/7 above don't trigger.

### 13.2 `app/scoring/scoring_consumer.py` — `ScoringConsumer`

Owns `stream:event_aggregated` consumption (loops, in-flight guard, `WorkerPool` dispatch, ACK/backoff), calling `ScoringService.score_event()` (§13.1). Split rationale: §13.1.

```python
class ScoringConsumer:
    def __init__(self, worker_pool: WorkerPool, config: ScoringConfig):
        self._service = get_scoring_service()               # §13.1
        self._redis_stream = get_redis_stream_client()      # §6.1 — consumes, reclaims, ACKs
        self._worker_pool = worker_pool
        self._config = config
        self._in_process: set[UUID] = set()   # event_id currently being scored, regardless of trigger

    async def start(self) -> None:
        """Entry point, called from main.py's lifespan (§16); rationale as
        CleanedNewsConsumer.start() (§11.2). Uses run_supervised() (§6.1)."""
        await self._redis_stream.create_group_if_not_exists(STREAM_EVENT_AGGREGATED, CONSUMER_GROUP_SCORING)
        await run_supervised(self._consumer_loop(), self._reclaim_loop())

    async def _consumer_loop(self) -> None:
        """Reads batches from stream:event_aggregated and dispatches each
        message to worker_pool — see Flowchart below."""

    async def _reclaim_loop(self) -> None:
        """Every STREAM_CLAIM_TIMEOUT_MS, reclaims timed-out messages via
        autoclaim(), dead-lettering ones past SCORING_MAX_RETRY — see
        below."""
```

#### In-flight Guard

In-memory, owned by `ScoringConsumer`, no locking needed (asyncio single-threaded), on the same synchronous check-then-insert pattern as `CleanedNewsConsumer`'s `processing_cleaned_ids` (§11.2) and `AggregationConsumer`'s `_enqueued` (§12.2) — checked and inserted as the first thing `_process_one()` does, with no `await` in between, so it's safe against a duplicate arriving concurrently from `_reclaim_loop()` (§6.1), not just from the same `read_from()` batch. Cleared in a `finally` when processing ends (success, drop, or error); empty on restart.

> `_in_process` closes before the DB fetch (§6.1), so a second trigger for the same `event_id` can't enter while the first is scoring. Each staleness checkpoint (§13.4) re-reads `events.aggregation_updated_at` (one cheap column) and compares it to the value captured at the start of `_process_one()`. The final conditional UPSERT (`persist_score_if_not_stale()`, §10.3) is the correctness mechanism; a re-aggregation during scoring costs at most one wasted pass.

#### `_consumer_loop()` (see Flowchart, below)

Reads batches (`read_from()`) and dispatches each message to `worker_pool` (`WorkerPool.dispatch()`, §11.3); nodes `A`–`ADD` of the flowchart below are its entire body.

#### Method: `ScoringConsumer._process_one(event_id: UUID, message_id: str) -> None`

Both `_consumer_loop()` (above) and `_reclaim_loop()` (below) call `_process_one(event_id, message_id)` — the diagram below is that shared method's body. Node `B` is the single call into `ScoringService.score_event(event_id)`, described above — its own internal steps aren't redrawn here.

```mermaid
flowchart TD
    A([_consumer_loop]) -->|read_from, empty| A
    A -->|Message: event_id| GATE{event_id in\n_in_process Set?}
    GATE -->|Yes| SKIP[Skip - already being scored\nleave unACKed for this delivery]
    GATE -->|No| ADD[Add to _in_process Set — no await\nbetween this check and this insert]
    ADD --> B[service.score_event event_id\nScoringService — see above]
    B -->|Returns normally\nscored or stale| P[Remove from _in_process\nACK]
    B -->|Raises LLMRateLimitError| H1[worker_pool.trigger_backoff\nRemove from _in_process\nDo not ACK]
    B -->|Raises LLMProviderError, LLMAuthenticationError,\nor LLMSchemaViolationError| H2[Remove from _in_process\nDo not ACK]
    SKIP & P & H1 & H2 --> A
```

> **Neither Skill's `run()` raises:** `score_event()` branches on `result.failure_reason`. `result.skill_log` is always populated, and `ScoringService` logs it as-is (both Skills take `event.event_id` directly, unlike `EntityAnalysisSkill`, §8.2).

> **`_process_one()` uses the same three-tier `try`/`except` as `CleanedNewsConsumer` (§11.2)**, from the `_in_process` Set-insert onward: `LLMRateLimitError` (backoff, then logged), `SAPIError` (logged at its own `log_level` with its `dev_message` and `event_id` — covers every other `LLMError` re-raised by `score_event()`), and `Exception` (log ERROR `f"Unhandled exception: {exc!r}"` with `exc_info`, remove `event_id` from `_in_process` in a `finally`, leave unACKed, don't re-raise). A repeating failure dead-letters at `delivery_count >= SCORING_MAX_RETRY` → `stream:scoring_dead_letter` (§6.1).

#### `_reclaim_loop()` (§6.1's Reclaim and Dead Letter pattern, applied here)

```
Purpose : while True: await asyncio.sleep(STREAM_CLAIM_TIMEOUT_MS / 1000);
          try: autoclaim(stream=STREAM_EVENT_AGGREGATED,
          group=CONSUMER_GROUP_SCORING, consumer=config.consumer_name,
          min_idle_ms=STREAM_CLAIM_TIMEOUT_MS, count=SCORING_BATCH_SIZE).
          For each reclaimed message: if delivery_count >= SCORING_MAX_RETRY,
          publish to STREAM_SCORING_DEAD_LETTER and ACK (never reaches
          _process_one()). Otherwise, extract event_id from fields and call
          _process_one(event_id, message_id) — identical call as
          _consumer_loop() uses.
          except Exception: log warning scoring_reclaim_sweep_failed
          with exc_info, do not re-raise — retried next tick.
```

`except Exception` per §6.1.

### 13.3 `app/scoring/rule_score.py`

#### Function: `compute_rule_score(event: Event, sentiment_agg: SentimentAggregation, source_weights: dict[str, float], total_active_sources: int, config: ScoringConfig) -> VersionedMetadata[RuleScore]`

Weights and `event_type_score` mapping: system-design.md §5.6 (rationale there). Returns the versioned, ready-to-persist value directly: `RULE_SCORE_VERSION` is stamped here so `ScoringService` needn't import it. `RuleScore` is imported from `app.models.event_score` (§13.1).

`event_type_score` is operator-configurable (§4.1's `ScoringConfig.event_type_score`). `_event_type_score_for()` is a pure lookup against the passed-in `config`, so this function does no I/O; `sentiment_agg`/`source_weights`/`total_active_sources` are resolved by the caller (§13.1 step 3).

```python
from app.models.event_score import RuleScore   # §13.1
from app.models.versioned_metadata import VersionedMetadata   # §3.2
from app.config import ScoringConfig   # §4.1

RULE_SCORE_VERSION = "v1.0.0"   # no LLM call, but a versioned scoring model (system-design.md §5.6)

def _event_type_score_for(event_type_primary: str, config: ScoringConfig) -> float:
    # config.event_type_score (§4.1) pairs each EventTypeEnum member with its score.
    return next(e.score for e in config.event_type_score if e.event_type == event_type_primary)

def compute_rule_score(event, sentiment_agg, source_weights, total_active_sources, config) -> VersionedMetadata[RuleScore]:
    event_type_score = _event_type_score_for(event.event_type_primary, config)
    source_authority_score = max(source_weights.get(s.source_name, 0.0) for s in event.source_list)
    sentiment_strength_score = sentiment_agg.sentiment_strength_score * 10   # [0,1] → [0,10], like the other three dimensions (system-design.md §5.6)
    source_heat_score = min(10.0, (event.source_count / total_active_sources) * 10)
    rule_score = RuleScore(event_type_score, source_authority_score, sentiment_strength_score, source_heat_score)
    return VersionedMetadata(version=RULE_SCORE_VERSION, output=rule_score)
```

> **`TOTAL_ACTIVE_SOURCES` cold-start fallback** (system-design.md §5.6): when `source:config` is cold *and* Admin is unreachable, `count_active_sources()` (§13.5) returns `config.total_active_sources_default`; `get_source_weights()` has its own independent fallback. Both arrive at `compute_rule_score()` already resolved.

#### Function: `compute_sentiment_aggregation(constituent_entity_events: list[EntityEvent], source_weights: dict[str, float]) -> SentimentAggregation`

Design rationale: system-design.md §5.8.

```python
@dataclass
class SentimentAggregation:
    weighted_signed_score: float     # feeds Score Fusion's direction fallback (§13.4)
    sentiment_strength_score: float  # [0,1], the weighted mean sentiment_score; compute_rule_score() scales it ×10 into RuleScore's [0,10]

_DIRECTION_COEFFICIENT = {"POSITIVE": 1, "NEUTRAL": 0, "NEGATIVE": -1}

def compute_sentiment_aggregation(constituent_entity_events, source_weights) -> SentimentAggregation:
    total_weight = 0.0
    weighted_signed_sum = 0.0
    weighted_strength_sum = 0.0
    for ee in constituent_entity_events:
        w = source_weights.get(ee.source_name, 0.0)
        signed = ee.analysis_outcome.output.sentiment_score * _DIRECTION_COEFFICIENT[ee.analysis_outcome.output.sentiment_label]
        weighted_signed_sum += signed * w
        weighted_strength_sum += ee.analysis_outcome.output.sentiment_score * w
        total_weight += w
    if total_weight == 0:
        return SentimentAggregation(0.0, 0.0)
    return SentimentAggregation(weighted_signed_sum / total_weight, weighted_strength_sum / total_weight)
```

The caller computes `SentimentAggregation` once (§13.1 step 3) and passes it to both `compute_rule_score()` and `_score_fusion()` (§13.4), since both values come from the same weighted pass over the constituent EntityEvents. `compute_rule_score()` scales `sentiment_strength_score` from its natural `[0,1]` (the range of each `sentiment_score`) to the `[0,10]` every Rule Score dimension uses — without that, the dimension's 0.15 weight could contribute at most 0.15 instead of 1.5. `weighted_signed_score` stays in `[-1,1]`: Score Fusion only uses its sign.

### 13.4 Score Fusion and Stale-Data Detection — `app/scoring/scoring_service.py`

#### Function: `_score_fusion(rule_score: RuleScore, llm_score: LLMScore, sentiment_agg: SentimentAggregation) -> tuple[int, float, float]`

Design rationale (direction derivation, persistence): system-design.md §5.7.

```python
def _score_fusion(rule_score, llm_score, sentiment_agg) -> tuple[int, float, float]:
    if llm_score.stock_impact_score > 0:
        direction = 1
    elif llm_score.stock_impact_score < 0:
        direction = -1
    else:
        direction = 1 if sentiment_agg.weighted_signed_score >= 0 else -1

    base_rule_score = rule_score.base_rule_score          # stored, unsigned, excludes recency (∈ [0,8])
    signed_rule_score = base_rule_score * direction
    raw_final_score = signed_rule_score * 0.67 + llm_score.stock_impact_score * 0.33 * 2
    abs_final_score = abs(raw_final_score)
    return direction, base_rule_score, abs_final_score
```

> `base_rule_score` returned here is the **stored** (recency-excluded, unsigned) value written to `event_scores.base_rule_score` — not the signed, recency-inclusive value the Cache Layer exposes in the API response (§6.6/§14.3). Do not conflate the two; they share a name across sections of the system-design doc but are computed at different pipeline stages.

`ScoringService` builds only `llm_score_detail` before the write; `rule_score_detail` is already finished (§13.3):

```python
llm_score_detail = VersionedMetadata(
    version=llm_score_result.skill_log.skill_version,   # llm_score_result: the EventLLMScoringResult from step 7's await, not llm_score (its unwrapped .output, used above)
    output=llm_score,   # already an LLMScore — no field-by-field reconstruction, same pattern brief_summary's wrap uses (§3.3)
)
```

#### Stale-Data Detection (system-design.md §5.5) — implemented as three checkpoints inside `ScoringService`, not a separate module

```python
async def _is_stale(self, event_id: UUID, expected_aggregation_updated_at: datetime) -> bool:
    current = await self._event_repo.fetch_aggregation_updated_at(event_id)   # §10.2
    return current is None or current != expected_aggregation_updated_at
```

Checked after `BriefSummarySkill` completes, after `EventLLMScoringSkill` completes, and a third time at the DB write via `persist_score_if_not_stale()`'s conditional UPSERT (§10.3), which gates both the insert and update branches. `fetch_aggregation_updated_at()` (§10.2) is a single-column read, cheap enough to avoid wasted LLM calls; the UPSERT is the correctness guarantee.

### 13.5 Source config access — `app/scoring/rule_score.py` — `get_source_weights`, `count_active_sources`

Owns the `source:config` key, built on `RedisStateClient`'s generic primitives; `AdminClient` is in §9.2. It lives alongside `compute_rule_score()` (§13.3), its sole consumer.

Two independent functions: `source_weights` is scoped to this Event's sources, `total_active_sources` is a global count. `get_source_weights()` calls `AdminClient` on a cold miss; `count_active_sources()` only reads Redis and falls back to its config value, since a full Admin refresh just for a count isn't worth the round-trip.

```python
from app.common.errors import ServiceUnavailableError

_SOURCE_CONFIG_KEY = "source:config"
_DEFAULT_TIER_WEIGHTS = {"P1": 9.0, "P2": 6.0, "P3": 4.0}   # fallback only — see below

_SOURCE_TIER = {
    "HKEX": "P1", "MINGPAO": "P2", "AASTOCKS": "P2", "YAHOO_HK": "P2",
}   # Mirrors admin-tad.md §4.1's data_sources seed data (authority_weight 9.0/6.0/6.0/6.0) and SADI's
    # 4 source_name values (stock-assistant-data-ingestion/docs/implementation.md §8.4). Dual-failure
    # fallback only (Admin unreachable AND source:config cold); update if admin-tad.md's sources/tiers change.

def _admin_tier_for(source_name: str) -> Optional[str]:
    return _SOURCE_TIER.get(source_name)   # None for an unrecognized source_name — the
                                            # caller's _DEFAULT_TIER_WEIGHTS.get(..., 0.0)
                                            # already handles that safely (weight 0.0)

def _fallback_weights(source_names: list[str]) -> dict[str, float]:
    return {name: _DEFAULT_TIER_WEIGHTS.get(_admin_tier_for(name), 0.0) for name in source_names}

async def get_source_weights(config: ScoringConfig, source_names: list[str]) -> dict[str, float]:
    """
    system-design.md §1.5's refresh-on-miss flow: HMGET source:config first; if none of the
    requested names came back — a cold key, or one that expired since the last read — call
    AdminClient.fetch_sources(), write the result with TTL=SOURCE_CONFIG_TTL_S, and serve from
    it. Reading first (no separate EXISTS) means a key expiring mid-call can't yield an empty
    result that silently scores every source 0.0. If Admin fails — or returns no sources, which can't be cached (Redis rejects an
    empty HSET) and isn't a usable config anyway — fall back to default tier weights
    (P1=9, P2=6, P3=4) via the local _SOURCE_TIER mirror (above), without caching them.
    """
    state_client = get_redis_state_client()   # §6.2
    admin_client = get_admin_client()         # §9.2
    raw = await state_client.hmget(_SOURCE_CONFIG_KEY, source_names)
    cached = {name: float(value) for name, value in raw.items() if value is not None}
    if cached:
        return cached   # a single missing name is an unknown source — the caller weighs it 0.0

    try:
        sources = await admin_client.fetch_sources()
    except ServiceUnavailableError:
        return _fallback_weights(source_names)
    if not sources:
        logger.warning("source_config_empty_from_admin")
        return _fallback_weights(source_names)
    weights = {s["source_name"]: str(s["authority_weight"]) for s in sources}
    await state_client.hset(_SOURCE_CONFIG_KEY, weights)
    await state_client.expire(_SOURCE_CONFIG_KEY, config.source_config_ttl_s)
    return {name: float(weights[name]) for name in source_names if name in weights}

async def count_active_sources(config: ScoringConfig) -> int:
    """
    HLEN source:config: the global source count, not scoped to one Event. Never calls
    AdminClient: a cold or missing key falls back to config.total_active_sources_default
    (system-design.md §5.6). Read-only; get_source_weights()'s refresh keeps the hash warm.
    """
    total = await get_redis_state_client().hlen(_SOURCE_CONFIG_KEY)   # §6.2 — 0 on a missing/empty key
    return total if total > 0 else config.total_active_sources_default
```

`except ServiceUnavailableError`, not `except Exception`, so an unrelated bug (e.g. a `KeyError` from a malformed response) isn't treated as an outage.

> `_admin_tier_for(name)` is used only by the dual-failure fallback branch above; Admin's `data_sources` table (admin-tad.md §4.1) remains the source of truth for tiers.

### 13.6 Error Handling (system-design.md §5.11)

| Scenario | Implementation |
|---|---|
| DB read failure | `EventRepository.fetch_event()`/`EntityEventRepository.fetch_entity_events_for_event()` (§10.2/§10.1) raise inside `ScoringService.score_event()`; propagates uncaught to `ScoringConsumer`'s outer `except Exception` (§13.2); do not ACK |
| Event row not found | `fetch_event()` returns `None` — `stream:event_aggregated` is only published after `persist_events()` commits (§12.1), so this means the row is gone, and no redelivery can fix it. `score_event()` logs `scoring_event_missing` (warning) and returns; ACKed the same as success (§13.2) |
| BriefSummarySkill: `LLMSchemaViolationError` | Fallback applied inside the Skill (§8.3) — `result.failure_reason` stays `None`; `ScoringService.score_event()` continues normally |
| EventLLMScoringSkill: `LLMSchemaViolationError` | Fallback applied inside the Skill (§8.4) — `result.failure_reason` stays `None`; `ScoringService.score_event()` continues normally |
| Either Skill: `LLMRateLimitError`/`LLMProviderError`/`LLMAuthenticationError` | Caught inside `SkillBase.run()` (§8.1, inherited by both Skills) → `result.failure_reason=err` (the caught exception itself), never raised by `run()` itself; `ScoringService.score_event()` logs the Skill's completion event from `result.skill_log` and re-raises `result.failure_reason` directly, same as `find_entity_events()` (§11.1); `ScoringConsumer` calls `WorkerPool.trigger_backoff()` only for `LLMRateLimitError`, removes from `_in_process`, does not ACK either way. `LLMAuthenticationError` is logged at CRITICAL, its own `log_level` (§7.2) |
| `source:config` unreachable | The Redis error propagates from `get_source_weights()`/`count_active_sources()` (§13.5) — no fallback weights, no Admin call — to `ScoringConsumer`'s outer `except Exception` (§13.2); do not ACK (rationale: system-design.md §5.11) |
| DB write failure | `_db_write_with_retry()` exhausts retries inside `EventRepository.update_brief_summary()`/`EventScoreRepository.persist_score_if_not_stale()` (§10.2/§10.3), raises; propagates uncaught to `ScoringConsumer`'s outer `except Exception` (§13.2); do not ACK |
| `RedisStreamClient` unreachable | Per §6.1 |

---

## 14. Cache Layer — `app/cache/`

### 14.1 `app/cache/cache_service.py` — `CacheService`

Split like the other layers (§11.1, §12.1, §13.1), along the batch boundary: `CacheService` is pure business logic: `build_initial_version()` (once, from `main.py`, §16), `build_new_version(event_ids)` (once per batch, and with an empty batch for the daily refresh, §14.9) and `orphan_cleanup_loop()`. `CacheConsumer` (§14.2) owns `stream:event_scored` consumption: the batching loop with inline reclaim, dead-letter routing and the batch ACK.

The build pipeline (§14.3–§14.5) pairs each `Event` with its `EventScore` by `event_id`, from two repository reads (`fetch_events()`, `fetch_scores()`, §10.2/§10.3) rather than a join (rule 9): `events: dict[UUID, Event]` plus `scores: list[EventScore]`. `_build_candidate_records()` (§14.3) is the only place that needs both halves.

The cache's own record shape is typed: `CacheRecord`/`CacheEventDetail`/`CacheScoreDetail` mirror `ScoredEventRecord`/`EventDetail`/`ScoreDetail` (§3.5) plus three internal-only fields (§14.3), with `to_dict()`/`from_dict()` for the raw JSON in `write_cache_version()`/`read_cache_version()` (§14.5). They are local to `app/cache/cache_service.py`, like `GroupKey` (§12.3): a Redis-cache shape, not a DB row.

```python
@dataclass
class CacheEventDetail:
    event_type_secondary: list[str]
    source_count: int
    source_list: list[SourceListItem]           # §3.3
    summary_short: Optional[str]
    summary_full: Optional[str]
    key_numbers: list[str]
    aggregation_updated_at: datetime            # internal-only — §14.3's recency recompute input

    def to_dict(self) -> dict:
        return {
            "event_type_secondary": self.event_type_secondary, "source_count": self.source_count,
            "source_list": [dataclasses.asdict(item) for item in self.source_list],
            "summary_short": self.summary_short, "summary_full": self.summary_full, "key_numbers": self.key_numbers,
            "aggregation_updated_at": self.aggregation_updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CacheEventDetail":
        return cls(
            event_type_secondary=d["event_type_secondary"], source_count=d["source_count"],
            source_list=[SourceListItem(**item) for item in d["source_list"]],
            summary_short=d["summary_short"], summary_full=d["summary_full"], key_numbers=d["key_numbers"],
            aggregation_updated_at=datetime.fromisoformat(d["aggregation_updated_at"]),
        )

@dataclass
class CacheLLMScoreDetail:
    """The API's LLMScoreDetail shape (§3.5): EventScore.llm_score_detail's adjustment_reason
    plus its version and EventScore.scored_at, which a bare LLMScore doesn't carry."""
    adjustment_reason: str
    score_version: str
    scored_at: datetime

    def to_dict(self) -> dict:
        return {"adjustment_reason": self.adjustment_reason, "score_version": self.score_version,
                "scored_at": self.scored_at.isoformat()}

    @classmethod
    def from_dict(cls, d: dict) -> "CacheLLMScoreDetail":
        return cls(adjustment_reason=d["adjustment_reason"], score_version=d["score_version"],
                   scored_at=datetime.fromisoformat(d["scored_at"]))

@dataclass
class CacheScoreDetail:
    stock_impact_score: float
    llm_fallback: bool
    rule_score_detail: RuleScore                 # §13.1 — unwrapped: no VersionedMetadata here, system-design.md §6.6 strips the version wrapper for the cache
    llm_score_detail: CacheLLMScoreDetail          # above — stock_impact_score lives one level up, as in the API
    stored_base_rule_score: float               # internal-only — unsigned, pre-recency (§14.3)
    direction: int                                 # internal-only — §14.3's recency recompute input

    def to_dict(self) -> dict:
        return {
            "stock_impact_score": self.stock_impact_score, "llm_fallback": self.llm_fallback,
            "rule_score_detail": dataclasses.asdict(self.rule_score_detail),   # plain dataclass (§13.1)
            "llm_score_detail": self.llm_score_detail.to_dict(),
            "stored_base_rule_score": self.stored_base_rule_score, "direction": self.direction,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CacheScoreDetail":
        return cls(
            stock_impact_score=d["stock_impact_score"], llm_fallback=d["llm_fallback"],
            rule_score_detail=RuleScore(**d["rule_score_detail"]), llm_score_detail=CacheLLMScoreDetail.from_dict(d["llm_score_detail"]),
            stored_base_rule_score=d["stored_base_rule_score"], direction=d["direction"],
        )

@dataclass
class CacheRecord:
    """One entry in the Morning Brief cache (§14.5). event/score carry three internal-only
    fields that ScoredEventRecord (§3.5) doesn't declare; Pydantic's default extra="ignore"
    drops them when get_morning_brief() (§15.3) validates a record."""
    event_id: UUID
    exchange: str
    stock_code: str
    event_type_primary: str
    abs_final_score: float          # display — recency-inclusive
    base_rule_score: float          # display — signed, recency-inclusive
    first_seen_at: datetime
    last_seen_at: datetime
    event: CacheEventDetail
    score: CacheScoreDetail

    def to_dict(self) -> dict:
        return {
            "event_id": str(self.event_id), "exchange": self.exchange, "stock_code": self.stock_code,
            "event_type_primary": self.event_type_primary,
            "abs_final_score": self.abs_final_score, "base_rule_score": self.base_rule_score,
            "first_seen_at": self.first_seen_at.isoformat(), "last_seen_at": self.last_seen_at.isoformat(),
            "event": self.event.to_dict(), "score": self.score.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CacheRecord":
        return cls(
            event_id=UUID(d["event_id"]), exchange=d["exchange"], stock_code=d["stock_code"],
            event_type_primary=d["event_type_primary"],
            abs_final_score=d["abs_final_score"], base_rule_score=d["base_rule_score"],
            first_seen_at=datetime.fromisoformat(d["first_seen_at"]), last_seen_at=datetime.fromisoformat(d["last_seen_at"]),
            event=CacheEventDetail.from_dict(d["event"]), score=CacheScoreDetail.from_dict(d["score"]),
        )

@dataclass
class Candidate:
    """A record plus its stored (pre-recency) abs_final_score, which _incremental_merge()
    (§14.4) needs for the threshold check."""
    record: CacheRecord
    stored_abs_final_score: float

class CacheService:
    def __init__(self, config: CacheConfig):
        self._event_repo = get_event_repository()               # §10.2 — Event reads
        self._event_score_repo = get_event_score_repository()  # §10.3 — EventScore reads
        self._cache_store = get_cache_store()             # §10.4
        self._config = config
        self._build_lock = asyncio.Lock()   # every build runs under it — score-triggered or the daily refresh (§14.9)

    async def _refresh_cached_records(self, current_version: int) -> dict[UUID, CacheRecord]:
        """Recomputes recency for every Event already in the cache, with no DB read: parses the
        cache's JSON via CacheRecord.from_dict() and calls apply_recency() (§14.3) from the
        fixed, never-adjusted internal fields (aggregation_updated_at,
        score.stored_base_rule_score, score.direction) plus score.stock_impact_score.
        Recomputing from fixed values avoids compounding decay (§14.3). Returns
        {event_id: record} for _incremental_merge() (§14.4) as `current`."""
        raw = await self._cache_store.read_cache_version(current_version)
        if raw is None:   # evicted/expired between get_current_version() and this read
            return {}
        refreshed = {}
        for item in json.loads(raw):
            record = CacheRecord.from_dict(item)
            record.base_rule_score, record.abs_final_score = apply_recency(
                record.event.aggregation_updated_at, record.score.stored_base_rule_score,
                record.score.direction, record.score.stock_impact_score, self._config.recency_decay_lambda,
            )   # display fields only — record.score's 3 internal fields are untouched, carried forward as-is
            refreshed[record.event_id] = record
        return refreshed

    async def build_initial_version(self) -> None:
        """Bootstraps the first cache version. Called once from main.py's lifespan (§16),
        before CacheConsumer's Task starts. Under self._build_lock: if
        self._cache_store.get_current_version() (§10.4) is not None, return — a prior
        version survived in Redis; otherwise await self._build_full()."""

    async def _build_full(self) -> int:
        """A cache version from every scored Event — the only full DB fetch. Used by
        build_initial_version() at startup, and by build_new_version() when no version
        exists yet (the startup build failed), so a DB that was down at boot still gets
        the whole history into the cache on the first successful build.
        1. scores = await self._event_score_repo.fetch_scores() (§10.3) — full,
           keyset-paginated.
        2. events = {} ; for each chunk of self._config.full_fetch_page_size ids in
           [s.event_id for s in scores]: events.update({e.event_id: e for e in await
           self._event_repo.fetch_events(chunk)}) (§10.2) — chunked like the score fetch, so
           no single query carries every scored Event's id (event_scores grows without bound).
        3. candidates = _build_candidate_records(scores, events,
           self._config.recency_decay_lambda) (§14.3).
        4. merged = _incremental_merge({}, candidates, self._config.noise_filter_threshold)
           (§14.4) — against an empty dict this is a plain threshold filter.
        5. sorted_records = sorted(merged.values(), key=ranking_key, reverse=True) (§14.3).
        6. return await self._write_new_version(sorted_records, old_version=None, started=started) (§14.5),
           where started = time.perf_counter() taken on entry.
        Called with self._build_lock already held. Catches nothing. At startup, main.py's lifespan (§16) logs CRITICAL and continues
        with current_version unbuilt; from build_new_version(), CacheConsumer's catch (§14.2)
        leaves the batch unACKed, and the next batch tries the full build again."""

    async def build_new_version(self, event_ids: list[UUID]) -> int:
        """Everything from §14.2's flowchart node BUILD onward; incremental once a version exists.
        Returns the version written. The whole body runs under self._build_lock. An empty
        event_ids is the daily refresh (§14.9): steps 3–4 are skipped (no DB read) and step 6
        merges nothing, so the build only recomputes recency for what's already cached.
        1. current_version = await self._cache_store.get_current_version() (§10.4).
           None — only if build_initial_version() failed at startup (§16): await
           self._build_full() and return. The full fetch already includes this batch's
           scores (they're written before stream:event_scored is published, §13.1), so the
           cache starts from the whole history, not from this batch alone.
        2. current = await self._refresh_cached_records(current_version) — CPU only, no DB read.
        3. scores = await self._event_score_repo.fetch_scores(event_ids) (§10.3) — this
           batch only.
        4. events = {e.event_id: e for e in await
           self._event_repo.fetch_events([s.event_id for s in scores])} (§10.2).
        5. candidates = _build_candidate_records(scores, events,
           self._config.recency_decay_lambda) (§14.3) — recency computed fresh.
        6. merged = _incremental_merge(current, candidates,
           self._config.noise_filter_threshold) (§14.4).
        7. sorted_records = sorted(merged.values(), key=ranking_key, reverse=True) (§14.3).
        8. return await self._write_new_version(sorted_records, current_version, started) (§14.5),
           where started = time.perf_counter() taken once the lock is held.
        Builds never overlap (§14.5's serial-writer requirement): CacheConsumer calls this
        serially, and the lock covers the refresh endpoint, a second caller (§14.9). Raises
        on failure; the caller decides what that means (CacheConsumer: don't ACK the batch;
        the endpoint: 503)."""

    async def orphan_cleanup_loop(self) -> None:
        """Background cache cleanup (§14.6), launched by CacheConsumer.start() via
        run_supervised() (§14.2). Catches its own per-sweep exceptions."""
```

`CacheService` is itself a module-level singleton + getter (§5.1), same shape as `EntityAnalysisService` (§11.1):

```python
_cache_service: Optional[CacheService] = None

def get_cache_service() -> CacheService:
    if _cache_service is None:
        raise ServiceUnavailableError("cache service is not initialized")
    return _cache_service

def create_cache_service(config: CacheConfig) -> CacheService:
    global _cache_service
    _cache_service = CacheService(config)
    return _cache_service
```

`create_cache_service()` is called from `main.py` (§16) with `config.cache`.

### 14.2 `app/cache/cache_consumer.py` — `CacheConsumer`

Owns `stream:event_scored` consumption (batching loop with inline reclaim, dead-letter routing, batch ACK), calling `CacheService.build_new_version()` (§14.1). Split rationale: §14.1.

```python
class CacheConsumer:
    def __init__(self, config: CacheConfig):
        self._service = get_cache_service()                # §14.1
        self._redis_stream = get_redis_stream_client()    # §6.1 — consumes, reclaims, ACKs
        self._config = config

    async def start(self) -> None:
        """Entry point, called from main.py's lifespan (§16); rationale as
        CleanedNewsConsumer.start() (§11.2). run_supervised() (§6.1) keeps a restart from
        leaking a second _consumer_loop(), which would double every build — the build lock
        (§14.9) keeps them from overlapping, not from both running."""
        await self._redis_stream.create_group_if_not_exists(STREAM_EVENT_SCORED, CONSUMER_GROUP_CACHE)
        await run_supervised(self._consumer_loop(), self._service.orphan_cleanup_loop())

    async def _consumer_loop(self) -> None:
        """Reads batches from stream:event_scored, reclaims inline (no
        separate _reclaim_loop() — see below), and calls
        service.build_new_version() once per iteration — see Flowchart
        below."""
```

Processes messages **serially**, with no semaphore or worker pool (system-design.md §6.1): the versioned replacement (§14.5) assumes one writer at a time, which `CacheService`'s build lock (§14.9) also enforces against the daily refresh.

> **Reclaim runs inside `_consumer_loop()`, not as a second coroutine (§6.1's pattern does not apply as-is).** The other layers' `_reclaim_loop()` is safe concurrently because of their per-key in-memory guards. Cache needs no guard for dedup (`_incremental_merge()` is idempotent, §14.4). Builds are serialised by `CacheService`'s lock (§14.9) whoever starts them, but a separate reclaim coroutine would trigger builds of its own; keeping reclaim inline means one build per iteration covers both, with the coalescing (below) in one place. The single loop does both roles sequentially each iteration.

#### Flowchart (system-design.md §6.2, with method-level detail) — `CacheConsumer._consumer_loop()`

Node `BUILD` is the single call into `CacheService.build_new_version(event_ids)` (§14.1); its steps are §14.3–§14.5. `build_initial_version()` runs once at startup, never from this loop (§16).

```mermaid
flowchart TD
    A([_consumer_loop iteration]) --> RC{STREAM_CLAIM_TIMEOUT_MS\nelapsed since last reclaim check?}
    RC -->|Yes| AC[autoclaim consumer=config.consumer_name\nmin_idle_ms=STREAM_CLAIM_TIMEOUT_MS\ncount=_RECLAIM_COUNT]
    RC -->|No| RG
    AC --> DL{Any reclaimed message with\ndelivery_count >= CACHE_MAX_RETRY?}
    DL -->|Yes| DLW[publish to STREAM_CACHE_DEAD_LETTER\nACK those; drop from this batch]
    DL -->|No| RG[read_from COUNT=None - everything pending\nBLOCK=CACHE_BATCH_TIMEOUT_MS\nconsumer=config.consumer_name\nthen coalesce: keep reading until\nCACHE_BATCH_TIMEOUT_MS since the first message]
    DLW --> RG
    RG -->|Empty and no reclaimed messages| A
    RG -->|Messages: event_id list\ncombined with any still-valid reclaimed event_ids| BUILD[service.build_new_version event_ids\nCacheService — see above]
    BUILD -->|Returns| J[ACK all messages in batch\nboth read_from and reclaimed]
    BUILD -->|Raises| K[Log warning cache_build_failed\nDo not ACK - reclaimed and retried later]
    J --> A
    K --> A
```

Combining reclaimed ids into the same batch as the current `read_from()` gives at most one `build_new_version()`, and one version bump, per iteration.

> **The loop coalesces reads into one build.** `read_from()` returns as soon as any message is available (§6.1), so under a steady trickle of scores each read would carry one message — and each build re-serialises and rewrites the whole cache, a cost that grows with cache size (Q-17). After the first non-empty read, the loop keeps calling `read_from()` with `BLOCK` = the time left until `CACHE_BATCH_TIMEOUT_MS` has passed since the first message, then builds once and ACKs everything collected. There's no batch-size limit (`COUNT` is omitted): a build costs about the same whether it merges one score or hundreds, since its cost is rewriting the whole cache — the layer only cares *whether* new scores arrived, not how many. Under load that's at most one build per `CACHE_BATCH_TIMEOUT_MS`; an isolated message still waits at most that long. Inline reclaim uses a fixed page, `_RECLAIM_COUNT = 100` (Redis's own `XAUTOCLAIM` default); anything beyond it is reclaimed on the next check. The other layers don't need this — each of their messages is processed independently, so batch size only shapes dispatch.

> **A failed build fails the batch, not the layer.** `build_new_version()` is wrapped in `try`/`except Exception`: log warning `cache_build_failed` with `exc_info`, leave the whole batch unACKed, continue the loop. The inline reclaim retries it after `STREAM_CLAIM_TIMEOUT_MS`, and `CACHE_MAX_RETRY` dead-letters a batch's messages that keep failing. Letting the exception escape would crash the layer Task on every DB or Redis blip (cancelling `orphan_cleanup_loop()` with it) and restart it through Coroutine Monitoring (§16) — noisy CRITICAL logs and a flapping `layers.cache` for an ordinary transient error. `asyncio.CancelledError` is not caught. A failed build writes nothing: the version write is one `MULTI/EXEC` (§14.5).

### 14.3 Recency Computation (design rationale: system-design.md §6.3)

#### Module: `app/cache/recency.py` — `apply_recency()`, `ranking_key()`

The one place the recency formula and the ranking order live, used by the cache build (`_refresh_cached_records()`, `_build_candidate_records()`, §14.1/below, and the `sorted()` steps). Recency is applied only when a version is built — on new scores or the daily refresh (§14.9) — never per request (system-design.md §6.3).

#### Function: `apply_recency(aggregation_updated_at: datetime, stored_base_rule_score: float, direction: int, stock_impact_score: float, decay_lambda: float) -> tuple[float, float]`

Pure: returns `(final_base_rule_score, final_abs_final_score)` without mutating anything. It takes primitives because its call sites source the values from different typed objects:

```python
def apply_recency(aggregation_updated_at: datetime, stored_base_rule_score: float, direction: int, stock_impact_score: float, decay_lambda: float) -> tuple[float, float]:
    hours_elapsed = (datetime.now(timezone.utc) - aggregation_updated_at).total_seconds() / 3600
    recency_score = 10 * math.exp(-decay_lambda * hours_elapsed)
    final_base_rule_score = (stored_base_rule_score + recency_score * 0.20) * direction
    final_abs_final_score = abs(final_base_rule_score * 0.67 + stock_impact_score * 0.33 * 2)
    return final_base_rule_score, final_abs_final_score
```

> This runs for **every** Event in the cache on **every** build, since recency decays for all of them. The inputs must always be the fixed, never-recency-adjusted values (`_refresh_cached_records()` reads them from the three internal fields `_to_cache_record()` wrote, §14.5; `_build_candidate_records()` reads them off a fresh `EventScore`/`Event`). Feeding back a previous call's output would compound the decay.

#### Function: `ranking_key(record: CacheRecord) -> tuple`

The Morning Brief order (system-design.md §6.6): `abs_final_score`, then `|base_rule_score|`, then `last_seen_at`, then `first_seen_at` — all descending, used as `sorted(..., key=ranking_key, reverse=True)` by both the cache build and the route.

```python
def ranking_key(record: CacheRecord) -> tuple:
    return (record.abs_final_score, abs(record.base_rule_score), record.last_seen_at, record.first_seen_at)
```

#### Function: `_build_candidate_records(scores: list[EventScore], events: dict[UUID, Event], decay_lambda: float) -> dict[UUID, Candidate]`

Converts this cycle's scores (the full table from `build_initial_version()`, otherwise the batch) into `CacheRecord`s with recency applied, returning `{event_id: Candidate}`. `Candidate.stored_abs_final_score` is carried because `_incremental_merge()` (§14.4) must threshold on the stored value, not the recency-adjusted `Candidate.record.abs_final_score`:

```python
def _build_candidate_records(scores: list[EventScore], events: dict[UUID, Event], decay_lambda: float) -> dict[UUID, Candidate]:
    result = {}
    for score in scores:
        event = events[score.event_id]
        final_base, final_abs = apply_recency(event.aggregation_updated_at, score.base_rule_score, score.direction, score.stock_impact_score, decay_lambda)
        result[score.event_id] = Candidate(_to_cache_record(event, score, final_base, final_abs), score.abs_final_score)
    return result
```

### 14.4 Incremental Merge Rules (system-design.md §6.4)

Uses the **stored** `abs_final_score` from `event_scores` (pre-recency) for inclusion/exclusion decisions — never `final_abs_final_score`:

| Condition | Action |
|---|---|
| Updated Event, stored `abs_final_score >= NOISE_FILTER_THRESHOLD`, already in cache | Replace |
| Updated Event, stored `abs_final_score >= NOISE_FILTER_THRESHOLD`, not in cache | Insert |
| Updated Event, stored `abs_final_score < NOISE_FILTER_THRESHOLD`, already in cache | Remove |
| Updated Event, stored `abs_final_score < NOISE_FILTER_THRESHOLD`, not in cache | Ignore |

```python
def _incremental_merge(current: dict[UUID, CacheRecord], candidates: dict[UUID, Candidate], threshold: float) -> dict[UUID, CacheRecord]:
    merged = dict(current)
    for event_id, candidate in candidates.items():
        if candidate.stored_abs_final_score >= threshold:
            merged[event_id] = candidate.record   # replace or insert
        else:
            merged.pop(event_id, None)              # remove if present, else no-op
    return merged
```

`current` is untouched for every `event_id` not in `candidates`: an Event not updated this cycle keeps its inclusion status and only its display values are refreshed (§14.1), so decay alone can never remove an Event (system-design.md §6.4).

**The DB fetch (§14.1) only needs the batch.** The cache has no retention policy (Q-17), so "every id in `current`" grows without bound. `_to_cache_record()` (§14.5) stores the three inputs `apply_recency()` needs, so `_refresh_cached_records()` never touches the DB and the fetch is bounded by batch size.

### 14.5 Versioned Cache Replacement (system-design.md §6.5)

```python
async def _write_new_version(self, sorted_records: list[CacheRecord], old_version: Optional[int], started: float) -> int:
    new_version = await self._cache_store.incr_version_counter()
    await self._cache_store.write_cache_version(
        new_version, json.dumps([r.to_dict() for r in sorted_records], ensure_ascii=False),
        datetime.now(timezone.utc), old_version,
    )
    get_cache_reader().prime(new_version, sorted_records)   # §14.8 — the API serves these without parsing
    logger.info("cache_build_completed", cache_version=new_version, records=len(sorted_records),
                latency_ms=(time.perf_counter() - started) * 1000)   # build duration, for Cache tuning
    return new_version
    # write_cache_version (§10.4) performs SET v{new}, SET current_version, DEL v{old}, SET last_updated
    # as one atomic MULTI/EXEC pipeline — readers never observe a version between "new written" and
    # "pointer switched", and v{old} is never visible as deleted before the pointer has already moved off it.
```

`sorted_records` are typed `CacheRecord`s (§14.1); `.to_dict()` produces the JSON `write_cache_version()` stores. `ensure_ascii=False` stores Chinese text as UTF-8 (about 3 bytes a character) instead of `\uXXXX` escapes (6 bytes) — roughly 40% smaller, so faster to write, read and parse; the Redis client already handles UTF-8 strings. `prime()` runs only after the write succeeded, so the reader is never handed a version that isn't in Redis. `old_version` is the `current_version` already read by `build_new_version()`; it can't have changed, since the build lock (§14.9) rules out a concurrent writer.

#### Function: `_to_cache_record(event: Event, score: EventScore, final_base_rule_score: float, final_abs_final_score: float) -> CacheRecord`

Builds one `CacheRecord` (§14.1) from a fresh `Event`/`EventScore` pair. `abs_final_score`/`base_rule_score` take the `final_*` arguments; the stored pre-recency values go into `CacheScoreDetail.stored_base_rule_score`. `event.brief_summary` unwraps into `summary_short`/`summary_full`/`key_numbers` (`None`/empty if absent, §3.3):

```python
def _to_cache_record(event: Event, score: EventScore, final_base_rule_score: float, final_abs_final_score: float) -> CacheRecord:
    brief = event.brief_summary.output if event.brief_summary else None
    return CacheRecord(
        event_id=event.event_id, exchange=event.exchange, stock_code=event.stock_code,
        event_type_primary=event.event_type_primary,
        abs_final_score=final_abs_final_score, base_rule_score=final_base_rule_score,
        first_seen_at=event.first_seen_at, last_seen_at=event.last_seen_at,
        event=CacheEventDetail(
            event_type_secondary=event.event_type_secondary or [], source_count=event.source_count,
            source_list=event.source_list or [],
            summary_short=brief.summary_short if brief else None,
            summary_full=brief.summary_full if brief else None,
            key_numbers=brief.key_numbers if brief else [],
            aggregation_updated_at=event.aggregation_updated_at,
        ),
        score=CacheScoreDetail(
            stock_impact_score=score.stock_impact_score, llm_fallback=score.llm_fallback,
            rule_score_detail=score.rule_score_detail.output,
            llm_score_detail=CacheLLMScoreDetail(
                adjustment_reason=score.llm_score_detail.output.adjustment_reason,
                score_version=score.llm_score_detail.version, scored_at=score.scored_at,
            ),
            stored_base_rule_score=score.base_rule_score, direction=score.direction,
        ),
    )
```

> **The three internal-only fields need no explicit filtering.** `CacheEventDetail.aggregation_updated_at`/`CacheScoreDetail.stored_base_rule_score`/`CacheScoreDetail.direction` exist so `_refresh_cached_records()` (§14.1) can recompute recency without a DB read (§14.4). `ScoredEventRecord`/`EventDetail`/`ScoreDetail` (§3.5) don't declare them, and Pydantic's default `extra="ignore"` drops them when `get_morning_brief()` (§15.3) builds the response.

### 14.6 Orphaned Cache Cleanup (system-design.md §12.1)

#### Method: `CacheService.orphan_cleanup_loop() -> None`

```
Purpose : while True: await asyncio.sleep(CACHE_ORPHAN_CLEANUP_INTERVAL_S);
          try: current_version = cache_store.get_current_version() (skip the sweep if None);
          cache_store.scan_orphaned_versions(current_version) — only versions older than
          that, never the counter (§10.4) — and delete every key returned.
          Covers a crash before write_cache_version()'s atomic pipeline runs, or an old
          version left behind if an earlier delete failed under a partial Redis outage.
          except Exception: log warning orphan_cleanup_sweep_failed with exc_info, do not
          re-raise — retried next tick (best-effort; `except Exception` per §6.1). This loop
          must not crash CacheConsumer's Task: a restart that leaked a second
          _consumer_loop() would double every build (§14.2).
```

### 14.8 `app/cache/cache_reader.py` — `CacheReader`

The Morning Brief API's (§15.3) parsed view of the current cache version. Parsing a version — `json.loads` plus `CacheRecord.from_dict()` for every record — would be nearly the whole cost of a request (hundreds of milliseconds at a few thousand Events, CPU work on the event loop every layer shares, §16); the request itself only selects from the parsed list. A version never changes once written, so it's parsed at most once and reused until the pointer moves.

```python
class CacheReader:
    def __init__(self):
        self._cache_store = get_cache_store()   # §10.4
        self._version: Optional[int] = None
        self._records: list[CacheRecord] = []

    def prime(self, version: int, records: list[CacheRecord]) -> None:
        """Called by CacheService._write_new_version() (§14.5) right after a successful write:
        the build already holds the typed records, so in the normal case no request ever parses."""
        self._version, self._records = version, records

    async def get_records(self, version: int) -> Optional[list[CacheRecord]]:
        """The records of `version`: from memory if it's the one held, else read from Redis and
        parsed once — after a restart, or a build that failed between write and prime. None if
        the version no longer exists in Redis (a build replaced it; the caller re-reads the
        pointer, §15.3). Callers must not mutate the returned records — they're shared by every
        request until the next version."""
        if version == self._version:
            return self._records
        raw = await self._cache_store.read_cache_version(version)
        if raw is None:
            return None
        records = [CacheRecord.from_dict(item) for item in json.loads(raw)]
        if self._version is None or version > self._version:   # never replace a newer held version with an older one
            self._version, self._records = version, records
        return records
```

Module-level singleton + getter (§5.1): `create_cache_reader()` (after `create_cache_store()`, §16) and `get_cache_reader()`, used by `CacheService` and the route. No lock: two requests arriving together for a version not yet held may both parse it — rare (only after a restart), and harmless. In-process state, so each replica holds its own (system-design.md §13.2 — SAPI runs one).

### 14.9 Daily Refresh (system-design.md §6.9)

`POST /v1/admin/morning-brief-refresh` (§15.7) calls `CacheService.build_new_version([])` directly and returns the version it wrote. An empty batch reads nothing from the DB: it recomputes recency for every cached Event from its stored inputs, re-sorts, and writes a new version — the only way recency moves forward on a day with no new scores. Admin's scheduler decides when (`admin-tad.md` §3.6); SAPI has no schedule or setting of its own.

The endpoint is a second caller of `build_new_version()` alongside `CacheConsumer`'s loop (§14.2), so `CacheService._build_lock` (§14.1) — an `asyncio.Lock` around every build, including `build_initial_version()` — is what keeps builds from overlapping now; the loop's own serial processing no longer covers it alone. A refresh arriving during a score-triggered build waits for it. If no version exists yet, `build_new_version()`'s step 1 falls back to the full build, so the refresh also recovers a failed startup build. In-process only — SAPI runs as one replica (system-design.md §13.2).

### 14.7 Error Handling (system-design.md §6.8)

| Scenario | Implementation |
|---|---|
| DB read failure | `EventScoreRepository.fetch_scores()` (§10.3) or `EventRepository.fetch_events()` (§10.2) raises inside `CacheService.build_new_version()`; caught by `CacheConsumer._consumer_loop()` (§14.2): log warning `cache_build_failed`, do not ACK, continue |
| `RedisStateClient` unreachable | Any read or write inside `build_new_version()` raises; caught by `_consumer_loop()` (§14.2): log warning `cache_build_failed`, do not ACK, retried via the inline reclaim |
| Atomic pipeline failure | `write_cache_version()`'s `MULTI/EXEC` (§10.4) raises with nothing written — no in-place retry; caught the same way, batch unACKed and retried via the inline reclaim |
| Orphan cleanup failure | `CacheService.orphan_cleanup_loop()` (§14.6) logs warning; retry next interval — its own concern, unrelated to `build_new_version()`'s error handling above |
| Initial cache build failure (startup) | `CacheService.build_initial_version()` raises; caught in `main.py`'s lifespan (§16), not here — logs CRITICAL, startup continues, `current_version` stays unbuilt. The first batch's `build_new_version()` (§14.1 step 1) sees no version and does the full build instead, so the whole history is recovered once the DB is back; until then `GET /v1/morning-brief` returns `SAPI-5001` |
| `RedisStreamClient` unreachable | Per §6.1 |

---

## 15. API Layer — `app/api/`

### 15.1 `app/api/main.py` — FastAPI App Factory

```python
API_VERSION = "v1"

def create_app() -> FastAPI:
    app = FastAPI(title="SAPI", version="1.0.0")
    app.include_router(health_router, prefix=f"/{API_VERSION}")
    app.include_router(morning_brief_router, prefix=f"/{API_VERSION}")
    app.include_router(hk_stock_list_sync_router, prefix=f"/{API_VERSION}")
    app.include_router(morning_brief_refresh_router, prefix=f"/{API_VERSION}")   # route path /admin/morning-brief-refresh (§15.7)
    register_error_handlers(app)   # app/api/error_handlers.py, below
    return app
```

#### `app/common/error_codes.py` — Error Codes

Every error code SAPI returns, as one commented constant each, in `docs/api.md` §1.2's `{SERVICE_PREFIX}-{CODE}` format (registry: `docs/api.md` Appendix). Error classes and the exception handlers reference these, never literals. Each ticket adds the constants it first uses.

```python
INTERNAL_ERROR = "COMMON-5000"             # Unexpected failure inside SAPI
SERVICE_UNAVAILABLE = "COMMON-5001"        # SAPI-controlled tools or services unreachable, or not initialized yet
UPSTREAM_UNAVAILABLE = "COMMON-5002"       # Third-party tools or outside services unavailable
PARSE_ERROR = "COMMON-6000"                # Structured or unstructured content cannot be parsed
LLM_ERROR = "COMMON-7000"                  # LLM call failed
LLM_RATE_LIMITED = "COMMON-7001"           # LLM provider rate limit hit
LLM_PROVIDER_ERROR = "COMMON-7002"         # LLM provider error, network error or timeout
LLM_AUTHENTICATION_FAILED = "COMMON-7003"  # LLM provider rejected the credentials
LLM_SCHEMA_VIOLATION = "COMMON-7004"       # LLM response did not match the output schema
LLM_REQUEST_REJECTED = "COMMON-7005"       # LLM provider rejected the request itself
MALFORMED_REQUEST = "COMMON-4000"          # Request body is not valid JSON
VALIDATION_FAILED = "COMMON-4001"          # Request fields failed validation
NOT_FOUND = "COMMON-4004"                  # Requested resource does not exist
CACHE_UNAVAILABLE = "SAPI-5001"            # Morning Brief cache has no current version
```

#### `app/common/error_categories.py` — Error Categories

The part of SAPI an error was raised from. Every raise site sets one, by where the raising code lives: anything under `app/common/` uses `COMMON`, except the HK Stock List (§4.2), which has its own; other packages use their own member (`app/config.py` → `CONFIG`, `app/db/` → `DB`, `app/entity_analysis/` → `ENTITY_ANALYSIS`, `app/llm/` → `LLM`, `app/clients/news_client.py` → `NEWS_CLIENT`, `app/redis/` → `REDIS`). Each ticket adds the members it first uses. Raise-site samples elsewhere in this doc leave `category` out; every real raise site passes one.

```python
class ErrorCategory(StrEnum):
    COMMON = "COMMON"
    CONFIG = "CONFIG"
    DB = "DB"
    ENTITY_ANALYSIS = "ENTITY_ANALYSIS"
    LLM = "LLM"
    NEWS_CLIENT = "NEWS_CLIENT"
    REDIS = "REDIS"
    STOCK_LIST = "STOCK_LIST"
```

#### `app/common/errors.py` — Errors

Every error SAPI raises is a `SAPIError` with six fields. `error_type`, `error_code` (one of the constants above), `message` (short public summary) and `log_level` have per-class defaults; `dev_message` (technical description of this occurrence) and `category` (above) are given at every raise site, which may also override any default for that occurrence. `category` and `error_code` are added to the log record whenever a `SAPIError` is logged (§4.3).

```python
import logging
from enum import StrEnum

class ErrorType(StrEnum):
    """Failure category of an error."""
    REQUEST = "REQUEST"
    GENERAL = "GENERAL"
    STORAGE = "STORAGE"
    PARSE = "PARSE"
    LLM = "LLM"

class SAPIError(Exception):
    """Base for every error SAPI raises, so a catch-all `except Exception` (§11.2/§13.2, the
    general handler below) never mistakes one for an unanticipated bug."""
    error_type: ErrorType = ErrorType.GENERAL
    error_code: str = error_codes.INTERNAL_ERROR
    message: str = "Internal server error"
    log_level: int = logging.ERROR

    def __init__(self, dev_message: str, *, category: ErrorCategory,
                 error_type: ErrorType | None = None, error_code: str | None = None,
                 message: str | None = None, log_level: int | None = None):
        super().__init__(dev_message)
        self.dev_message = dev_message
        self.category = category
        # each override given replaces the class default for this occurrence only

class NotFoundError(SAPIError):
    error_type = ErrorType.REQUEST
    error_code = error_codes.NOT_FOUND
    message = "Resource not found"
    log_level = logging.WARNING

class ServiceUnavailableError(SAPIError):
    """Used by every get_X() accessor's "called before create_X()" case (§5.1) and an
    unreachable DB/Redis (§5.2). An unreachable upstream (SADI/Admin/HKEXnews, e.g. §4.2)
    is built with upstream_unavailable()."""
    error_code = error_codes.SERVICE_UNAVAILABLE
    message = "Service unavailable"

    @classmethod
    def upstream_unavailable(cls, dev_message: str, *, category: ErrorCategory) -> Self:
        return cls(dev_message, category=category, error_code=error_codes.UPSTREAM_UNAVAILABLE,
                   log_level=logging.WARNING)   # an outside outage needs no code/config change

class ParseError(SAPIError):
    """Structured or unstructured content couldn't be parsed: a code/schema issue, not an
    availability one."""
    error_type = ErrorType.PARSE
    error_code = error_codes.PARSE_ERROR
    message = "Content could not be parsed"
```

**Exception handlers** live in `app/api/error_handlers.py`: an `HTTP_STATUS` table (`SAPIError` class → HTTP status), `to_error_response(exc) -> ErrorResponse`, and `register_error_handlers(app)`, which `create_app()` calls. One `sapi_error_handler`, registered for the `SAPIError` base class, serves every `SAPIError`: its status is the table's entry for the error's class or nearest listed base class, else 500, and it reads `exc.error_code`/`.message`/`.dev_message`/`.log_level` off the exception. `unhandled_error_handler`, registered for `Exception`, catches anything else: it logs at `ERROR` and returns 500 `COMMON-5000`. `RequestValidationError` (third-party) gets its own handler.

| Exception | Response |
|---|---|
| `RequestValidationError` (Pydantic) | HTTP 400; field failure → `COMMON-4001` with `detail.errors`; malformed JSON (`type == "json_invalid"`) → `COMMON-4000` |
| `NotFoundError` | HTTP 404, `COMMON-4004` |
| `ServiceUnavailableError` | HTTP 503, `exc.error_code` — `COMMON-5001` by default, `COMMON-5002` from `upstream_unavailable()` |
| `MorningBriefCacheUnavailableError` | HTTP 503, `SAPI-5001` (§15.3 below) |
| `ParseError` | HTTP 500, `COMMON-6000` (e.g. §4.2's HK Stock List; a code/schema issue, not an availability one, so it gets its own code rather than folding into `COMMON-5000` below) |
| General `Exception` | HTTP 500, `COMMON-5000` |

A `SAPIError` response is `ErrorResponse(error_code=exc.error_code, message=exc.message)`, with `detail` left `{}`. `detail` is filled only by errors that must tell the caller more, such as `COMMON-4001`'s field list (api.md §1.4). `dev_message` carries the occurrence's technical details, including the underlying exception's (`str(exc)`, `{exc!r}`), and never leaves SAPI: each handler logs it exactly once, as the log entry's message, at `exc.log_level`, with `exc_info=exc`. `log_level` is `ERROR` for an error that needs a code or config change, `WARNING` for a transient one that needs none (`upstream_unavailable()`).

### 15.2 `app/api/routes/health.py` — `GET /v1/health`

#### Handler: `get_health(response: Response) -> HealthResponse`

`response` is FastAPI's injected `Response`, used only to set the 503 status code (§5.4).

```
Purpose : Check DB, both Redis connections, HK Stock List readiness, and
          all four layer Tasks' liveness. Returns overall status per system-design.md §7.1
          / api.md §3.2's rules.
Returns : HTTP 200 (healthy or degraded) or HTTP 503 (unhealthy)
```

```
database     = ok  if `SELECT 1` succeeds, else error
redis_stream = ok  if RedisStreamClient.ping() (§6.1) succeeds, else error
redis_state  = ok  if RedisStateClient.ping() (§6.2) succeeds, else error
    — the three run concurrently (asyncio.gather), each as its own Task bounded by
    asyncio.wait({task}, timeout=get_config().health.check_timeout_s) (§4.1). On
    timeout the Task is cancelled and abandoned, not awaited (system-design.md §7.1):
    a module-level set holds it until its cleanup finishes, and a done callback
    retrieves its result so asyncio never reports it as unhandled. A timeout or any
    exception → that field = error, logged at WARNING. Worst-case latency is one
    timeout, not three (system-design.md §7.1).
hk_stock_list = ok if len(get_hk_stock_list_cache()) > 0, else not_ready   # §4.2 — in-process,
    no Redis
layers.{entity_analysis,aggregation,scoring,cache} = ok if the corresponding asyncio.Task
    from get_layer_tasks() (§15.6) is still running (not done()), else error

status = unhealthy (HTTP 503) if database=error OR any redis=error OR hk_stock_list=not_ready
status = degraded  (HTTP 200) if any layers.*=error (database/Redis/HK-Stock-List are all fine,
                    but a layer Task has crashed and not yet been auto-restarted — system-design.md §12.1
                    "Coroutine Monitoring": a done_callback restarts it, so this is
                    self-healing and transient)
status = healthy   (HTTP 200) otherwise
```

### 15.3 `app/api/routes/morning_brief.py` — `GET /v1/morning-brief`

#### Handler: `get_morning_brief(stocks: Annotated[str, Query(min_length=1)], k: Annotated[int | None, Query(ge=1, le=50)] = None) -> MorningBriefResponse`

`stocks` is comma-separated, split by the handler; an omitted `k` means `CacheConfig.top_k_default` (TOP_K_DEFAULT). The handler reads infra through `get_cache_store()` (§10.4) and `get_cache_reader()` (§14.8), per §5.4.

```
Purpose : Serve the current Morning Brief cache, filtered/ranked per stocks
          and k (system-design.md §7.3, api.md §3.3).
Errors  : HTTP 400 COMMON-4001 on validation (stocks empty, k out of [1,50])
          HTTP 503 SAPI-5001 if no cache version has ever been built successfully
          HTTP 503 COMMON-5001 if Redis is unreachable
```

#### Server processing logic (design rationale: system-design.md §7.3)

```python
_MAX_POINTER_READS = 3   # a build replacing the version mid-read twice in a row is already unlikely

async def get_morning_brief(stocks: str, k: int | None = None) -> MorningBriefResponse:
    config = get_config().cache   # §4.1
    cache_store = get_cache_store()
    cache_reader = get_cache_reader()
    k = k if k is not None else config.top_k_default

    # The pointer and the version data are two reads; a build can commit between them,
    # deleting the version just read (system-design.md §6.5). A missing version can only mean
    # that, so re-read the pointer and try again rather than serving an empty brief.
    for _ in range(_MAX_POINTER_READS):
        pointer = await cache_store.get_current_pointer()   # §10.4 — version + last_updated, one MGET
        if pointer is None:
            # No cache version has ever been built (system-design.md §7.4, api.md §3.3) —
            # SAPI-5001, HTTP 503. Distinct from a built cache that just has no
            # events matching this query, which is a plain 200 with an empty
            # events array (see §15.4 note below).
            raise MorningBriefCacheUnavailableError()
        cached = await cache_reader.get_records(pointer.version)   # §14.8 — parsed at most once per version
        if cached is not None:
            break
    else:
        raise ServiceUnavailableError("cache version kept disappearing between reads")   # 503 COMMON-5001

    records = cached   # already ranked by the build (§14.3) — read-only, shared by every request (§14.8)

    stock_set = set(stocks.split(","))
    # Watchlist Events first, then the backfill — each group keeps the version's ranking_key() order;
    # the response keeps this order (api.md §3.3), it isn't re-sorted as one list. No re-scoring here:
    # recency is as of the version's build (system-design.md §6.3), refreshed daily (§14.9).
    selected = [r for r in records if r.stock_code in stock_set][:k]
    if len(selected) < k:
        chosen = {r.event_id for r in selected}
        selected += [r for r in records if r.event_id not in chosen][: k - len(selected)]

    return MorningBriefResponse(
        cache_version=pointer.version, last_updated=pointer.last_updated,
        events=[r.to_dict() for r in selected],   # extra="ignore" drops the three internal fields (§14.5)
    )
```

> **`SAPI-5001` vs. the empty-cache response.** `pointer is None` (no cache version has ever been built) is the sole `SAPI-5001` trigger; a built cache with zero matching Events after `NOISE_FILTER_THRESHOLD` filtering is a plain `200` with a non-null `cache_version`/`last_updated` and an empty `events` array — `cache_version`/`last_updated` are never null on a `200` response. system-design.md §7.4 and `docs/api.md` §3.3 match this.

### 15.4 `app/common/errors.py` addition

```python
class MorningBriefCacheUnavailableError(SAPIError):
    error_type = ErrorType.STORAGE
    error_code = error_codes.CACHE_UNAVAILABLE
    message = "Morning brief cache unavailable"
```

### 15.5 `app/api/routes/hk_stock_list_sync.py` — `POST /v1/admin/hk-stock-list-sync`

#### Handler: `post_hk_stock_list_sync() -> HKStockListSyncResponse`

```
Purpose : Synchronously call §4.2's refresh_hk_stock_list() and return entries_loaded
          (api.md §3.4). It takes no arguments and reaches its own http_client/cache via
          get_http_client()/get_hk_stock_list_cache(), so this handler takes no parameters. It
          is the same function app/main.py's startup calls (§16).
Returns : HTTP 200 HKStockListSyncResponse
Errors  : HTTP 503 COMMON-5002 (ServiceUnavailableError) if HKEXnews is unreachable;
          HTTP 500 COMMON-6000 (ParseError) if its response shape didn't match (§4.2,
          §15.1). The existing cache is untouched on either.
```

### 15.6 `app/layer_tasks.py`

The four layer Tasks follow the same singleton-plus-getter pattern as every infra resource (§5.1): `main.py`'s lifespan registers them (§16), and `GET /health` reads them (§15.2).

```python
_layer_tasks: Optional[dict[str, asyncio.Task]] = None

def register_layer_tasks(tasks: dict[str, asyncio.Task]) -> None:
    """Register {"entity_analysis": ..., "aggregation": ..., "scoring": ..., "cache": ...}. main.py
    passes the dict its monitor keeps updated, so an auto-restarted Task is visible here."""
    global _layer_tasks
    _layer_tasks = tasks

def get_layer_tasks() -> dict[str, asyncio.Task]:
    if _layer_tasks is None:
        raise ServiceUnavailableError("layer tasks are not registered")
    return _layer_tasks
```

---

### 15.7 `app/api/routes/morning_brief_refresh.py` — `POST /v1/admin/morning-brief-refresh`

#### Handler: `post_morning_brief_refresh() -> MorningBriefRefreshResponse`

```
Purpose : The daily refresh (§14.9, api.md §3.5): version = await
          get_cache_service().build_new_version([]) — waits its turn on the build lock, then
          writes a new version with recency as of now. Synchronous, so Admin's job sees
          the real outcome.
Returns : HTTP 200 MorningBriefRefreshResponse(status="success", cache_version=version)
Errors  : HTTP 503 COMMON-5001 (ServiceUnavailableError) if Redis or the DB is
          unreachable — nothing written, the previous version keeps serving. Redis/DB
          errors already arrive as ServiceUnavailableError (§5.2, §10.4); the
          existing handler (§15.1) maps them.
```

---

## 16. Service Entry Point — `app/main.py`

Mostly a sequence of `create_X()` registration calls (§5.1, §6.1, §6.2, §7.3, §9, §10, §12.5, §4.2, §11.1, §12.1, §13.1, §14.1). `main.py` decides construction order where data dependencies exist (e.g. `http_client` before `NewsClient`); each Service pulls its infra via `get_X()` and each `Consumer` pulls its Service the same way. `main.py` explicitly constructs only the `WorkerPool` instances (Entity Analysis and Scoring, §11.3), passed to their Consumers. Skills are never constructed here: each Service builds a fresh instance per call (§11.1, §13.1, §8.1).

```python
from contextlib import AsyncExitStack, asynccontextmanager
from fastapi import FastAPI
import asyncio

from app.config import load_config
from app.logger import configure_logging, get_logger
from app.db.connection import create_db_client
from app.redis.stream_client import create_stream_client
from app.redis.state_client import create_state_client
from app.llm.vertex_adapter import create_llm_adapter
from app.common.http_client import create_http_client
from app.clients.news_client import create_news_client
from app.clients.admin_client import create_admin_client
from app.common.hk_stock_list import refresh_hk_stock_list
from app.entity_analysis.worker_pool import WorkerPool
from app.persistent.entity_event_repository import create_entity_event_repository
from app.persistent.event_repository import create_event_repository
from app.entity_analysis.entity_analysis_service import create_entity_analysis_service
from app.entity_analysis.cleaned_news_consumer import CleanedNewsConsumer
from app.aggregation.event_aggregation_store import create_event_aggregation_store
from app.aggregation.aggregation_service import create_aggregation_service
from app.aggregation.aggregation_consumer import AggregationConsumer
from app.skills.brief_summary_skill import BriefSummarySkill
from app.skills.event_scoring_skill import EventLLMScoringSkill
from app.persistent.event_score_repository import create_event_score_repository
from app.scoring.scoring_service import create_scoring_service
from app.scoring.scoring_consumer import ScoringConsumer
from app.persistent.cache_store import create_cache_store
from app.cache.cache_service import create_cache_service
from app.cache.cache_consumer import CacheConsumer
from app.layer_tasks import register_layer_tasks
from app.api.main import create_app

configure_logging()   # §4.3
logger = get_logger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as resources:
        # ── Startup ─────────────────────────────────────────────────────────────
        # Every resource registers its own close on the exit stack as soon as it exists. Shutdown runs
        # them in reverse registration order, and a failed startup closes whatever was already opened.
        # Each create_X() constructs its resource and registers it as that module's singleton (§5.1).
        # Order matters only where a data dependency exists (e.g. http_client before create_news_client()).
        config = load_config()

        db = await create_db_client(config.db)
        resources.push_async_callback(db.close, config.db.close_timeout_s)
        redis_stream = await create_stream_client(config.redis.stream_url, config.redis.reconnect_interval_s)
        resources.push_async_callback(redis_stream.close)
        redis_state = await create_state_client(config.redis.state_url)
        resources.push_async_callback(redis_state.close)
        http_client = create_http_client(config.http.timeout_s)
        resources.push_async_callback(http_client.aclose)

        llm_adapter = create_llm_adapter(config.llm)
        resources.push_async_callback(llm_adapter.close)
        news_client = create_news_client(http_client, config.entity_analysis.sadi_api_url)
        admin_client = create_admin_client(http_client, config.scoring.admin_api_url)
        entity_event_repository = create_entity_event_repository()
        event_repository = create_event_repository()
        event_score_repository = create_event_score_repository()
        event_aggregation_store = create_event_aggregation_store()
        cache_store = create_cache_store()
        create_cache_reader()   # §14.8 — after cache_store, which it reads through

        # Eager, BLOCKING HK Stock List load before any layer Task starts (system-design.md §9.2.1).
        # If it raises, startup continues with the empty fallback cache refresh_hk_stock_list()
        # installs (§4.2); GET /health reports 503 until a later sync succeeds.
        try:
            await refresh_hk_stock_list()
        except Exception:
            logger.critical("hk_stock_list_startup_load_failed", exc_info=True)

        # WorkerPools are layer-owned collaborators, constructed here and passed to their Consumer.
        # Skills are never constructed here.

        entity_analysis_worker_pool = WorkerPool("entity_analysis", config.entity_analysis.max_concurrent, config.llm.rate_limit_backoff_s)
        scoring_worker_pool = WorkerPool("scoring", config.scoring.max_concurrent, config.llm.rate_limit_backoff_s)

        # Each Service takes only its config and pulls its infra via get_X(); each Consumer pulls its
        # Service via get_X_service() (§5.1).
        create_entity_analysis_service(config.entity_analysis)
        cleaned_news_consumer = CleanedNewsConsumer(entity_analysis_worker_pool, config.entity_analysis, config.redis)
        aggregation_service = create_aggregation_service(config.agg)
        resources.push_async_callback(aggregation_service.close)   # group Tasks outlive the layer Task (§12.1)
        agg_consumer = AggregationConsumer(config.agg)
        create_scoring_service(config.scoring)
        scoring_consumer = ScoringConsumer(scoring_worker_pool, config.scoring)
        cache_service = create_cache_service(config.cache)

        # Eager, BLOCKING initial cache build before CacheConsumer's Task exists (both write through the
        # serial-writer path, §14.5). This is its one catch site (§14.1): on failure, startup continues
        # with current_version unbuilt, and the first build_new_version() does the full build instead.
        try:
            await cache_service.build_initial_version()
        except Exception:
            logger.critical("cache_startup_build_failed", exc_info=True)

        cache_consumer = CacheConsumer(config.cache)

        layer_tasks: dict[str, asyncio.Task] = {}   # registered below for GET /health (§15.6)
        shutting_down = False

        # Launch each layer as a monitored Task (system-design.md §12.1 Coroutine Monitoring). start() uses
        # run_supervised() (§6.1), so when _on_done fires both of the layer's coroutines have stopped and
        # the restart can't race a live sibling.
        def _make_monitored(name: str, coro_factory):
            task = asyncio.create_task(coro_factory())
            def _on_done(t: asyncio.Task):
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is None:
                    return
                if shutting_down:   # no restart during shutdown (system-design.md §12.1)
                    logger.error("layer_task_failed_during_shutdown", layer=name, exc_info=exc)
                    return
                logger.critical("layer_task_crashed", layer=name, exc_info=exc)
                layer_tasks[name] = _make_monitored(name, coro_factory)  # auto-restart
            task.add_done_callback(_on_done)
            return task

        layer_tasks["entity_analysis"] = _make_monitored("entity_analysis", cleaned_news_consumer.start)
        layer_tasks["aggregation"] = _make_monitored("aggregation", agg_consumer.start)
        layer_tasks["scoring"] = _make_monitored("scoring", scoring_consumer.start)
        layer_tasks["cache"] = _make_monitored("cache", cache_consumer.start)
        register_layer_tasks(layer_tasks)

        async def _cancel_layer_tasks() -> None:
            nonlocal shutting_down
            shutting_down = True
            for task in layer_tasks.values():
                task.cancel()
            await asyncio.gather(*layer_tasks.values(), return_exceptions=True)

        resources.push_async_callback(_cancel_layer_tasks)   # registered last, so it runs first

        yield  # Service is running here; leaving the block runs every registered close

app = create_app()
app.router.lifespan_context = lifespan
```

> **Aggregation's final flush:** `AggregationConsumer.start()`'s `try/finally` (§12.2) flushes when the Task is cancelled; `_cancel_layer_tasks()` needs only `task.cancel()` + `gather()`.

> **Routes** reach infra by calling each owning module's `get_X()` in the handler body (§5.4).

---

## 17. Database Migrations — `alembic/`

### Setup

Same Alembic workflow as SADI:

```bash
alembic upgrade head
alembic revision --autogenerate -m "description"
alembic downgrade -1
```

One migration file per table group, added by whichever layer first writes to it — an applied migration is never edited afterwards, so a table is only created once something actually uses it. File order follows the foreign-key dependencies below (`event_entity_map` references `entity_events`; `event_scores` references `events`).

### `alembic/versions/001_create_entity_events.py`

**`entity_events`**
```sql
CREATE TABLE entity_events (
    entity_event_id     UUID PRIMARY KEY,
    exchange            VARCHAR(10) NOT NULL,
    source_url          TEXT NOT NULL,
    source_name         VARCHAR(50) NOT NULL,
    published_at        TIMESTAMPTZ,
    stock_code          VARCHAR(20) NOT NULL,
    event_type_primary  VARCHAR(50) NOT NULL,
    sentiment_label     VARCHAR(10) NOT NULL,
    sentiment_score     FLOAT NOT NULL,
    headline            TEXT NOT NULL,
    entity_summary      TEXT NOT NULL,
    additional_outcome  JSONB NOT NULL,
    metadata            JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_entity_events_source_url_stock_code UNIQUE (source_url, stock_code)
    -- Composite, not source_url alone — one article can produce multiple
    -- entity_events rows (one per verified entity, system-design.md §9.4's
    -- EntityAnalysisResult.entity_events), so a single-column unique index on
    -- source_url would silently drop every entity past the first for a
    -- multi-entity article (system-design.md §10.1).
);

CREATE INDEX idx_entity_events_exchange ON entity_events (exchange);
CREATE INDEX idx_entity_events_stock_event_type ON entity_events (stock_code, event_type_primary);
```

### `alembic/versions/002_create_events.py`

**`events`**
```sql
CREATE TABLE events (
    event_id                 UUID PRIMARY KEY,
    exchange                 VARCHAR(10) NOT NULL,
    stock_code                VARCHAR(20) NOT NULL,
    event_type_primary        VARCHAR(50) NOT NULL,
    event_type_secondary       JSONB,
    source_list                JSONB NOT NULL,   -- source_count intentionally not a column; derive via jsonb_array_length(source_list) — see §3.3
    first_seen_at               TIMESTAMPTZ NOT NULL,
    last_seen_at                 TIMESTAMPTZ NOT NULL,
    brief_summary                 JSONB,
    aggregation_updated_at        TIMESTAMPTZ NOT NULL,
    published_aggregation_updated_at TIMESTAMPTZ,   -- outbox marker: last aggregation_updated_at published to stream:event_aggregated (§10.2); NULL = never published
    updated_at                    TIMESTAMPTZ NOT NULL,
    created_at                    TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_events_exchange ON events (exchange);
CREATE INDEX idx_events_stock_event_type ON events (stock_code, event_type_primary);
CREATE INDEX idx_events_aggregation_updated_at ON events (aggregation_updated_at);
CREATE INDEX idx_events_stock_code ON events (stock_code);
```

**`event_entity_map`**
```sql
CREATE TABLE event_entity_map (
    event_id          UUID NOT NULL REFERENCES events(event_id),
    entity_event_id     UUID NOT NULL REFERENCES entity_events(entity_event_id),
    created_at            TIMESTAMPTZ NOT NULL,
    CONSTRAINT pk_event_entity_map PRIMARY KEY (event_id, entity_event_id),
    CONSTRAINT uq_event_entity_map_entity_event_id UNIQUE (entity_event_id)
    -- "one EntityEvent belongs to exactly one Event" (system-design.md §10.4)
);

CREATE INDEX idx_event_entity_map_event_id ON event_entity_map (event_id);
```

### `alembic/versions/003_create_event_scores.py`

**`event_scores`**
```sql
CREATE TABLE event_scores (
    score_id             UUID PRIMARY KEY,
    event_id              UUID NOT NULL REFERENCES events(event_id),
    abs_final_score        FLOAT NOT NULL,
    base_rule_score         FLOAT NOT NULL,
    stock_impact_score       FLOAT NOT NULL,
    direction                 SMALLINT NOT NULL,
    llm_fallback               BOOLEAN NOT NULL,
    rule_score_detail           JSONB NOT NULL,
    llm_score_detail             JSONB NOT NULL,
    scored_at                     TIMESTAMPTZ NOT NULL,
    scored_aggregation_updated_at TIMESTAMPTZ NOT NULL,   -- events.aggregation_updated_at this score was computed for (§13.1 step 1)
    updated_at                     TIMESTAMPTZ NOT NULL,
    created_at                      TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_event_scores_event_id UNIQUE (event_id)
);

CREATE INDEX idx_event_scores_abs_final_score ON event_scores (abs_final_score);
CREATE INDEX idx_event_scores_base_rule_score ON event_scores (base_rule_score);
CREATE INDEX idx_event_scores_llm_fallback ON event_scores (llm_fallback);
```


---

## 18. Open Questions

Open questions from system-design.md §14 carry forward below, plus implementation-level gaps it doesn't address. Per the top-level `CLAUDE.md`, flag these; don't implement around them.

### Carried forward from system-design.md §14 (unresolved there)

| # | Question | Impact | Target |
|---|---|---|---|
| Q-1 | Validate batch size and timeout defaults against real throughput | Latency and efficiency tuning | Week 1 |
| Q-2 | Validate `SCORING_MAX_CONCURRENT` against LLM API rate limits | Scoring Layer throughput | Week 1 |
| Q-3 | Post-MVP: Dead Letter Stream consumption and reprocessing pipeline | Service reliability | Post-MVP |
| Q-6 | Post-MVP: `entity_event_processing_status` table for state machine tracking and quality monitoring | Observability | Post-MVP |
| Q-8 | Validate BriefSummarySkill's 200-char input cap against real aggregated Events | BriefSummarySkill output quality | Week 2 |
| Q-9 | Validate `RATE_LIMIT_BACKOFF_S=60` against actual Vertex AI rate limit reset window | LLM throughput | Week 1 |
| Q-10 | Post-MVP: quality monitoring Service design (log ingestion, LLM-as-Judge, post-event price correlation) | LLM quality improvement | Post-MVP |
| Q-11 | Validate the common `HTTP_TIMEOUT_S=120` (§4.1 — covers SADI, Admin, and HKEXnews calls) against real latency measurements (`LLM_API_TIMEOUT_S` is measured, §4.1) | Latency tuning | Week 1 |
| Q-15 | Validate `source_heat_score`'s `source_count / TOTAL_ACTIVE_SOURCES` formula against real cross-source corroboration rates | `source_heat_score` calibration | Week 1-2 |
| Q-16 | Validate the name checks of entity verification (§4.2 `name_matches_stock`/`lookup_stock`) at real volume: how often `lookup_stock` decides an entity, and a hand-checked sample of its matches and of unverified entities | EntityAnalysisSkill accuracy | Week 2 |
| Q-17 | Morning Brief cache retention: Events never age out of the cache (§14.4), so it grows without bound and old high-impact Events keep surfacing in the backfill | Morning Brief content; build cost (§14.1, §14.9); Redis memory | Pre-go-live (product decision) |

(Q-4, Q-5, Q-7, Q-12, Q-13, Q-14 are marked Resolved in system-design.md §14 and are not repeated here.)

### New — implementation-doc-level gaps found while writing this document

| # | Gap | Where | Recommended resolution |
|---|---|---|---|
| I-1 | **`lookup_stock`'s (§4.2) real-traffic latency is unmeasured.** Its name search (`_search()`) runs synchronously on the shared event loop (§16). Before traffic it measured p50 0.18 ms, p99 5.0 ms, max 6.6 ms over 737 real name queries (Python 3.12, Apple M5 Pro), with the `StockNameIndex` prepared at refresh (16 ms to build), and it runs for about 1.5% of entities on real news. So `asyncio.to_thread()` is not needed. | §4.2 | After traffic: p50/p95/p99 from `lookup_stock_completed`. Revisit `asyncio.to_thread()` only if calls cross the 20 ms WARNING threshold. |
| I-3 | **BriefSummarySkill/EventLLMScoringSkill's model and `Max Tokens` (§8 table: `gemini-3.1-flash-lite`, 4096/2048) are unmeasured starting values;** EntityAnalysisSkill's row is measured (§8). | §8, system-design.md §9.1 | Measure on real aggregated Events before implementing either Skill: check for calls hitting the max (truncated JSON → `LLMSchemaViolationError`) and for persistent unused headroom at p95/p99; adjust per Skill. |
---

*— End of Document | SAPI Implementation Guide v0.1 —*
