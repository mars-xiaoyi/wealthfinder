# Mars Wealthfinder — Claude Code Guide

## Project Overview

**Mars Wealthfinder (MWP)** is a Hong Kong equity news intelligence system. It ingests news from multiple sources, enriches it through an entity analysis and scoring pipeline, and produces a ranked "Morning Brief" of stock-relevant events for investment research.

All three services are fully documented. **SADI implementation is substantially complete** (all 10 planned phases done — crawlers, cleaning pipeline, API, DB migrations, Docker packaging — see [stock-assistant-data-ingestion/progress.md](stock-assistant-data-ingestion/progress.md) for status and open questions). **SAPI and Admin are still design-only**, with no implementation code yet.

---

## Working Rules

Before implementing any feature:
1. **Read the relevant system-design doc and implementation doc first.** See [Key Documentation](#key-documentation) for file paths.
2. **Check Open Questions in the system-design doc.** Some sections have unresolved design decisions that block implementation. Do not implement around them — flag them instead.
3. **Do not invent structure.** If a directory or file does not exist, confirm the target path before creating it.
4. **Design decisions need approval before being applied, not explained after.** If a fix or change involves a genuine technical/design decision — a new pattern, a new type or relationship between things, a scope call, anything with a plausible alternative — stop and present the design and trade-offs first, then wait for explicit approval before writing it in. This applies even inside fast-moving edits where small mechanical fixes are getting quick go-aheads; a design decision always gets pulled out and surfaced on its own rather than folded into the same turn as the edit.
5. **While working on an implementation doc, resolve issues that affect the implementation — don't park them as Open Questions.** Open Questions are for decisions that genuinely need someone else's input (rule #2 above); they are not a place to defer something with a clear, in-scope fix. If an inconsistency or gap is found while writing the doc and it can be resolved directly, resolve it — don't leave it logged as unresolved. And never do the reverse either: an issue that has actually been fixed must not still be recorded as an open question.
6. **Don't explain why a dropped solution was dropped, inside the doc itself.** When a design changes during review — an enum removed, a wrapper type merged away, a field renamed — describe the resulting code as it is now. Don't leave a trailing note about what used to be there or why the earlier approach didn't work; implementation/system-design docs describe the current design, not a changelog. Keep that history only if specifically asked to.
7. **Don't restate a fact in multiple places.** Before writing a sentence, check whether it's already covered earlier in the doc, or covered in more detail in a later section — cross-reference with a `§X` pointer instead of re-explaining. Each fact should be authoritative in exactly one place; a callout that just repeats a nearby Purpose block or duplicates a later section's detailed rationale should be trimmed down to whatever it actually adds.
8. **Strongly type variables that cross a class or module boundary, whenever possible.** A bare `dict`/`tuple` of primitives passed between classes (e.g. a function returning `tuple[tuple[str, str, str], dict]` instead of a named dataclass) loses field names and types at every boundary it crosses, not just at the DB/Redis/wire edges where untyped data genuinely has to start. Give it a real dataclass in `app/models/` instead, even if that means introducing a small type used by only two or three classes. Internal-only locals (a single function's own working state, never passed elsewhere) don't need this — the rule is about what crosses a boundary, not every variable.
9. **Repository functions stay independent from business logic — they read and write faithfully, they don't decide.** A repository method (§10's `EntityEventRepository`/`EventRepository`/`EventScoreRepository`) should never bake in a business-specific rule just because it happens to be convenient there — that decision belongs in the Service/business-logic layer that actually needs it, even if it means a slightly longer call chain. Two concrete instances this bit: an `EntityEventRepository` read query silently applying `COALESCE(published_at, created_at)` — a business fallback only `merge_or_create()` (the Aggregation Layer's own logic) needed — until it was moved into `merge_or_create()` itself, so the repository's other caller (`ScoringService`) wasn't also silently affected by a rule it never asked for; and `EventRepository`'s own UPSERT computing `aggregation_updated_at = now()` itself, when that timestamp is a business-logic value (Scoring's staleness key) that only `merge_or_create()` — the place that knows whether an Event's aggregation state actually changed — should set. When in doubt: does this repository method's caller other than the one you're thinking about right now also want this behavior? If not, it doesn't belong in the repository.
10. **system-design.md and implementation.md each own a distinct layer of content — don't let one duplicate the other.** `system-design.md` is the high-level technical design: architecture, challenges/edge cases, and design proposals/rationale — no config defaults, no full request/response or DB schemas, no literal algorithm pseudocode, no LLM prompts. `implementation.md` is the buildable reference a dev or AI agent codes directly from: pseudocode/code, exact schemas, config classes, prompts — kept simple and non-duplicative, a first-time reader should be able to follow it without wading through prose. Both target the same audience (architects, devs, AI agents), so neither should re-explain what the other already states — cross-reference with a `§X` pointer (rule 7) instead of restating a formula, schema, or config table in both places. When a design decision has real rationale worth keeping, it lives once in system-design.md; implementation.md points back to it rather than repeating it.

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
SAPI (Python/FastAPI)    — entity analysis enrichment, event scoring & caching
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

Always read the relevant documents before implementing any feature. All design decisions are defined in the system-design and implementation docs — use the latest version.

| File | Description |
|------|-------------|
| `docs/api.md` | API Spec — all endpoints, schemas, error codes |
| `docs/admin-tad.md` | Admin TAD (no implementation doc or `admin/` code yet) |
| `stock-assistant-data-ingestion/docs/system-design.md` | SADI system design doc |
| `stock-assistant-data-ingestion/docs/implementation.md` | SADI implementation doc |
| `stock-assistant-data-ingestion/progress.md` | SADI implementation progress & open questions |
| `stock-assistant-pipeline-intelligence/docs/system-design.md` | SAPI system design doc |
| `stock-assistant-pipeline-intelligence/docs/implementation.md` | SAPI implementation doc |

---

## Project Status

- [x] System design documentation complete (SADI, SAPI, Admin)
- [x] API specification complete
- [x] SADI data source probe validated (all 4 sources pass)
- [x] SADI service implementation — all 10 phases done: config, DB/Redis clients, models, 4 crawlers (HKEX, Ming Pao, AAStocks, Yahoo HK), cleaning pipeline, API routes, Alembic migrations, Dockerfile. Q-2 and Q-9 (previously open in `progress.md`) were both resolved 2026-09-14 via an 11-day real-traffic volume test — see `progress.md` (Open Questions): `CLEAN_BODY_MIN_LENGTH=50` validated as-is, and per-record DB batching determined not worth the complexity at observed volume.
- [ ] SAPI service implementation — not started, design-only
- [ ] Admin service implementation — not started, design-only, `admin/` directory does not exist yet
- [x] Docker Compose setup — done for SADI (`sadi` + `postgres:16-alpine` + `redis:7-alpine`); not yet extended to SAPI/Admin