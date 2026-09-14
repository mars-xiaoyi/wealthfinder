# Mars Wealthfinder — Claude Code Guide

## Project Overview

**Mars Wealthfinder (MWP)** is a Hong Kong equity news intelligence system. It ingests news from multiple sources, enriches it through an NLP and scoring pipeline, and produces a ranked "Morning Brief" of stock-relevant events for investment research.

All three services are fully documented. **SADI implementation is substantially complete** (all 10 planned phases done — crawlers, cleaning pipeline, API, DB migrations, Docker packaging — see [stock-assistant-data-ingestion/progress.md](stock-assistant-data-ingestion/progress.md) for status and open questions). **SAPI and Admin are still design-only**, with no implementation code yet.

---

## Working Rules

Before implementing any feature:
1. **Read the relevant TAD and implementation doc first.** See [Key Documentation](#key-documentation) for file paths.
2. **Check Open Questions in the TAD.** Some sections have unresolved design decisions that block implementation. Do not implement around them — flag them instead.
3. **Do not invent structure.** If a directory or file does not exist, confirm the target path before creating it.

---

## Language Conventions

| Context | Language |
|---------|----------|
| All code, variable names, function names, class names | English |
| Log messages, error messages, exception text | English |
| API field names, DB column names, Redis keys | English |
| Comments and docstrings | English |
| User-facing output (Morning Brief summaries, event descriptions, news content) | Traditional Chinese |

---

## Architecture

Three microservices communicate via REST APIs and Redis Streams:

```
Admin (Java/Spring Boot) — scheduler & job orchestration
  ↓ POST /v1/crawl
SADI (Python/FastAPI)    — news ingestion & cleaning
  ↓ stream:raw_news_cleaned
SAPI (Python/FastAPI)    — NLP enrichment, event scoring & caching
  ↓ GET /v1/morning-brief (served to end users)
  ↑ stream:crawl_completed (SADI → Admin, crawl completion signal)
```

Shared infrastructure: **PostgreSQL 16**, **Redis 7**

---

## Services

| Service | Stack |
|---------|-------|
| SADI | Python 3.12, FastAPI, asyncio, httpx, trafilatura, Playwright |
| SAPI | Python 3.12, FastAPI, asyncio, Celery, Vertex AI (Gemini) |
| Admin | Java 21, Spring Boot 3, Spring Data JPA, Flyway |

---

## Repository Structure

```
mars-wealthfinder/
├── stock-assistant-data-ingestion/
├── stock-assistant-pipeline-intelligence/
├── admin/
├── poc/
└── docs/
```

---

## Key Documentation

Always read the relevant documents before implementing any feature. All design decisions are defined in the TADs and implementation docs — use the latest version.

| File | Description |
|------|-------------|
| `docs/api.md` | API Spec — all endpoints, schemas, error codes |
| `docs/admin-tad.md` | Admin TAD (no implementation doc or `admin/` code yet) |
| `stock-assistant-data-ingestion/docs/architecture/system-design.md` | SADI TAD |
| `stock-assistant-data-ingestion/docs/implementation.md` | SADI implementation doc |
| `stock-assistant-data-ingestion/progress.md` | SADI implementation progress & open questions |
| `stock-assistant-pipeline-intelligence/docs/architecture/system-design.md` | SAPI TAD (no implementation doc yet) |

---

## Project Status

- [x] System design documentation complete (SADI, SAPI, Admin)
- [x] API specification complete
- [x] SADI data source probe validated (all 4 sources pass)
- [x] SADI service implementation — all 10 phases done: config, DB/Redis clients, models, 4 crawlers (HKEX, Ming Pao, AAStocks, Yahoo HK), cleaning pipeline, API routes, Alembic migrations, Dockerfile. Q-2 and Q-9 (previously open in `progress.md`) were both resolved 2026-09-14 via an 11-day real-traffic volume test — see `progress.md` (Open Questions): `CLEAN_BODY_MIN_LENGTH=50` validated as-is, and per-record DB batching determined not worth the complexity at observed volume.
- [ ] SAPI service implementation — not started, design-only
- [ ] Admin service implementation — not started, design-only, `admin/` directory does not exist yet
- [x] Docker Compose setup — done for SADI (`sadi` + `postgres:16-alpine` + `redis:7-alpine`); not yet extended to SAPI/Admin