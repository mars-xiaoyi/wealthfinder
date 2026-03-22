# HK Stock AI Research Assistant
## API Specification — v0.1

| Field | Detail |
|---|---|
| Document Version | v0.1 |
| Parent System | HK Stock AI Research Assistant |
| Document Status | DRAFT — Work in progress |
| API Versioning | All endpoints prefixed with `/v1` |
| Authentication | None (MVP single-user mode) |

---

## Table of Contents

1. [Error Response Standard](#1-error-response-standard)
2. [SADI — Stock Assistant Data Ingestion](#2-sadi--stock-assistant-data-ingestion)
3. [SAPI — Stock Assistant Pipeline Intelligence](#3-sapi--stock-assistant-pipeline-intelligence)
4. [Admin — MWP Admin Service](#4-admin--mwp-admin-service)

---

## 1. Error Response Standard

### 1.1 Error Response Structure

All services return a unified error response body on non-2xx responses:

```json
{
    "error_code": "SADI-4001",
    "message": "Human-readable description of the error",
    "detail": {}
}
```

| Field | Type | Description |
|---|---|---|
| `error_code` | String | Structured error code; format `{SERVICE}-{CODE}` |
| `message` | String | Human-readable error description |
| `detail` | Object | Optional additional context (e.g. field validation failures); empty object `{}` if not applicable |

### 1.2 Error Code Format

```
{SERVICE_PREFIX}-{CODE}

SERVICE_PREFIX:
    SADI    Stock Assistant Data Ingestion
    SAPI    Stock Assistant Pipeline Intelligence
    ADMIN   MWP Admin Service
    COMMON  Errors applicable across all services

CODE:
    4xxx    Client errors (bad request, not found, validation)
    5xxx    Server errors (internal, dependency unavailable)
```

### 1.3 Common Error Codes

Applicable across all services. Use `COMMON` prefix.

| Error Code | HTTP Status | Description |
|---|---|---|
| `COMMON-4000` | 400 | Malformed request — unparseable JSON or missing Content-Type |
| `COMMON-4001` | 400 | Request validation failed — one or more fields failed validation; see `detail` |
| `COMMON-4004` | 404 | Resource not found |
| `COMMON-4005` | 405 | Method not allowed |
| `COMMON-5000` | 500 | Internal server error — unexpected condition |
| `COMMON-5001` | 503 | Service unavailable — dependency unreachable (DB or Redis) |
| `COMMON-5002` | 503 | Service unavailable — upstream service unreachable |

### 1.4 Validation Error Detail

When `COMMON-4001` is returned, `detail` contains a list of field-level errors:

```json
{
    "error_code": "COMMON-4001",
    "message": "Request validation failed",
    "detail": {
        "errors": [
            {
                "field": "window_start",
                "issue": "must be a valid ISO 8601 datetime"
            },
            {
                "field": "k",
                "issue": "must be between 1 and 50"
            }
        ]
    }
}
```

---

## 2. SADI — Stock Assistant Data Ingestion

Base URL: `http://sadi/v1`

### 2.1 Endpoints Overview

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Service health check |
| `POST` | `/crawl` | Trigger a crawl execution |
| `GET` | `/cleaned_news/{cleaned_id}` | Fetch a single cleaned article by ID |
| `POST` | `/cleaned_news/batch` | Fetch multiple cleaned articles by ID list |

---

### 2.2 `GET /v1/health`

Returns service health status.

**Response `HTTP 200`:**

```json
{
    "status": "healthy",
    "database": "ok",
    "redis": "ok",
    "coroutines": {
        "clean_worker": "ok",
        "stream_handler": "ok"
    }
}
```

| Field | Values | Description |
|---|---|---|
| `status` | `healthy` / `degraded` / `unhealthy` | Overall service status |
| `database` | `ok` / `error` | PostgreSQL connectivity |
| `redis` | `ok` / `error` | Redis connectivity |
| `coroutines.clean_worker` | `ok` / `error` | `clean_worker` coroutine status |
| `coroutines.stream_handler` | `ok` / `error` | `stream_handler` coroutine status |

**HTTP status codes:**
- All components healthy → `200` with `status: healthy`
- Any component degraded → `200` with `status: degraded`
- Database or Redis unreachable → `503` with `status: unhealthy`

---

### 2.3 `POST /v1/crawl`

Triggers a crawl execution asynchronously. SADI begins crawling immediately and signals completion via `stream:crawl_completed` when done.

**Request body:**

```json
{
    "execution_id": "550e8400-e29b-41d4-a716-446655440000",
    "window_start": "2025-01-14T14:00:00Z",
    "window_stop": "2025-01-15T00:00:00Z"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `execution_id` | UUID | Yes | Correlation ID from Admin Scheduler; echoed back in `stream:crawl_completed` |
| `window_start` | ISO 8601 datetime (UTC) | Yes | Start of crawl time window; inclusive |
| `window_stop` | ISO 8601 datetime (UTC) | Yes | End of crawl time window; inclusive |

**Constraints:**
- `window_start` must be before `window_stop`
- `window_stop` must not be in the future

**Response `HTTP 202 Accepted`:**

```json
{
    "execution_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "accepted"
}
```

| Field | Type | Description |
|---|---|---|
| `execution_id` | UUID | Echoed from request; used to match `stream:crawl_completed` signal |
| `status` | String | Always `accepted` on HTTP 202 |

**Error codes:**

| Error Code | HTTP Status | Trigger |
|---|---|---|
| `COMMON-4000` | 400 | Malformed JSON body |
| `COMMON-4001` | 400 | Validation failed: missing fields, invalid datetime format, `window_start >= window_stop`, `window_stop` in future |
| `SADI-4001` | 409 | A crawl with this `execution_id` is already running |
| `COMMON-5001` | 503 | Database or Redis unavailable |

---

### 2.4 `GET /v1/cleaned_news/{cleaned_id}`

Fetches a single cleaned article record by `cleaned_id`. Called by SAPI NLP Layer when processing individual articles.

**Path parameter:**

| Parameter | Type | Description |
|---|---|---|
| `cleaned_id` | UUID | Primary key of the `cleaned_news` record |

**Response `HTTP 200`:**

```json
{
    "cleaned_id": "550e8400-e29b-41d4-a716-446655440000",
    "raw_id": "660e8400-e29b-41d4-a716-446655440001",
    "title_cleaned": "騰訊控股公佈第四季業績",
    "body_cleaned": "騰訊控股（00700.HK）第四季廣告收入錄得強勁增長...",
    "published_at": "2025-01-15T00:00:00Z",
    "source_name": "MINGPAO",
    "source_url": "https://finance.mingpao.com/...",
    "created_at": "2025-01-15T00:05:00Z"
}
```

| Field | Type | Description |
|---|---|---|
| `cleaned_id` | UUID | Primary key |
| `raw_id` | UUID | FK to `raw_news` record for traceability |
| `title_cleaned` | String | Normalised article title |
| `body_cleaned` | String | Normalised plain text body |
| `published_at` | ISO 8601 datetime (UTC) / null | Article publish time; null if unavailable in source |
| `source_name` | String | Source identifier, e.g. `MINGPAO` |
| `source_url` | String | Original article URL |
| `created_at` | ISO 8601 datetime (UTC) | Record creation time |

**Error codes:**

| Error Code | HTTP Status | Trigger |
|---|---|---|
| `COMMON-4004` | 404 | `cleaned_id` not found |
| `COMMON-5001` | 503 | Database unavailable |

---

### 2.5 `POST /v1/cleaned_news/batch`

Fetches multiple cleaned article records by ID list. Called by SAPI NLP Layer for batch article content retrieval.

**Request body:**

```json
{
    "cleaned_ids": [
        "550e8400-e29b-41d4-a716-446655440000",
        "660e8400-e29b-41d4-a716-446655440001"
    ]
}
```

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `cleaned_ids` | Array of UUID | Yes | Min 1, max 50 | List of `cleaned_news` primary keys |

**Response `HTTP 200`:**

```json
{
    "results": [
        {
            "cleaned_id": "550e8400-e29b-41d4-a716-446655440000",
            "raw_id": "660e8400-e29b-41d4-a716-446655440001",
            "title_cleaned": "騰訊控股公佈第四季業績",
            "body_cleaned": "騰訊控股（00700.HK）第四季廣告收入錄得強勁增長...",
            "published_at": "2025-01-15T00:00:00Z",
            "source_name": "MINGPAO",
            "source_url": "https://finance.mingpao.com/...",
            "created_at": "2025-01-15T00:05:00Z"
        }
    ],
    "not_found": [
        "770e8400-e29b-41d4-a716-446655440002"
    ]
}
```

| Field | Type | Description |
|---|---|---|
| `results` | Array | Successfully found records; same schema as `GET /cleaned_news/{cleaned_id}` response |
| `not_found` | Array of UUID | IDs from request that were not found; empty array if all found |

> Partial success is a valid `HTTP 200` response. Callers must check `not_found` to detect missing records.

**Error codes:**

| Error Code | HTTP Status | Trigger |
|---|---|---|
| `COMMON-4000` | 400 | Malformed JSON body |
| `COMMON-4001` | 400 | Validation failed: `cleaned_ids` empty or exceeds 50 items, invalid UUID format |
| `COMMON-5001` | 503 | Database unavailable |

---

## 3. SAPI — Stock Assistant Pipeline Intelligence

Base URL: `http://sapi/v1`

### 3.1 Endpoints Overview

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Service health check |
| `GET` | `/morning-brief` | Fetch scored Morning Brief events |

---

### 3.2 `GET /v1/health`

Returns service health status.

**Response `HTTP 200`:**

```json
{
    "status": "healthy",
    "database": "ok",
    "redis_stream": "ok",
    "redis_state": "ok",
    "layers": {
        "nlp": "ok",
        "aggregation": "ok",
        "scoring": "ok",
        "cache": "ok"
    }
}
```

| Field | Values | Description |
|---|---|---|
| `status` | `healthy` / `degraded` / `unhealthy` | Overall service status |
| `database` | `ok` / `error` | PostgreSQL connectivity |
| `redis_stream` | `ok` / `error` | `RedisStreamClient` connectivity |
| `redis_state` | `ok` / `error` | `RedisStateClient` connectivity |
| `layers.nlp` | `ok` / `error` | NLP Layer consumer coroutine status |
| `layers.aggregation` | `ok` / `error` | Aggregation Layer consumer coroutine status |
| `layers.scoring` | `ok` / `error` | Scoring Layer consumer coroutine status |
| `layers.cache` | `ok` / `error` | Cache Layer consumer coroutine status |

**HTTP status codes:**
- All components healthy → `200` with `status: healthy`
- Any layer degraded → `200` with `status: degraded`
- Database or Redis unreachable → `503` with `status: unhealthy`

---

### 3.3 `GET /v1/morning-brief`

Returns a ranked list of scored Entity-Event Pairs for the Morning Brief. The server returns the top `k` pairs prioritising watchlist-matching stocks; the client applies `POOL_MATCH_BOOST` and re-ranks locally.

**Query parameters:**

| Parameter | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `stocks` | String | Yes | Comma-separated HK stock codes; min 1 | Client watchlist stock codes, e.g. `00700,09988,03690` |
| `k` | Integer | No | 1–50; default 20 | Number of events to return |

**Response `HTTP 200` — normal:**

```json
{
    "cache_version": 43,
    "last_updated": "2025-01-15T00:28:00Z",
    "events": [
        {
            "event_id": "550e8400-e29b-41d4-a716-446655440000",
            "exchange": "HKEX",
            "stock_code": "00700.HK",
            "event_type_primary": "EARNINGS",
            "abs_final_score": 7.43,
            "base_rule_score": 8.95,
            "first_seen_at": "2025-01-15T00:00:00Z",
            "last_seen_at": "2025-01-15T06:00:00Z",
            "event": {
                "event_type_secondary": ["BUYBACK"],
                "source_count": 3,
                "source_list": [
                    {
                        "source_name": "MINGPAO",
                        "url": "https://finance.mingpao.com/...",
                        "published_at": "2025-01-15T00:00:00Z"
                    }
                ],
                "summary_short": "騰訊Q4廣告收入超預期",
                "summary_full": "騰訊第四季廣告收入按年增長32%，主要受惠於微信視頻號廣告業務強勁增長...",
                "key_numbers": [
                    "廣告收入+32% YoY",
                    "總收入HK$1,722億",
                    "回購計劃HK$1,000億"
                ]
            },
            "score": {
                "stock_impact_score": 3.8,
                "llm_fallback": false,
                "rule_score_detail": {
                    "event_type_score": 9.0,
                    "source_authority_score": 8.5,
                    "sentiment_strength_score": 8.5,
                    "source_heat_score": 7.0
                },
                "llm_score_detail": {
                    "adjustment_reason": "業績顯著超預期，回購規模反映管理層對前景高度信心，對股價具明確正面催化作用",
                    "score_version": "v1.0.0",
                    "scored_at": "2025-01-15T00:28:00Z"
                }
            }
        }
    ]
}
```

| Field | Type | Description |
|---|---|---|
| `cache_version` | Integer | Current cache version number |
| `last_updated` | ISO 8601 datetime (UTC) | Time of last successful cache build |
| `events` | Array | Ordered list of scored events; sorted by `abs_final_score` DESC server-side |
| `events[].event_id` | UUID | Primary key |
| `events[].exchange` | String | Exchange identifier, e.g. `HKEX` |
| `events[].stock_code` | String | HK stock code, e.g. `00700.HK` |
| `events[].event_type_primary` | String | Primary event classification |
| `events[].abs_final_score` | Float | Final score including real-time recency; primary sort key |
| `events[].base_rule_score` | Float | Rule score including real-time recency; secondary sort key |
| `events[].first_seen_at` | ISO 8601 datetime (UTC) | Earliest constituent article publish time |
| `events[].last_seen_at` | ISO 8601 datetime (UTC) | Most recent constituent article publish time |
| `events[].event.event_type_secondary` | Array of String | Up to 2 secondary event types; empty array if none |
| `events[].event.source_count` | Integer | Number of distinct sources reporting this event |
| `events[].event.source_list` | Array | Source attribution list |
| `events[].event.summary_short` | String | Short summary ≤30 Chinese characters |
| `events[].event.summary_full` | String | Full summary ≤150 Chinese characters |
| `events[].event.key_numbers` | Array of String | Up to 3 extracted numeric data points; empty array if none |
| `events[].score.stock_impact_score` | Float | EventScoringSkill output [-5, +5] |
| `events[].score.llm_fallback` | Boolean | True if EventScoringSkill failed and rule score only was used |
| `events[].score.rule_score_detail` | Object | Per-dimension rule score breakdown |
| `events[].score.llm_score_detail.adjustment_reason` | String | Plain-language AI explanation (Traditional Chinese) |
| `events[].score.llm_score_detail.score_version` | String | Skill version that produced this score |
| `events[].score.llm_score_detail.scored_at` | ISO 8601 datetime (UTC) | Score computation time |

**Response `HTTP 200` — empty cache (pipeline not yet run):**

```json
{
    "cache_version": null,
    "last_updated": null,
    "events": []
}
```

**Error codes:**

| Error Code | HTTP Status | Trigger |
|---|---|---|
| `COMMON-4001` | 400 | Validation failed: `stocks` missing or empty, `k` out of range |
| `SAPI-5001` | 503 | Morning Brief cache unavailable |
| `COMMON-5001` | 503 | Redis unavailable |

---

## 4. Admin — MWP Admin Service

Base URL: `http://admin/v1`

### 4.1 Endpoints Overview

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Service health check |
| `GET` | `/sources` | Fetch all active source configurations |

---

### 4.2 `GET /v1/health`

Returns service health status.

**Response `HTTP 200`:**

```json
{
    "status": "healthy",
    "database": "ok",
    "redis": "ok",
    "scheduler": "ok"
}
```

| Field | Values | Description |
|---|---|---|
| `status` | `healthy` / `degraded` / `unhealthy` | Overall service status |
| `database` | `ok` / `error` | PostgreSQL connectivity |
| `redis` | `ok` / `error` | Redis connectivity |
| `scheduler` | `ok` / `error` | Scheduler main loop thread status |

**HTTP status codes:**
- All components healthy → `200` with `status: healthy`
- Any component degraded → `200` with `status: degraded`
- Database or Redis unreachable → `503` with `status: unhealthy`

---

### 4.3 `GET /v1/sources`

Returns all currently active source configurations. Consumed by SADI and SAPI on startup and TTL-based cache refresh.

**Response `HTTP 200`:**

```json
{
    "sources": [
        {
            "source_id": "550e8400-e29b-41d4-a716-446655440000",
            "source_name": "HKEX",
            "source_type": "RSS_FULL",
            "priority": "P1",
            "authority_weight": 9.0,
            "rss_url": "https://www.hkexnews.hk/rss/listedco_rss.xhtml",
            "base_url": "https://www.hkexnews.hk",
            "max_concurrent": 3,
            "request_interval_min_ms": 500,
            "request_interval_max_ms": 1000,
            "crawl_config": {},
            "is_active": true,
            "updated_at": "2025-01-15T00:00:00Z"
        }
    ]
}
```

| Field | Type | Description |
|---|---|---|
| `source_id` | UUID | Primary key |
| `source_name` | String | Canonical source identifier, e.g. `HKEX`, `MINGPAO` |
| `source_type` | String | `RSS_FULL` / `RSS_CRAWLER` / `API` |
| `priority` | String | `P1` / `P2` / `P3` |
| `authority_weight` | Float | Source authority score for SAPI Rule Score |
| `rss_url` | String / null | RSS feed URL |
| `base_url` | String / null | Site root URL for resolving relative paths |
| `max_concurrent` | Integer | Max concurrent crawler coroutines |
| `request_interval_min_ms` | Integer | Min request interval (ms) |
| `request_interval_max_ms` | Integer | Max request interval (ms) |
| `crawl_config` | Object | Source-specific crawl config; always `{}` in MVP |
| `is_active` | Boolean | Always `true` — only active sources are returned |
| `updated_at` | ISO 8601 datetime (UTC) | Last configuration update time |

**Error codes:**

| Error Code | HTTP Status | Trigger |
|---|---|---|
| `COMMON-5001` | 503 | Database unavailable |

---

## Appendix: Error Code Registry

### SADI Error Codes

| Error Code | HTTP Status | Description |
|---|---|---|
| `SADI-4001` | 409 | Duplicate `execution_id` — a crawl with this ID is already running |

### SAPI Error Codes

| Error Code | HTTP Status | Description |
|---|---|---|
| `SAPI-5001` | 503 | Morning Brief cache unavailable — pipeline has not completed a successful run |

### ADMIN Error Codes

No service-specific error codes defined in MVP.

### COMMON Error Codes

| Error Code | HTTP Status | Description |
|---|---|---|
| `COMMON-4000` | 400 | Malformed request |
| `COMMON-4001` | 400 | Request validation failed |
| `COMMON-4004` | 404 | Resource not found |
| `COMMON-4005` | 405 | Method not allowed |
| `COMMON-5000` | 500 | Internal server error |
| `COMMON-5001` | 503 | Service unavailable — DB or Redis unreachable |
| `COMMON-5002` | 503 | Service unavailable — upstream service unreachable |

---

*— End of Document | API Specification v0.1 | Work in progress —*