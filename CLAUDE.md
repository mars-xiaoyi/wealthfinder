# Mars Wealthfinder — Claude Code Guide

## Project Overview

**Mars Wealthfinder (MWP)** is a Hong Kong equity news intelligence system. It ingests news from multiple sources, enriches it through an NLP and scoring pipeline, and produces a ranked "Morning Brief" of stock-relevant events for investment research.

The system is in the **architecture/design phase** — all three services are fully documented but not yet implemented. The only runnable code is a SADI proof-of-concept (POC) for validating data sources.

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
| `admin/docs/system-design.md` | Admin TAD |
| `admin/docs/implementation.md` | Admin implementation doc |
| `stock-assistant-data-ingestion/docs/architecture/system-design.md` | SADI TAD |
| `stock-assistant-data-ingestion/docs/implementation.md` | SADI implementation doc |
| `stock-assistant-pipeline-intelligence/docs/system-design.md` | SAPI TAD |
| `stock-assistant-pipeline-intelligence/docs/implementation.md` | SAPI implementation doc |

---

## Project Status

- [x] System design documentation complete (SADI, SAPI, Admin)
- [x] API specification complete
- [x] SADI data source probe validated (all 4 sources pass)
- [ ] SADI service implementation
- [ ] SAPI service implementation
- [ ] Admin service implementation
- [ ] Docker Compose setup