# MWP Admin Service
## Technical Architecture Document — v0.2

| Field | Detail |
|---|---|
| Service Name | MWP Admin Service |
| Document Version | TAD v0.2 |
| Parent System | HK Stock AI Research Assistant |
| Service Responsibility | System configuration, source management, and job scheduling |
| Tech Stack | Java 21 + Spring Boot 3 |
| Dependencies | PostgreSQL 16, Redis, SADI (via API + Redis Streams) |
| Document Status | DRAFT — Work in progress |

---

## Table of Contents

1. [Scheduler Framework](#1-scheduler-framework)
2. [CrawlHandler](#2-crawlhandler)
3. [Source API](#3-source-api)
4. [Redis Stream Signal Specification](#4-redis-stream-signal-specification)
5. [API](#5-api)
6. [Deployment](#6-deployment)
7. [Open Questions](#7-open-questions)

---

## 1. Scheduler Framework

### 1.1 Overview

The Scheduler Framework is a module within MWP Admin Service responsible for time-based job triggering. It reads job configurations from the database, generates scheduled trigger records, and dispatches execution to dynamically loaded handler implementations.

> **Core Principle:** The framework is generic — it has no knowledge of business logic. All business behaviour is encapsulated in handler implementations that conform to the JobHandler contract.

### 1.2 Database Design

#### `scheduled_jobs`

Stores job configuration. One record per named job. Modified by operators only.

| Field | Type | Description |
|---|---|---|
| `job_id` | UUID | Primary key |
| `job_name` | VARCHAR | Human-readable name, e.g. `hkex-morning-crawl` |
| `job_type` | ENUM | `ONE_TIME` / `RECURRING` |
| `handler_key` | VARCHAR | Maps to handler class name in handlers package, e.g. `CRAWL` |
| `job_params` | JSONB | Per-job parameters passed to handler via `JobContext`, e.g. `{"source_name": "HKEX", "date_offset": 0}` |
| `start_time` | TIME (UTC) | Daily trigger time; for `RECURRING` defines first trigger of the day |
| `interval_value` | INTEGER | Null for `ONE_TIME` |
| `interval_unit` | ENUM | `MINUTE` / `HOUR` / `DAY`; Null for `ONE_TIME` |
| `execution_timeout_s` | INTEGER | Execution timeout passed to `Future.get(timeout)`; Null uses `DEFAULT_EXECUTION_TIMEOUT_S` |
| `is_active` | BOOLEAN | Whether this job is currently enabled |
| `created_at` | TIMESTAMPTZ | Record creation time UTC |
| `updated_at` | TIMESTAMPTZ | Last updated time UTC |

**MVP seed data:**

| job_name | job_type | handler_key | job_params | start_time | interval_value / unit |
|---|---|---|---|---|---|
| `hkex-evening-crawl` | `RECURRING` | `CRAWL` | `{"source_name": "HKEX"}` | 14:00 UTC | 1 / `DAY` |
| `hkex-morning-crawl` | `RECURRING` | `CRAWL` | `{"source_name": "HKEX"}` | 01:00 UTC | 1 / `DAY` |
| `mingpao-crawl` | `RECURRING` | `CRAWL` | `{"source_name": "MINGPAO"}` | 00:00 UTC | 1 / `HOUR` |
| `aastocks-crawl` | `RECURRING` | `CRAWL` | `{"source_name": "AASTOCKS"}` | 00:00 UTC | 1 / `HOUR` |
| `yahoo-hk-crawl` | `RECURRING` | `CRAWL` | `{"source_name": "YAHOO_HK"}` | 00:00 UTC | 1 / `HOUR` |

> **HKEX:** Two daily executions — evening (14:00 UTC = 22:00 HKT, post-market) and morning (01:00 UTC = 09:00 HKT, pre-open). Both use `interval = 1 DAY`, generating exactly one trigger per day each.
>
> **Other sources:** `interval = 1 HOUR` with no stop time — triggers generated for every hour of the day (24 triggers/day per source).

#### `scheduled_triggers`

Pre-generated trigger time points for all active jobs. Generated daily at `TRIGGER_GENERATION_TIME`. The main loop polls this table rather than computing trigger times inline.

| Field | Type | Description |
|---|---|---|
| `trigger_id` | UUID | Primary key |
| `job_id` | UUID | FK → `scheduled_jobs.job_id` |
| `scheduled_time` | TIMESTAMPTZ | Pre-computed trigger time UTC; indexed |
| `status` | ENUM | `PENDING` / `FIRED` / `SKIPPED` / `MISSED` |
| `execution_id` | UUID | FK → `job_executions.execution_id`; Null until fired |
| `created_at` | TIMESTAMPTZ | Record creation time UTC |
| `updated_at` | TIMESTAMPTZ | Last updated time UTC |

> **Trigger status flow:** `PENDING` → `FIRED` (normal) / `SKIPPED` (init returned false) / `MISSED` (stale on service restart)

> **Manual re-trigger:** Insert a new `PENDING` record with `scheduled_time = now()`. The main loop picks it up on the next cycle.

#### `job_executions`

One record per handler invocation. Written and updated exclusively by the framework.

| Field | Type | Description |
|---|---|---|
| `execution_id` | UUID | Primary key |
| `job_id` | UUID | FK → `scheduled_jobs.job_id` |
| `status` | ENUM | `RUNNING` / `SUCCESS` / `FAILED` / `INTERRUPTED` |
| `start_time` | TIMESTAMPTZ | Written by framework after `init()` returns True; equals `trigger.scheduled_time` |
| `completed_at` | TIMESTAMPTZ | Null until terminal state reached |
| `error_detail` | TEXT | Null unless `FAILED` |
| `created_at` | TIMESTAMPTZ | Record creation time UTC |
| `updated_at` | TIMESTAMPTZ | Last updated time UTC |

> **Execution status flow:** `RUNNING` → `SUCCESS` / `FAILED` (exception or timeout) / `INTERRUPTED` (stale on service restart)

### 1.3 Trigger Generation

**Daily generation:** At `TRIGGER_GENERATION_TIME` (00:00 UTC), the framework generates all trigger records for the current day for every `is_active = true` job.

**RECURRING trigger calculation:**

```mermaid
flowchart TD
    A([Generate triggers for RECURRING job]) --> B[t = start_time]
    B --> C{t <= 23:59?}
    C -->|Yes| D[Insert PENDING trigger at t]
    D --> E[t = t + interval]
    E --> C
    C -->|No| F([Done])
```

> `interval = 1 DAY` produces exactly one trigger per day at `start_time`. `interval = 1 HOUR` produces 24 triggers. The same formula handles both cases uniformly — no special branching required.

> **`stop_time` removed:** All `RECURRING` jobs run for the full day. Scheduling constraints (e.g. crawl only during trading hours) are expressed by adjusting `start_time` and `interval`, or by adding a second job record.

### 1.4 Scheduler Main Loop

**Startup sequence:**

```mermaid
flowchart TD
    A([Admin Service starts]) --> B[Mark stale RUNNING executions\nstatus = INTERRUPTED]
    B --> C[Mark stale PENDING triggers\nwhere scheduled_time <= now\nstatus = MISSED]
    C --> D{Today's triggers\nalready generated?}
    D -->|No| E[Generate today's triggers]
    D -->|Yes| F
    E --> F([Enter main loop])
```

**Main loop:**

```mermaid
flowchart TD
    A([Loop start]) --> B[Sleep SCHEDULER_LOOP_INTERVAL_S]
    B --> C[Query scheduled_triggers\nWHERE status = PENDING\nAND scheduled_time <= now\nORDER BY scheduled_time ASC]
    C --> D{Triggers found?}
    D -->|No| A
    D -->|Yes| E{Concurrent dispatches\n< MAX_CONCURRENT_DISPATCHES?}
    E -->|No — at capacity| F[Wait and retry]
    F --> E
    E -->|Yes| G[Submit dispatch trigger
to ThreadPoolTaskExecutor]
    G --> D
```

> Each trigger is dispatched as an independent `Future` submitted to `ThreadPoolTaskExecutor`. The main loop does not await individual dispatches. `MAX_CONCURRENT_DISPATCHES` maps directly to the executor pool size, preventing unbounded concurrency on service restart when many stale `PENDING` triggers may be present.

### 1.5 Handler Contract

#### JobHandler Interface

| Method | Async | Description |
|---|---|---|
| `init(job_id, context)` | Yes | Decides whether execution should proceed. Returns `True` to proceed, `False` to skip. Raises on unrecoverable error. If retry is needed, handler inserts a new `PENDING` trigger before returning `False`. |
| `execute(job_id, context)` | Yes | Core business logic. Subject to `Future.get(execution_timeout_s)` timeout enforced by `JobExecutor`. |
| `complete_with_success(job_id, context)` | No | Called after `SUCCESS` committed. For post-execution business logic. |
| `complete_with_error(job_id, context, error)` | No | Called after `FAILED` committed. For alerting and cleanup. |

#### JobContext

`JobContext` carries information the framework produces or can derive at dispatch time. Handler-specific configuration and query results are the handler's own responsibility.

| Field | Type | Populated by | Description |
|---|---|---|---|
| `job_id` | UUID | Framework at dispatch | From `scheduled_triggers.job_id` |
| `trigger_id` | UUID | Framework at dispatch | From `scheduled_triggers.trigger_id` |
| `job_params` | Map | Framework at dispatch | Parsed from `scheduled_jobs.job_params` JSONB; e.g. `{"source_name": "HKEX"}` |
| `execution_id` | UUID | Framework after `init()` returns True | Set after `RUNNING` record written; available to `execute()` and `complete_*()` |
| `start_time` | datetime | Framework after `init()` returns True | Set after `RUNNING` record written; equals `trigger.scheduled_time` |

> `JobContext` is a placeholder in MVP. Per-job metadata fields may be added post-MVP as needed.

> `previous_start_time` is not in context. Handlers that need the previous execution's `start_time` query `job_executions` directly within `execute()`.

#### Handler Loading

Handlers are loaded dynamically at dispatch time via Java reflection. All handler classes live under `HANDLERS_PACKAGE`. The class name must match `handler_key` exactly.

```
com.mwp.admin.handlers/
    CRAWL.java     # class CRAWL implements JobHandler
```

`JobExecutor` resolves and instantiates the handler at dispatch time:

```
Class<?> clazz = Class.forName(HANDLERS_PACKAGE + "." + job.getHandlerKey())
JobHandler handler = (JobHandler) clazz.getDeclaredConstructor().newInstance()
```

### 1.6 Framework Execution Flow

```mermaid
flowchart TD
    A([dispatch trigger]) --> B[Load handler via handler_key\nfrom HANDLERS_PACKAGE]
    B --> C[Build JobContext\njob_id + trigger_id + job_params]
    C --> D[await handler.init]
    D --> E{Result?}
    E -->|Raises| F[log error\nno execution record written]
    E -->|False| G[update trigger\nstatus = SKIPPED]
    E -->|True| H[Write execution\nstatus = RUNNING\nstart_time = trigger.scheduled_time\nUpdate trigger status = FIRED\nSet context.execution_id\nSet context.start_time]
    H --> I[Submit handler.execute to executor\nFuture.get with execution_timeout_s]
    I --> J{Result?}
    J -->|Success| K[update execution\nstatus = SUCCESS\ncompleted_at = now\ncommit]
    J -->|TimeoutError| L[update execution\nstatus = FAILED\nerror_detail = execution timeout\ncompleted_at = now\ncommit]
    J -->|Exception| M[update execution\nstatus = FAILED\nerror_detail = str e\ncompleted_at = now\ncommit]
    K --> N[handler.complete_with_success]
    L --> O[handler.complete_with_error]
    M --> O
```

> `commit()` is called before `complete_with_success` / `complete_with_error`. Terminal state is persisted even if the complete methods raise.

### 1.7 Responsibility Boundary

| Responsibility | Framework | Handler |
|---|---|---|
| Generate daily trigger records | yes | |
| Poll `scheduled_triggers` and dispatch | yes | |
| Limit concurrent dispatches via Semaphore | yes | |
| Dynamically load handler by `handler_key` | yes | |
| Parse `job_params` from `scheduled_jobs` and populate `JobContext` | yes | |
| Decide whether to execute (conflict detection) | | `init()` |
| Insert retry trigger on conflict | | `init()` |
| Write and update `job_executions` records | yes | |
| Commit execution status to terminal state | yes | |
| Apply `Future.get` execution timeout | yes | |
| Core business execution logic | | `execute()` |
| Query previous execution data | | `execute()` |
| Post-success business logic | | `complete_with_success()` |
| Post-failure business logic | | `complete_with_error()` |
| Recover `RUNNING` → `INTERRUPTED` on startup | yes | |
| Recover `PENDING` trigger → `MISSED` on startup | yes | |
| Generate missed triggers on startup | yes | |

### 1.8 Configuration Parameters

| Parameter | Default | Description |
|---|---|---|
| `DEFAULT_EXECUTION_TIMEOUT_S` | 1800 | Global execution timeout (30 min); overridden per job via `scheduled_jobs.execution_timeout_s` |
| `SCHEDULER_LOOP_INTERVAL_S` | 60 | Main loop wake interval (seconds) |
| `TRIGGER_GENERATION_TIME` | 00:00 UTC | Daily time at which trigger records are generated |
| `MAX_CONCURRENT_DISPATCHES` | 10 | `ThreadPoolTaskExecutor` max pool size; limits concurrent dispatch tasks |
| `HANDLERS_PACKAGE` | `com.mwp.admin.handlers` | Package containing handler implementations; used for dynamic class loading via reflection |

---

## 2. CrawlHandler

### 2.1 Overview

`CRAWL` is the single handler implementation for all source crawl jobs. It reads `source_name` from `context.job_params`, triggers SADI's crawl pipeline via `POST /crawl`, and awaits completion via `stream:crawl_completed`.

| handler_key | Handles |
|---|---|
| `CRAWL` | All source crawl jobs — `source_name` resolved from `context.job_params` |

### 2.2 `init()`

```mermaid
flowchart TD
    A([init called]) --> B[Query job_executions\nWHERE job_id = job_id\nAND status = RUNNING]
    B --> C{RUNNING found?}
    C -->|No| D([return True])
    C -->|Yes| E[Insert scheduled_triggers\nscheduled_time = now + retry_delay\nstatus = PENDING]
    E --> F([return False])
```

> `retry_delay` is a fixed constant within the handler (default: 5 minutes). If per-job tuning is needed post-MVP, it can be added to `job_params`.

### 2.3 `execute()`

```mermaid
flowchart TD
    A([execute called]) --> B[Read source_name from context.job_params]
    B --> C[POST /crawl\nbody: execution_id source_name date]
    C --> D[Subscribe stream:crawl_completed\nAwait message where\nmessage.execution_id = context.execution_id]
    D --> E{message.status?}
    E -->|SUCCESS| F([return normally])
    E -->|FAILED| G([raise CrawlFailedError\nmessage.error_detail])
```

> `date` in the request body is derived from `context.start_time` (formatted as `YYYYMMDD`). SADI uses it only for HKEX batch pull; other sources ignore it.

> `execute()` is subject to `Future.get(execution_timeout_s)` timeout enforced by `JobExecutor`. On timeout, the framework marks the execution `FAILED` and calls `complete_with_error()`.

### 2.4 `complete_with_success()`

No-op in MVP.

### 2.5 `complete_with_error()`

Logs a CRITICAL alert with fields: `job_id`, `execution_id`, `source_name`, `error_detail`.

---

## 3. Source API

### 3.1 `data_sources` Table

Stores configuration for all news sources. Owned and managed exclusively by Admin Service. SAPI is the sole read-only consumer via `GET /sources`.

| Field | Type | Description |
|---|---|---|
| `source_id` | UUID | Primary key |
| `source_name` | VARCHAR | Canonical source identifier, e.g. `HKEX`, `MINGPAO`; unique |
| `authority_weight` | FLOAT | Source authority score used by SAPI Rule Score; per-source value |
| `is_active` | BOOLEAN | Whether this source is currently enabled |
| `created_at` | TIMESTAMPTZ | Record creation time UTC |
| `updated_at` | TIMESTAMPTZ | Last updated time UTC |

> Crawl logic (URL patterns, selectors, pagination) and crawl behaviour parameters (concurrency, request interval) are owned exclusively by SADI. They are not stored in `data_sources`.

**MVP seed data:**

| source_name | authority_weight | is_active |
|---|---|---|
| `HKEX` | 9.0 | true |
| `MINGPAO` | 9.0 | true |
| `AASTOCKS` | 6.0 | true |
| `YAHOO_HK` | 6.0 | true |

> Initial `authority_weight` values follow P1=9.0, P2=6.0 groupings. To be validated against real data in Week 3-4 (PRD OQ-2).

### 3.2 `GET /sources`

Returns all `is_active = true` source records. Consumed by SAPI for `authority_weight` lookup. Full API contract defined in API specification document.

### 3.3 Consumer Caching

| Consumer | Fields used | Redis key | Fallback |
|---|---|---|---|
| SAPI | `source_name`, `authority_weight` | `source:config` (Hash keyed by `source_name`) | Use default weights P1=9, P2=6; log warning |

---

## 4. Redis Stream Signal Specification

Admin Service consumes one Redis Stream produced by SADI, used by CrawlHandler `execute()` to detect crawl completion.

| Stream | Producer | Consumer Group | Message Fields | Role |
|---|---|---|---|---|
| `stream:crawl_completed` | SADI | `admin-scheduler` | `execution_id`, `status` (SUCCESS / FAILED), `error_detail` | CrawlHandler awaits message matching `context.execution_id` to resolve `execute()` |

**Consumption mechanism:** `XREADGROUP COUNT+BLOCK` on `stream:crawl_completed`. Messages remain unACKed until CrawlHandler matches `execution_id` and resolves the result. Unacknowledged messages are reclaimed via `XAUTOCLAIM` after `STREAM_CLAIM_TIMEOUT_MS` and redelivered to the consumer group.

**Retry and Dead Letter:**

| Condition | Action |
|---|---|
| `delivery_count < CRAWL_STREAM_MAX_RETRY` | Redeliver via `XAUTOCLAIM`; CrawlHandler retries matching |
| `delivery_count >= CRAWL_STREAM_MAX_RETRY` | Write to `stream:crawl_completed_dead_letter`; ACK original message; log CRITICAL |

Dead Letter message fields: `execution_id`, `error_detail`, `delivery_count`, `failed_at`.

> In MVP, Dead Letter Stream serves as a monitoring indicator only — no automated reprocessing. Manual intervention required.

---

## 5. API

### 5.1 Endpoints (MVP)

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Service health check |
| GET | `/sources` | Return all active source records; full API contract in API specification document |

### 5.2 `GET /health` Response

| Field | Values | Description |
|---|---|---|
| `status` | healthy / degraded / unhealthy | Overall service status |
| `database` | ok / error | PostgreSQL connectivity |
| `redis` | ok / error | Redis connectivity |
| `scheduler` | ok / error | Scheduler main loop Task status |

HTTP response codes:
- All healthy → `200 healthy`
- Any component degraded → `200 degraded`
- Database or Redis unreachable → `503 unhealthy`

---

## 6. Deployment

### 6.1 Tech Stack

| Component | Choice | Rationale |
|---|---|---|
| Runtime | Java 21 | LTS release; virtual threads available if needed post-MVP |
| Framework | Spring Boot 3 | Native async support via `@Async`; integrates with JPA and Redis |
| Async execution | `@Async` + `ThreadPoolTaskExecutor` | Sufficient for low-concurrency scheduler dispatch; compatible with Spring Data JPA |
| DB client | Spring Data JPA | Standard ORM for Spring Boot; straightforward entity mapping |
| Redis client | Lettuce (via Spring Data Redis) | Default Spring Data Redis client; `RedisTemplate` for stream operations |
| DB migration | Flyway | Standard migration tool for Spring Boot |
| Containerisation | Docker | Independent deployment; docker-compose for local development |

### 6.2 Container Configuration

| Service | Image | Notes |
|---|---|---|
| admin | eclipse-temurin:21-jre-alpine (custom build) | Main Admin Service; includes Scheduler Framework, CrawlHandler, Source API |
| postgres | postgres:16-alpine | Shared with SADI and SAPI; persistent volume mounted |
| redis | redis:7-alpine | Shared with SADI and SAPI |

### 6.3 Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SPRING_DATASOURCE_URL` | — | PostgreSQL JDBC connection string; required |
| `SPRING_DATASOURCE_USERNAME` | — | PostgreSQL username; required |
| `SPRING_DATASOURCE_PASSWORD` | — | PostgreSQL password; required |
| `SPRING_REDIS_URL` | — | Redis connection string; required |
| `SADI_API_URL` | — | SADI service API endpoint; required |
| `DEFAULT_EXECUTION_TIMEOUT_S` | 1800 | Global job execution timeout (seconds) |
| `SCHEDULER_LOOP_INTERVAL_S` | 60 | Scheduler main loop wake interval (seconds) |
| `TRIGGER_GENERATION_TIME` | 00:00 UTC | Daily trigger generation time |
| `MAX_CONCURRENT_DISPATCHES` | 10 | `ThreadPoolTaskExecutor` max pool size for dispatch tasks |
| `HANDLERS_PACKAGE` | `com.mwp.admin.handlers` | Package containing handler implementations |
| `STREAM_CLAIM_TIMEOUT_MS` | 30000 | Pending message idle time before XAUTOCLAIM redelivery (ms) |
| `CRAWL_STREAM_MAX_RETRY` | 3 | Max delivery attempts for `stream:crawl_completed` before Dead Letter |

### 6.4 Project Structure

```
admin/
├── src/main/java/com/mwp/admin/
│   ├── scheduler/
│   │   ├── SchedulerLoop.java           # Main loop; startup recovery; trigger dispatch
│   │   ├── JobExecutor.java             # dispatch(); dynamic handler loading; framework execution flow
│   │   ├── TriggerGenerator.java        # RECURRING calculation; daily generation
│   │   └── JobHandler.java              # JobHandler interface; JobContext class
│   ├── handlers/
│   │   └── CRAWL.java                   # class CRAWL implements JobHandler; reads source_name from job_params
│   ├── stream/
│   │   └── CrawlCompletedConsumer.java  # XREADGROUP consumer for stream:crawl_completed; XAUTOCLAIM; Dead Letter
│   ├── api/
│   │   ├── HealthController.java        # GET /health
│   │   └── SourcesController.java       # GET /sources
│   ├── entity/                          # JPA entities: ScheduledJob, ScheduledTrigger, JobExecution, DataSource
│   ├── repository/                      # Spring Data JPA repositories
│   ├── config/
│   │   ├── AppConfig.java               # ThreadPoolTaskExecutor; environment variable binding
│   │   └── RedisConfig.java             # RedisTemplate configuration
│   └── AdminApplication.java            # Spring Boot entry point
├── src/main/resources/
│   ├── application.yml                  # Spring Boot configuration
│   └── db/migration/
│       └── V1__create_tables.sql        # Flyway: scheduled_jobs + scheduled_triggers + job_executions + data_sources schema + seed data
├── Dockerfile
├── pom.xml
└── docker-compose.yml
```

---

## 7. Open Questions

| # | Question | Impact | Target |
|---|---|---|---|
| Q-1 | Validate `DEFAULT_EXECUTION_TIMEOUT_S = 1800s` against real crawl + pipeline completion time | Timeout calibration | Week 1 |
| Q-2 | ~~`POST /crawl` and `GET /sources` full API contracts defined in API specification document~~ | ~~CrawlHandler implementation~~ | ✅ **Resolved** — see API Specification v0.2 |
| Q-3 | ~~`crawl_config` JSONB schema: define structure for CSS selectors, custom headers, and other source-specific crawl parameters~~ | ~~SADI crawl accuracy~~ | ✅ **Resolved** — `crawl_config` abolished; crawl logic owned exclusively by SADI codebase |
| Q-4 | Validate `authority_weight` initial values (P1=9.0, P2=6.0) against real data; per-source fine-tuning post-validation | SAPI Rule Score calibration | Week 3-4 |

---

*— End of Document | Admin TAD v0.2 | Work in progress —*