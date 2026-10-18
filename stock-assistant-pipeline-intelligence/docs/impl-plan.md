# SAPI — Implementation Plan (Ticket Breakdown)

| Field | Detail |
|---|---|
| Service | Stock Assistant Pipeline Intelligence (SAPI) |
| Status | DRAFT |
| Source of truth | `stock-assistant-pipeline-intelligence/docs/system-design.md` (`design.md` below) and `docs/implementation.md` (`impl.md` below) |
| Purpose | Split the already-approved SAPI design into independently-committable tickets |

This document does not introduce new design — every ticket below implements something already specified in `design.md`/`impl.md`. Its only job is to split that work into tickets and tell each assignee what their ticket covers; it is not a second copy of the spec. Exact signatures, types, schemas, and pseudocode live in `impl.md`/`design.md` — this doc points at the `§` section, it doesn't restate it. Where a ticket's scope required a judgment call not explicit in those docs (e.g. test file layout), it's flagged inline as **PM note**, not presented as settled design.

---

## How to use this doc

- **One ticket = one PR**, sized so a dev can pick it up, implement, and get it reviewed without needing to also implement something downstream of it. "Acceptance criteria" is the Definition of Done — all listed test scenarios pass, plus whatever the repo's standard CI runs (lint, type-check).
- **Each ticket is a vertical slice of an observable system behavior**, not a technical layer. A ticket builds exactly what its own behavior needs — config fields, infra clients, domain types — and nothing else; shared infrastructure has no ticket of its own and gets built incrementally by whichever ticket first needs it. If a later ticket needs more from something an earlier ticket built, the later ticket adds it and says so in its own scope — it doesn't get pre-built speculatively.
- **For exact signatures, read `impl.md`.** This doc tells you *which* class/function a ticket touches and *how much of it* (a ticket may only need one method off a larger class) — the `§` pointer is where the binding contract actually lives. If `impl.md` itself has a gap (describes a behavior but doesn't expose a way to achieve it), flag that in `impl.md` directly rather than inventing an interface here.
- **No "Files" line.** A ticket's Scope and Docs pointers already say which modules it touches; a separate file list is one more thing that can drift out of sync with the actual code. Not included on any ticket below, and not added to new ones.
- **Code conventions** (root `CLAUDE.md`, unchanged for this doc): strongly type everything crossing a module boundary; repositories stay free of business logic; no comments except where a non-obvious WHY needs recording; all LLM-facing/user-facing text is Traditional Chinese, everything else is English.
- **Every process-wide resource follows the same `create_X()` / `get_X()` singleton pattern** (impl.md §5.1) once more than one ticket needs to reach it. A resource used by exactly one ticket doesn't need this pattern yet.
- **A ticket creates its own package directory** the moment it adds the first file into it — no separate scaffolding ticket pre-creates subpackages for work that hasn't started.
- **A class built "minimal" stays minimal until a real need grows it.** Several classes below (`DatabaseClient`, `RedisStreamClient`, `RedisStateClient`) expose only the one or two methods their first consumer actually calls, not their full eventual method set from `impl.md`. The ticket that first needs another method adds it to the existing class at that point.
- **Test layout (PM note, not in impl.md):** mirror SADI's convention — `tests/<module_path>/test_<file>.py` under the service root, pytest. A ticket with a live-dependency risk (an external API whose real response shape could drift) owns its own "Integration testing" note rather than deferring all live checks to one end-of-project ticket.
- **SAPI is tested and deployed independently of other MWP services.** In integration passes, anything another MWP service provides (SADI's API and its `stream:raw_news_cleaned` messages, Admin's API) is mocked; SAPI's own infra (Postgres, Redis) is real, and third-party APIs whose real behaviour is the ticket's risk (Vertex AI, HKEXnews) are called live. No implementation ticket depends on another MWP service being built or deployed. Post-Release Tuning Validation tickets are the exception — a production-like volume run, measured on real traffic by definition.
- **Status** tracks each ticket's own lifecycle, independent of implementation progress: `Designing` (default — scope/boundaries here may still change), `Designed` (scope finalized and reviewed, ready to create in the tracker), `Created` (an actual ticket now exists in the tracker for this work).

---

## Tickets

### Health Check: `GET /v1/health` (M)

**Status:** Created
**Depends on:** nothing
**Scope:** Implement `GET /v1/health` (impl.md §15.2, api.md §3.2) with real `database`/`redis_stream`/`redis_state` checks. Build only the slice of each piece this needs — not each class's full eventual shape:
- `app/config.py`: just the `DatabaseConfig`/`RedisConfig` fields pool creation and connection need, plus `HealthConfig` (§4.1) — none of the retry/reconnect fields, nothing uses those yet.
- `app/common/error_codes.py`/`exceptions.py`: `SAPIException`/`ServiceUnavailableException` and the one catalog entry its default needs (§15.1).
- `app/db/connection.py`: `DatabaseClient`, `fetch_one()` only (§5.1) — no writes, retry, or transactions.
- `app/redis/stream_client.py`/`state_client.py`: `ping()` only (§6.1/§6.2).
- `app/api/schemas.py`: `HealthResponse`'s full shape, since it's a fixed external contract — but `hk_stock_list`/`layers` are hardcoded to their honest failure states here, since nothing real exists yet to check either.
- Wire it together per §15.2/§16: DB/Redis connect at startup with no failure tolerance — mandatory infra, unlike the HK Stock List ticket's deliberately resilient handling.
**Acceptance criteria** (per impl.md §15.2/api.md §3.2's status rules):
- All real checks (DB, Redis stream, Redis state) passing → their fields report `ok`, but overall status stays `unhealthy`, `HTTP 503` — the hardcoded `hk_stock_list=not_ready` forces it per §15.2's status rule. `HTTP 200` becomes reachable only in the next ticket.
- A DB or Redis failure → `unhealthy`, `HTTP 503`.
- Hardcoded `hk_stock_list`/`layers` fields always report their honest failure state, never a fabricated "ok".
- Unit-tested against mocked asyncpg/redis.
- A live Postgres/Redis integration pass: connectivity, and a stopped *or paused* Postgres/Redis reported as `error` within `HEALTH_CHECK_TIMEOUT_S`, the three checks running concurrently.
**Docs:** impl.md §3.5, §5.1, §6.1/§6.2, §15.1, §15.2; api.md §3.2.
**Explicitly deferred:** `hk_stock_list`'s real check (next ticket); `layers.*` (needs real consumer Tasks, Phase 1+); `DatabaseClient`'s write/retry methods; the Redis clients' full method sets; `AppConfig`'s remaining sub-configs; domain models; LLM adapter; Skill base; `WorkerPool`; `NewsClient`/`AdminClient`; Docker/compose; migrations; `.env.example`.

---

### Startup & Health: Fetch & Cache HK Stock List (S)

**Status:** Created
**Depends on:** Health Check: `GET /v1/health` (for `SAPIException`/`ServiceUnavailableException`, `app/logger.py`, `app/api/schemas.py`/`routes/health.py`, `app/main.py`)
**Scope:** Implement SAPI's eager startup HK Stock List load and wire its readiness into the health check (impl.md §4.2, §15.2, §16).
- `app/config.py`: `HttpConfig` (§4.1) — the one common outbound-call timeout, deliberately decoupled from `LLMConfig`'s own timeout (different SDK, different latency profile).
- `app/common/http_client.py`: the shared `httpx` client singleton (§4.2).
- `app/common/error_codes.py`/`exceptions.py`: `ParseException` and the two new catalog entries this ticket raises (§15.1).
- `app/common/hk_stock_list.py`: `Stock`, `HKStockListCache`, `refresh_hk_stock_list()`, `get_hk_stock_list_cache()` (§4.2) exactly as specified — fetch/filter/cache-rebind, resource-vs-code exception split. `get_hk_stock_list_cache()`'s first real caller is the health route below, not `lookup_stock` (Stock Resolution: `lookup_stock`).
- `app/main.py`/`app/api/routes/health.py`: wire the startup load and the health check's real `hk_stock_list` field per §16/§15.2 — this one failure must never crash startup, unlike the mandatory DB/Redis connects from the previous ticket.
**Acceptance criteria** (per impl.md §4.2/§15.2):
- Fetch/filter/cache behavior matches §4.2 exactly — equity-only filtering, the resource-vs-code exception split, fallback-cache-only-if-none-registered-yet, full rebind rather than mutation.
- `GET /v1/health` reports `hk_stock_list` honestly — `ok` once loaded, `not_ready` (and overall `unhealthy`) otherwise.
- The Health Check ticket's integration cases re-run against the real `hk_stock_list` check: with everything up the response is now `degraded`, `HTTP 200` (`layers.*` is still hardcoded to `error`, so `healthy` isn't reachable yet), and recovery from a DB/Redis failure returns to `degraded`, `HTTP 200`.
- The common HTTP timeout is actually applied to the shared client, not left at a library default.
- Unit-tested against mocked `httpx`.
- A live-HKEX integration pass — the real risk here is HKEX's response shape drifting, not something a fixture can catch.
**Docs:** impl.md §4.1 (`HttpConfig` only), §4.2, §15.2 (the `hk_stock_list` field and its status-derivation arm).
**Explicitly deferred:** `POST /v1/admin/hk-stock-list-sync` (next ticket); `lookup_stock`/`_fuzzy_match`/`_lcs_length` and its log event (Stock Resolution: `lookup_stock` — carries design.md §9.2/§9.2.1 and Open Question I-1 with it); everything else listed in the previous ticket's deferred section.

---

### On-Demand Sync: `POST /v1/admin/hk-stock-list-sync` (S)

**Status:** Created
**Depends on:** Startup & Health: Fetch & Cache HK Stock List (for `refresh_hk_stock_list()`)
**Scope:** Expose `refresh_hk_stock_list()` as `POST /v1/admin/hk-stock-list-sync` (api.md §3.4, impl.md §15.5) — a thin wrapper, no new business logic.
- `HKStockListSyncResponse` schema (§3.5).
- The route itself, registered via `APIRouter` + `app.include_router()` from `app/main.py`.
- Exception handlers for `ServiceUnavailableException`/`ParseException`, registered directly in `app/main.py` rather than the route module — decorating `app` from the route file would create a circular import (`app/main.py` already imports the route's router).
**Acceptance criteria** (per api.md §3.4):
- Success returns `entries_loaded` matching `refresh_hk_stock_list()`'s own return value.
- Both documented failure modes map to their correct HTTP status/`error_code`, and leave the existing cache untouched.
- Unit-tested against mocked `httpx`.
- Integration pass extends the previous ticket's live-HKEX test.
**Docs:** impl.md §3.5, §15.5; api.md §3.4.
**Explicitly deferred:** the generic exception-handler table and remaining error-code entries; `lookup_stock` (Stock Resolution: `lookup_stock`); everything else in the previous tickets' deferred sections.

---

### Entity Analysis: Consume Cleaned News & Fetch Article (M)

**Status:** Created
**Depends on:** Startup & Health: Fetch & Cache HK Stock List (for `get_http_client()`/`HttpConfig`, `SAPIException`/`ServiceUnavailableException`, `app/logger.py`)
**Scope:** Consume `stream:raw_news_cleaned` and fetch each article from SADI — no LLM analysis or persistence yet (impl.md §6.1, §9.1; this ticket implements a deliberately narrow subset of §11.2).
- `app/config.py`: `RedisConfig.reconnect_interval_s` (needed now — §12.1's Reconnect behavior lives at the consumer loop's own call site) and a minimal `EntityAnalysisConfig` — just the fields a bare read-and-fetch loop needs, not the ones backing `WorkerPool`/reclaim/the LLM skill.
- `app/redis/stream_client.py`: the stream/group constants, `StreamMessage`, `create_group_if_not_exists()`/`read_from()`/`ack()` (§6.1) — not `publish()`/`autoclaim()`, nothing publishes or reclaims yet.
- `app/clients/news_client.py`: `NewsClient` (§9.1), its first real consumer.
- `app/entity_analysis/cleaned_news_consumer.py`: a single consumer loop (no `run_supervised()` — nothing to supervise yet with only one coroutine) that reads, fetches, and ACKs on a terminal outcome (success or 404) but deliberately leaves a client failure un-ACKed, so a transient SADI failure stays recoverable once a future ticket adds the reclaim loop, rather than being silently discarded now.
- Wire `NewsClient` and the consumer into `app/main.py`'s lifespan.
**Acceptance criteria** (per impl.md §6.1/§9.1/§11.2):
- A stream message's `cleaned_id` (a string on the wire) is correctly parsed into the type `fetch_cleaned_news()` expects.
- Success and a 404 both ACK; a client failure (including a timeout) does not, and the loop keeps going.
- A Redis connection failure triggers the documented warn/sleep/retry at the consumer loop's own call site, without crashing.
- Unit-tested against mocked Redis/`httpx`.
- Integration pass against real Redis, with SADI's `GET /v1/cleaned_news/{cleaned_id}` (api.md §2.4) mocked — success, 404, and failure responses.
**Docs:** impl.md §6.1, §9.1, §11.2 (this ticket implements a subset); api.md §2.4; design.md §3.2, §3.3.
**Explicitly deferred:** `run_supervised()`/`_reclaim_loop()`/dead-letter routing (also what will eventually recover the un-ACKed messages above); the `processing_cleaned_ids` dedup guard; the `hk_stock_list_not_ready` gate (Entity Analysis: HK Stock List Readiness Gate); `WorkerPool`; the LLM-based entity analysis step itself (Entity Analysis: LLM Entity Extraction; `lookup_stock` itself is Stock Resolution: `lookup_stock`); the `v` schema-version check; `EntityAnalysisConfig`'s remaining fields.

---

### Spike: Entity Analysis LLM Skill (S)

**Status:** Created
**Depends on:** Startup & Health: Fetch & Cache HK Stock List (for the real HKEX list fetch)
**Scope:** Throwaway scripts in `poc/sapi/` against live Vertex AI, answering the questions whose answers would change `impl.md`'s design before Entity Analysis: LLM Entity Extraction codes against it — no production code.
- **I-2:** pin `google-genai`/`instructor` versions; confirm the token-count attribute path and whether Instructor surfaces a retry count.
- **FC termination:** does `function_calling_config.mode = ANY` (what §7.3 maps `strict=True` to) force a function call every turn, making the loop's normal `no tool_calls` exit unreachable? Compare against `AUTO`.
- **`tool_call_id`:** does Vertex populate `FunctionCall.id`, including with several calls in one turn?
- **Instructor with FC history:** does `instructor.from_genai(..., GENAI_STRUCTURED_OUTPUTS)` accept a message history containing function-call/function-response turns, as §8.2's final `generate_structured()` call requires?
- **Prompt sanity:** design.md §9.4's few-shot articles plus a handful of real SADI articles, end to end through the FC loop and structured-output call.
- Record every `lookup_stock` argument the model sends, as extra cases for the fuzzy-matching spike's case set.
- **Initial-value sanity:** per-call latency of FC rounds and the structured-output call (Q-11); FC rounds used and calls per turn on a many-entity article (Q-7); `output_tokens` on the largest article in the set (I-3).
- **Quota model:** record which quota model applies to the pinned model and region — fixed RPM, Dynamic Shared Quota, or Provisioned Throughput. If fixed RPM, set the `ENTITY_ANALYSIS_MAX_CONCURRENT` default below it; otherwise keep the impl.md default and leave tuning to Q-9.
**Acceptance criteria:**
- Every item above has a written answer with evidence (raw response printed, or measured numbers).
- Each design-changing answer is written back into `impl.md` (§7.1/§7.3/§8.2) as a fix, not an open question, and approved before the implementation ticket starts; I-2 marked resolved in §18.
- Initial values for `LLM_API_TIMEOUT_S`, `MAX_FC_ROUNDS`/`MAX_FC_RETRIES_PER_ENTITY`, and EntityAnalysisSkill's `max_tokens` — plus `ENTITY_ANALYSIS_MAX_CONCURRENT` if the quota is a fixed RPM — are confirmed or changed in impl.md (§4.1, §8's table), with the measurements behind them.
**Docs:** impl.md §7.1, §7.3, §8.2, §18 (I-2); design.md §9.3, §9.4.
**Explicitly deferred:** calibrating Q-7, Q-11, I-3 at volume, and Q-9 (Entity Analysis: Post-Release Tuning Validation); fuzzy matching (its own spike).
**PM note:** time-box length to be set.

---

### Spike: `lookup_stock` Fuzzy Matching (S)

**Status:** Created
**Depends on:** Startup & Health: Fetch & Cache HK Stock List (for the real HKEX list fetch)
**Scope:** Throwaway scripts in `poc/sapi/` exercising §4.2's `lookup_stock`/`_fuzzy_match` as specified against the real HKEX list — no LLM involved.
- **Q-16 (offline part):** a hand-collected set of company mentions from real SADI articles — abbreviations (阿里 → 阿里巴巴集團控股有限公司), English names, A/H dual-listed names, non-HKEX companies that should miss, and ambiguous shared-prefix names (騰訊 → 騰訊控股 vs 騰訊音樂, generic terms like 地產) — recording expected vs. actual match for each.
- Sensitivity of results to `NAME_MATCH_MIN_OVERLAP_RATIO` (default 0.6), and whether `set` vs. `Counter` intersection (§4.2's implementation note) changes any outcome.
- **I-1 micro-benchmark:** per-call latency over the full list, plus a heartbeat-gap check for event-loop blocking.
**Acceptance criteria:**
- A results table of correct/wrong/missed matches, with ties called out and the cause of each.
- I-1's pre-traffic part has measured numbers and a decision on whether `asyncio.to_thread()` is needed.
- Any algorithm or default change is written back into impl.md §4.2 and design.md §9.2 as a fix and approved; I-1's pre-traffic part marked resolved in §18 (Q-16 stays open only for its at-scale check, Entity Analysis: Post-Release Tuning Validation).
**Docs:** impl.md §4.2, §18 (I-1, Q-16); design.md §9.2, §9.2.1.
**Explicitly deferred:** Q-16 at scale and I-1's real-traffic part (Entity Analysis: Post-Release Tuning Validation).

---

### Stock Resolution: `lookup_stock` (S)

**Status:** Created
**Depends on:** Spike: `lookup_stock` Fuzzy Matching (for the final §4.2 algorithm/defaults); Startup & Health: Fetch & Cache HK Stock List (for `HKStockListCache`/`get_hk_stock_list_cache()`/`Stock`)
**Scope:** Resolve a company name or stock code against the in-process HK Stock List (impl.md §4.2, as revised by the fuzzy-matching spike).
- `app/common/hk_stock_list.py`: `lookup_stock()`, `_fuzzy_match()`, `_lcs_length()`, and the `lookup_stock_completed` log event with its `_SLOW_CALL_THRESHOLD_MS` WARNING escalation.
- No config change — `min_overlap_ratio` is a parameter; `EntityAnalysisConfig.name_match_min_overlap_ratio` is added by its first real reader (LLM Entity Extraction).
**Acceptance criteria** (per impl.md §4.2):
- Exact-code path: `"700"`/`"0700"`/`"00700.HK"` all resolve to `"00700"`; the ≥2-digit rule separates code from name; `candidates_scanned=0`.
- Fuzzy path: overlap-ratio floor, overlap-then-LCS ranking, `candidates_scanned=len(cache)`; a miss returns `None`.
- `lookup_stock_completed` carries every §4.2 field, at WARNING above the threshold.
- Unit-tested against a seeded `HKStockListCache`.
- The fuzzy-matching spike's case table becomes a regression fixture, run once against the live HKEX list as the integration pass.
**Docs:** impl.md §4.2 (`lookup_stock` onward); design.md §9.2, §9.2.1. Validated by: Entity Analysis: Post-Release Tuning Validation (Q-16 at scale, I-1's real-traffic part).
**Explicitly deferred:** the `lookup_stock` tool wiring, wire-shape dict, and retry `instruction` (the FC loop — LLM Entity Extraction).

---

### Entity Analysis: HK Stock List Readiness Gate (XS)

**Status:** Created
**Depends on:** Entity Analysis: Consume Cleaned News & Fetch Article (for `CleanedNewsConsumer._process_one()`). Must land before the persistence ticket — without it, an empty HK Stock List yields a wrong empty result that gets ACKed and persisted.
**Scope:** Add the `hk_stock_list_not_ready` gate as `CleanedNewsConsumer._process_one()`'s first check (impl.md §11.2 callout, §11.4 last row): `len(get_hk_stock_list_cache()) == 0` (synchronous, no `await`) → log warning, leave the message un-ACKed. No new config, types, or infra.
**Acceptance criteria:**
- An empty cache logs `hk_stock_list_not_ready`, leaves the message un-ACKed, and never fetches the article.
- A non-empty cache proceeds exactly as before.
- Unit-tested with a seeded empty and non-empty `HKStockListCache`.
- The integration cases from Entity Analysis: Consume Cleaned News & Fetch Article re-run with the HK Stock List loaded: success and `404` ACK; `503`, timeout and unreachable stay un-ACKed.
**Docs:** impl.md §11.2, §11.4.
**Explicitly deferred:** redelivery of gated messages (the reclaim-loop ticket).

---

### Entity Analysis: LLM Entity Extraction (L)

**Status:** Created
**Depends on:** Spike: Entity Analysis LLM Skill (for pinned SDK versions and any §7/§8.2 fixes); Stock Resolution: `lookup_stock`; Entity Analysis: HK Stock List Readiness Gate (for `CleanedNewsConsumer._process_one()`'s current shape)
**Scope:** Run `EntityAnalysisSkill` on each fetched article and act on its result — no persistence or publishing yet (impl.md §7, §8.1, §8.2, §11.1 steps 1–2 and 4–6, up to building `EntityEvent`s).
- `app/config.py`: `LLMConfig` minus `rate_limit_backoff_s`; `EntityAnalysisConfig` gains `name_match_min_overlap_ratio`/`max_fc_rounds`/`max_fc_retries_per_entity`.
- `app/llm/`: `adapter.py` types/interface (including `GenerationConfig`, passed on every call), `exceptions.py`, `VertexAIAdapter` + `create_llm_adapter()`/`get_llm_adapter()` (§7.1–§7.3), built against the spike's pinned SDK versions.
- `app/models/`: `enums.py` (§3.1), `versioned_metadata.py` (§3.2), `AnalysisOutcome`/`EntityEvent` in `entity_event.py` (§11.1).
- `app/skills/`: `skill_base.py` (§8.1), `entity_analysis_skill.py` (§8.2) — FC loop calling `lookup_stock()`, `LOOKUP_STOCK_TOOL`, verbatim system prompt, business-constraint validation, dedup backstop.
- `app/entity_analysis/entity_analysis_service.py`: `EntityAnalysisService` + singleton (§11.1), constructed with `NewsClient` only; `find_entity_events()` runs steps 1–2 and 4–6 and logs `entity_analysis_completed` without `entity_event_ids` (added by the persistence ticket).
- `CleanedNewsConsumer._process_one()`: after the readiness gate, calls the Service instead of `NewsClient` directly, inside `except LLMRateLimitException`/`except LLMException`/`except Exception` (§11.2) — no ACK on any; only the catch-all logs `entity_analysis_process_one_crashed`.
- Wire `create_llm_adapter()`/`close()` and `create_entity_analysis_service()` into `app/main.py`'s lifespan (§16).
**Acceptance criteria** (per impl.md §8.1/§8.2/§11.1/§11.4):
- FC loop: stops at `MAX_FC_ROUNDS` with `max_rounds_reached`; `instruction` and `UnverifiedEntity` only once `MAX_FC_RETRIES_PER_ENTITY` is hit; unverified `stock_code`s dropped; duplicate `stock_code`s keep the first.
- Every row of §8.2's Business Constraint Validation table applied and logged without setting `failure_reason`.
- A verified `stock_code` missing from the cache at §11.1 step 6 is dropped with `entity_stock_no_longer_listed`; the other items proceed.
- `SkillBase.run()`: all four LLM exceptions → `failure_reason=type(err)`; only `LLMAuthenticationException` logs CRITICAL; anything else propagates uncaught.
- Every Adapter call from `EntityAnalysisSkill` passes its `_GENERATION_CONFIG` (temperature 0, max output tokens 8192, §8's table), and `VertexAIAdapter` applies it to the request.
- Consumer: success, `entities=[]`, and 404 ACK; every `LLMException` and any unanticipated exception leave the message un-ACKed and the loop keeps going.
- Unit-tested with a fake `LLMAdapter` and a seeded `HKStockListCache`.
- Integration pass against live Vertex AI with the LLM spike's article set.
- The integration cases from Entity Analysis: Consume Cleaned News & Fetch Article for `404`, `503`, timeout and unreachable SADI re-run through `EntityAnalysisService`: the same ACK/un-ACK results, with a SADI failure now also logging ERROR `entity_analysis_process_one_crashed`.
**Docs:** impl.md §3.1, §3.2, §4.1, §4.2 (`lookup_stock` call contract only), §7, §8.1, §8.2, §8.5, §8.6, §11.1, §11.2, §11.4; design.md §9.3, §9.4, §9.7, §9.8. Validated by: Entity Analysis: Post-Release Tuning Validation (Q-7, Q-11, I-3).
**Explicitly deferred:** persistence and the redelivery check (`EntityEventRepository`, migration, DB write methods, `entity_event_ids` in the log — Entity Analysis: Persist & Publish Entity Events); publishing to `stream:entity_event_completed`; `WorkerPool`/backoff; the dedup guard; reclaim/dead-letter; the other two Skills; the `v` schema-version check.

---

### Entity Analysis: Persist & Publish Entity Events (M)

**Status:** Created
**Depends on:** Entity Analysis: LLM Entity Extraction (for `EntityAnalysisService`, `EntityEvent`/`AnalysisOutcome`); Entity Analysis: HK Stock List Readiness Gate
**Scope:** Persist each article's verified `EntityEvent`s and publish them to `stream:entity_event_completed`, and republish instead of re-analysing an article that was already persisted — completing `find_entity_events()` (impl.md §11.1 steps 3 and 7–9; design rationale: design.md §3.4).
- `app/config.py`: `DatabaseConfig.max_retry`/`retry_base_wait_ms`.
- `app/db/`: `DatabaseClient.transaction()` (with its `execute_returning()`) and `fetch_all()` (§5.1); `_db_write_with_retry()` (§5.2); `exceptions.py` (§5.3). Not `DatabaseClient.execute()` — nothing here needs it.
- `alembic/`: Alembic setup and `001_create_entity_events.py` (§17), against SAPI's own `sapi` database on the shared Postgres instance — not SADI's `sadi`, so the two services' `alembic_version` tables never collide. Local setup per impl.md §1 (step 6).
- `app/persistent/entity_event_repository.py`: `EntityEventRepository` + singleton; `persist_entities()` (one transaction per article) and `fetch_entity_event_ids_by_source_url()` (§10.1). Not `_row_to_entity_event()` — nothing here reads a full row back.
- `app/redis/stream_client.py`: `publish()` with §6.1's reconnect behaviour; payload `entity_event_id` + `v="1"` (design.md §2.3).
- `EntityAnalysisService`: constructor gains the repo and stream client; `find_entity_events()` adds step 3 (redelivery check → republish, `entity_analysis_republished`) and steps 7–9; `entity_analysis_completed` gains the real `entity_event_ids`.
- `app/main.py`: `create_entity_event_repository()` after `create_db_client()` (§16).
**Acceptance criteria** (per impl.md §5.1/§5.2/§10.1/§11.1):
- A multi-entity article writes one row per entity, all in one transaction; a failure mid-article leaves no rows for it.
- Each row's columns and JSONB match §10.1 exactly — `additional_outcome` holds `version` plus exactly five `output` keys, `company_name_zh`/`company_name_en` come from `Stock`, `metadata` holds `{"cleaned_id": ...}`.
- Each inserted row is published once (`entity_event_id`, `v="1"`); `entities=[]` writes nothing, publishes nothing, still ACKs.
- An article whose rows already exist skips the Skill entirely (no LLM call), republishes every existing `entity_event_id`, logs `entity_analysis_republished`, and ACKs.
- The message is ACKed only after every insert and publish has completed (§6.1's `ack()` rule).
- Transient asyncpg errors retry with backoff up to `max_retry`; exhaustion raises `ServiceUnavailableException`, reaching the consumer's catch-all with no ACK.
- A Redis `ConnectionError` on publish triggers warn/sleep/retry.
- Unit-tested against mocked asyncpg/redis.
- Integration pass against real Postgres and Redis with a faked LLM adapter: migration upgrade/downgrade, one article producing rows and stream messages, and a redelivery of that article producing republished messages and no new rows.
- The integration cases from Entity Analysis: LLM Entity Extraction for `entities=[]`, each LLM failure class and SADI `404` re-run with persistence and publishing wired in: no rows and no stream entries are written, only `entities=[]` and `404` ACK, and each failure's completion record has `entity_event_ids == []`.
**Docs:** impl.md §1 (step 6), §5.1 (`transaction`, `fetch_all`), §5.2, §5.3, §6.1 (`publish`, reconnect, `v`), §10.1 (`persist_entities`, `fetch_entity_event_ids_by_source_url`), §11.1 (steps 3, 7–9, logging), §11.2 (Idempotency), §16, §17 (`001`); design.md §2.3, §3.4, §10.1, §12.1.
**Explicitly deferred:** Aggregation's duplicate check (`EventRepository.fetch_mapped_event_id()`, §12.1 — with the Aggregation ticket; nothing consumes `stream:entity_event_completed` until then, so a republished duplicate has no effect before it lands); `fetch_entity_event()`/`fetch_entity_events_for_event()`/`_row_to_entity_event()`, `DatabaseClient.execute()`, migrations `002`/`003` (Aggregation onward); the dedup guard; `WorkerPool`; reclaim/dead-letter; the consumer-side `v` check.

---

### Entity Analysis: Reclaim, Dead Letter & Schema-Version Check (M)

**Status:** Created
**Depends on:** Entity Analysis: Persist & Publish Entity Events (for `RedisStreamClient.publish()`, used for dead-lettering).
**Scope:** Recover messages this layer deliberately leaves un-ACKed (LLM failure, SADI failure, readiness gate, unanticipated crash) — today nothing redelivers them, since `XREADGROUP >` only ever returns new messages — and route the ones that keep failing, or that are malformed (missing or unrecognised `v`, missing or invalid `cleaned_id`), to the dead-letter stream (impl.md §6.1's Reclaim and Dead Letter pattern and Layer Supervision, §11.2).
- `app/config.py`: `RedisConfig.stream_claim_timeout_ms`, `EntityAnalysisConfig.max_retry`.
- `app/redis/stream_client.py`: `ReclaimedMessage`, `autoclaim()` (§6.1).
- `app/common/supervised.py`: `run_supervised()` (§6.1).
- `CleanedNewsConsumer`:
  - `start()` runs `_consumer_loop()` and `_reclaim_loop()` through `run_supervised()` (§11.2).
  - `_reclaim_loop()`: retry-exhaustion check → dead-letter + ACK; otherwise into the same `_process_one()` as the consumer loop. Each sweep wrapped in `except Exception` → `entity_analysis_reclaim_sweep_failed`.
  - The `processing_cleaned_ids` dedup guard (§11.2, design.md §3.4 Layer 1) — needed now, since this is the first time two coroutines can dispatch the same message concurrently.
  - The message shape check (§6.1's shape-check callout), immediately after `read_from()`/`autoclaim()` returns: a missing or unrecognised `v`, or a missing or invalid `cleaned_id`, goes straight to dead-letter + ACK, never reaching `_process_one()`; each message is handled in isolation (§6.1).
**Acceptance criteria** (per impl.md §6.1/§11.2):
- A message left un-ACKed is reclaimed after `STREAM_CLAIM_TIMEOUT_MS` and processed again through `_process_one()`.
- At `delivery_count >= ENTITY_ANALYSIS_MAX_RETRY`, the message is published to `stream:entity_analysis_dead_letter` with §6.1's exact payload and ACKed, without being processed.
- A message whose `v` is missing or unrecognised, or whose `cleaned_id` is missing or not a valid UUID, on either path, is dead-lettered with §6.1's payload (`delivery_count="1"` on the `read_from()` path) and ACKed without being processed; every other message in its batch is still processed normally.
- A message already in `processing_cleaned_ids` is skipped and left un-ACKed; the check-then-insert has no `await` in between.
- A failing sweep logs a warning and the loop continues; `asyncio.CancelledError` still propagates.
- `run_supervised()`: when one coroutine raises, the other is cancelled and awaited before the exception is re-raised.
- Unit-tested against mocked Redis.
- Integration pass against real Redis: a message forced to fail reaches the dead-letter stream after `ENTITY_ANALYSIS_MAX_RETRY` deliveries.
- The integration cases from Entity Analysis: Consume Cleaned News & Fetch Article (failed SADI fetch), Entity Analysis: HK Stock List Readiness Gate (gated message) and Entity Analysis: LLM Entity Extraction (LLM failure classes) re-run with the reclaim loop running: each un-ACKed message is redelivered after `STREAM_CLAIM_TIMEOUT_MS` and ACKed once its cause clears, and dead-lettered if the cause persists.
**Docs:** impl.md §4.1, §6.1 (`autoclaim`, `v` field, Reclaim and Dead Letter pattern, Layer Supervision), §11.2 (`start()`, `_process_one()`, `_reclaim_loop()`, Idempotency); design.md §2.3, §3.4, §12.1.
**Explicitly deferred:** `WorkerPool` (both loops call `_process_one()` directly until then); layer-Task monitoring and auto-restart; dead-letter consumption/reprocessing (Q-3, Post-MVP).

---

### Entity Analysis: Concurrency & Rate-Limit Backoff (M)

**Status:** Created
**Depends on:** Entity Analysis: Reclaim, Dead Letter & Schema-Version Check (both loops dispatch through `WorkerPool`)
**Scope:** Process articles concurrently, bounded by `ENTITY_ANALYSIS_MAX_CONCURRENT`, and pause the whole layer for `RATE_LIMIT_BACKOFF_S` when Vertex AI rate-limits (impl.md §11.3; design.md §3.3, §12.1 Rate Limit Backoff).
- `app/config.py`: `EntityAnalysisConfig.max_concurrent`, `LLMConfig.rate_limit_backoff_s`.
- `app/entity_analysis/worker_pool.py`: `WorkerPool` — `dispatch()` and `trigger_backoff()` (§11.3).
- `CleanedNewsConsumer`: takes a `WorkerPool` in its constructor (constructed in `app/main.py`, §16 — a layer-owned collaborator, not a singleton); both loops dispatch through it; `_process_one()`'s `except LLMRateLimitException` calls `trigger_backoff()`.
**Acceptance criteria** (per impl.md §11.2/§11.3):
- No more than `ENTITY_ANALYSIS_MAX_CONCURRENT` articles are processed at once.
- After `trigger_backoff()`, no new work starts until `RATE_LIMIT_BACKOFF_S` has elapsed — including a dispatch already waiting on the semaphore when the backoff fires; work already running is not interrupted.
- Each loop gathers its batch's dispatches and reads again only once all have returned (§11.3).
- An `LLMRateLimitException` from any one article triggers the layer-wide backoff and leaves that message un-ACKed.
- Unit-tested with fake coroutines and a controllable clock.
- Integration pass against live Vertex AI at the configured concurrency.
- The integration cases from Entity Analysis: Reclaim, Dead Letter & Schema-Version Check re-run with both loops dispatching through `WorkerPool`, and the `HTTP 429` case from Entity Analysis: LLM Entity Extraction re-run with the backoff in force: the same results, except that a rate-limited article now delays the next article's start by `RATE_LIMIT_BACKOFF_S`.
- Each backoff logs WARNING `rate_limit_backoff_started` with `layer` and `backoff_until`, and each gathered batch logs INFO `entity_analysis_batch_completed` with `batch_size` and `latency_ms` (impl.md §11.3).
**Config note:** `RATE_LIMIT_BACKOFF_S * 1000` must stay below `STREAM_CLAIM_TIMEOUT_MS` (§11.3), or messages paused by a backoff get reclaimed as stalled — a deployment constraint, not checked in code.
**Docs:** impl.md §4.1, §11.2, §11.3, §16; design.md §3.3, §12.1. Validated by: Entity Analysis: Post-Release Tuning Validation (Q-1, Q-9).
**Explicitly deferred:** the Scoring Layer's own `WorkerPool` instance (same class, its own config — Scoring ticket).

---

### Entity Analysis: Layer Monitoring & Health (S)

**Status:** Created
**Depends on:** Entity Analysis: Reclaim, Dead Letter & Schema-Version Check (for `run_supervised()`, so a restart never races a leftover sibling loop)
**Scope:** Auto-restart the Entity Analysis layer's Task when it crashes, and report its real liveness in `GET /v1/health` (impl.md §16 Coroutine Monitoring, §15.2, §15.6; design.md §12.1).
- `app/main.py`: `_make_monitored()` with its `done_callback` (CRITICAL `layer_task_crashed`, then restart), `layer_tasks` on `app.state`, and cancel-and-gather of every layer Task at shutdown (§16). Only `"entity_analysis"` is registered; each later layer adds its own line.
- `app/api/dependencies.py`: `get_layer_tasks` (§15.6).
- `app/api/routes/health.py`: `layers.entity_analysis` reflects whether its Task is running; `degraded` is derived per §15.2. The other three `layers.*` fields stay hardcoded to their failure state until their layers exist.
**Acceptance criteria** (per impl.md §15.2/§16; api.md §3.2):
- A crash in `CleanedNewsConsumer.start()` logs `layer_task_crashed` at CRITICAL and starts a new Task on the same consumer instance.
- A cancelled Task (shutdown) is never restarted.
- `layers.entity_analysis` flips between `ok` and `error` with the Task's state; overall status is `degraded`, `HTTP 200`, since no other layer exists yet.
- The Health Check ticket's integration cases re-run with `layers.entity_analysis` real: with everything up the response is `degraded`, `HTTP 200` — `database`/`redis_stream`/`redis_state`/`hk_stock_list` and `layers.entity_analysis` all `ok`, the other three `layers.*` still `error`. Each Postgres/Redis stop or pause gives `unhealthy`, `HTTP 503`, with only the failed field `error` and `layers.entity_analysis` still `ok` (the consumer retries rather than exiting, §6.1). Recovery returns to `degraded`, `HTTP 200`, without restarting the app.
- Shutdown cancels the Task and waits for it before closing DB/Redis/HTTP/LLM clients.
- Unit-tested with a fake consumer whose `start()` raises.
**Docs:** impl.md §15.2, §15.6, §16; api.md §3.2; design.md §7.1, §12.1.
**Explicitly deferred:** the other three layers' registration and `layers.*` fields (each layer's own ticket).

---

### Entity Analysis: Post-Release Tuning Validation (M)

**Status:** Created
**Depends on:** every Entity Analysis ticket above; Deployment: SAPI Container, Compose & Production-like Run (below — the real-traffic run), run for an agreed length.
**Scope:** Answer the Entity Analysis open questions that need real volume, from the logs the earlier tickets already produce (`entity_analysis_completed`, `llm_calls[]`, `lookup_stock_completed`) — no new instrumentation. Except Q-1, each starts from a value already sanity-checked by a spike.
- **Q-1:** `ENTITY_ANALYSIS_BATCH_SIZE`/`ENTITY_ANALYSIS_BATCH_TIMEOUT_MS`, from throughput and per-batch latency.
- **Q-7:** `MAX_FC_ROUNDS`/`MAX_FC_RETRIES_PER_ENTITY`, from `fc_summary` — how often `max_rounds_reached` fires, and the retry distribution.
- **Q-9:** `RATE_LIMIT_BACKOFF_S` (and `ENTITY_ANALYSIS_MAX_CONCURRENT`), from 429 frequency and recovery time.
- **Q-11:** `LLM_API_TIMEOUT_S`/`HTTP_TIMEOUT_S`, from p95/p99 call latency.
- **Q-16 at scale:** `lookup_stock` hit/miss rates, plus a hand-checked sample of unverified entities.
- **I-1 real-traffic part:** `lookup_stock_completed` p50/p95/p99 and fuzzy-path frequency.
- **I-3:** EntityAnalysisSkill's `max_tokens`, against `output_tokens` p95/p99 and any truncation.
**Acceptance criteria:**
- Each item has measured numbers and a decision: keep the default, or change it with the reason.
- Changed defaults are applied in `app/config.py` and impl.md §4.1/§8.
- Each answered item is marked resolved in impl.md §18, and in design.md §14 where it came from there.
**Docs:** impl.md §4.1, §8, §18; design.md §14.
**Explicitly deferred:** the Q-1/Q-9/Q-11 shares that belong to Aggregation and Scoring (each layer's own validation ticket).
**PM note:** run length to be set (SADI's equivalent volume test ran 11 days).

---

### Aggregation: Sliding-Window Merge Algorithm (S)

**Status:** Created
**Depends on:** Entity Analysis: LLM Entity Extraction (for `EntityEvent`/`AnalysisOutcome` and `app/models/`)
**Scope:** The pure merge decision — whether an incoming EntityEvent extends its group's active Event or starts a new one (impl.md §12.4; design rationale: design.md §4.4).
- `app/config.py`: `AggConfig.sliding_window_hours`/`event_max_timespan_hours`/`db_flush_interval_s` (the last one for `ttl_for()`'s grace period).
- `app/models/event.py`: `Event` without its `brief_summary` field, and `SourceListItem` (§3.3).
- `app/aggregation/sliding_window.py`: `merge_or_create()`, `is_late()`, `ttl_for()`.
**Acceptance criteria** (per impl.md §12.4):
- Merge vs. new Event decided correctly at the sliding-window and hard-cap boundaries (just inside, exactly at, just outside).
- `first_seen_at`/`last_seen_at` take min/max on out-of-order arrival, never a blind overwrite.
- `published_at=None` falls back to `created_at` for the computation only.
- `aggregation_updated_at` is set on both branches; a merge keeps `event_id` and appends the `entity_event_id`, a new Event gets a fresh `event_id`.
- `is_late()` is true only for an EntityEvent dated before the active Event's `last_seen_at` by more than `SLIDING_WINDOW_HOURS`, with the same `published_at` fallback.
- `ttl_for()` returns `min(SLIDING_WINDOW_HOURS, remaining lifespan)` in seconds plus `2 × AGG_DB_FLUSH_INTERVAL_S` — never `0`, even at the hard cap.
- Unit-tested with fixed timestamps. No integration pass — a pure function with no I/O; its real-world behaviour is exercised by Aggregation: Consume & Merge into Active Events.
**Docs:** impl.md §3.3, §4.1, §12.4; design.md §4.4, §11.2 (TTL grace period).
**Explicitly deferred:** `Event.brief_summary` and `BriefSummary` (added by the first Scoring ticket that writes it); everything with I/O — Redis, DB, streams.

---

### Aggregation: Consume & Merge into Active Events (L)

**Status:** Created
**Depends on:** Aggregation: Sliding-Window Merge Algorithm (for `merge_or_create()`/`ttl_for()`, `Event`); Entity Analysis: Persist & Publish Entity Events (for the `stream:entity_event_completed` payload, `EntityEventRepository`, migration `001`)
**Scope:** Consume `stream:entity_event_completed`, merge each EntityEvent into its group's active Event in Redis, and ACK once the Redis write lands (impl.md §12.1–§12.3, §12.5; design.md §4.2–§4.4).
- `app/config.py`: `AggConfig.batch_size`/`batch_timeout_ms`/`max_concurrent`/`group_idle_timeout_s`/`consumer_name`.
- `app/redis/state_client.py`: `hgetall()`/`hset()`/`expire()` only (§6.2).
- `app/aggregation/event_aggregation_store.py`: `EventAggregationStore` + singleton — `get_active_event()`/`upsert_active_event()` only, not `scan_all()` (§12.5).
- `app/persistent/entity_event_repository.py`: `fetch_entity_event()` and `_row_to_entity_event()` (§10.1).
- `app/aggregation/group_coroutine.py`: `GroupKey`, `GroupCoroutine` (§12.3).
- `app/aggregation/aggregation_service.py`: `AggregationService` + singleton — `process_entity_event()` with the missing-row ACK, the in-Redis half of the duplicate check, and `_process()`'s `try`/`except` → `on_failed()` (§12.1).
- `app/aggregation/aggregation_consumer.py`: `AggregationConsumer` with one consumer loop — `_route_one()` (including its pre-enqueue `try`/`except`), `_enqueued`, `_on_processed()` (§12.2).
- `app/main.py`: `create_event_aggregation_store()`, `create_aggregation_service()`, the consumer started as a plain Task in `layer_tasks` (so the existing shutdown cancels and awaits it before clients close), and `AggregationService.close()` at shutdown after the layer Tasks are awaited (§12.1, §16).
**Acceptance criteria** (per impl.md §12.1–§12.3, §12.6):
- EntityEvents of one group are merged strictly in order; different groups run concurrently, bounded by `AGG_MAX_CONCURRENT`.
- A group idle for `AGG_GROUP_IDLE_TIMEOUT_S` tears itself down; a later message for it recreates it.
- A message is ACKed only after its Redis write; the key's TTL is `ttl_for()`'s value.
- A missing `entity_events` row logs `aggregation_entity_event_missing` and ACKs without merging.
- An `entity_event_id` already in the active Event logs `aggregation_duplicate_skipped` and ACKs without merging again.
- A late EntityEvent (`is_late()`) logs `aggregation_late_entity_event_skipped` and ACKs; the active Event and its Redis key are untouched.
- A failure before or inside the unit of work logs `aggregation_route_failed`/`aggregation_process_failed`, leaves the message un-ACKed, and releases its `_enqueued` entry; the group and the consumer loop keep running.
- A Redis `ConnectionError` in the consumer loop triggers the documented warn/sleep/retry (§6.1), without crashing.
- Shutdown cancels and awaits the layer Task, then `AggregationService.close()` cancels any running groups, all before clients close.
- `GroupCoroutine` (§12.3): an exception escaping `work()` logs `aggregation_group_work_failed` at CRITICAL and the group continues with its next item (no re-raise, no teardown); an item enqueued just as the idle timeout fires is still processed, not abandoned; the idle timeout releases the group's dict entry and semaphore permit via `on_idle`.
- Unit-tested against fake Redis/repositories (failure paths included).
- Integration pass against real Redis and Postgres: seed `entity_events` rows and `XADD` messages to the stream directly (SAPI's own internal stream — no mocking needed); control merge-vs-new through the seeded `published_at` values; verify results in the `agg:event:*` keys and `XPENDING`.
**Meanwhile:** active Events live only in Redis — never flushed, and a replaced Event's merges are overwritten. Acceptable until Aggregation: Flush Active Events & Publish, since nothing reads aggregation output before then.
**Config note:** `AGG_MAX_CONCURRENT` must exceed the peak number of distinct groups live within any `AGG_GROUP_IDLE_TIMEOUT_S` window — a live group holds its permit until it idles out, so running out of permits stalls the consumer loop and, once flushing exists, the flush triggers too (design.md §4.3, known limitation).
**Docs:** impl.md §4.1, §6.2, §10.1 (`fetch_entity_event`), §12.1, §12.2, §12.3, §12.5, §12.6, §16; design.md §4.2–§4.4, §4.7, §11.2.
**Explicitly deferred:** flush and every DB write; `fetch_mapped_event_id()` (the DB half of the duplicate check); flush-before-replace; reclaim/dead-letter and the `v` check; layer monitoring.

---

### Aggregation: Flush Active Events & Publish (M)

**Status:** Created
**Depends on:** Aggregation: Consume & Merge into Active Events (for `AggregationService`, `EventAggregationStore`, the consumer loop)
**Scope:** Persist active Events to `events`/`event_entity_map` and publish `stream:event_aggregated` for the ones that changed (impl.md §10.2, §12.1 `flush()`/`_persist_and_publish()`, §12.2 flush triggers, §12.5 `scan_all()`; design.md §4.5).
- `app/redis/state_client.py`: `scan()` (§6.2).
- `app/db/connection.py`: `DatabaseClient.execute()`, including on the transaction connection (§5.1).
- `alembic/`: `002_create_events.py` (§17), including the `published_aggregation_updated_at` outbox column.
- `app/models/event_entity_map.py`: `EventEntityMap` (§3.4) — the Event↔EntityEvent relationship this ticket persists.
- `app/persistent/event_repository.py`: `EventRepository` + singleton — `persist_events()` (conditional UPSERT, returns only rows written), `_row_to_event()` without `brief_summary` (`Event` has no such field yet; the column stays NULL until Scoring writes it), `fetch_unpublished_events()`, `mark_published()`, `fetch_mapped_event_id()` (§10.2).
- `EventAggregationStore.scan_all()` (§12.5).
- `AggregationService`: `flush()`, `_persist_and_publish()`, the DB half of the duplicate check, and flush-before-replace in `_process()` (§12.1).
- `AggregationConsumer`: the queue-empty and `AGG_DB_FLUSH_INTERVAL_S` flush triggers, each wrapped in `except Exception` → `aggregation_flush_failed`, and `start()`'s `finally: flush()` (§12.2).
- `app/main.py`: `create_event_repository()` (§16).
**Acceptance criteria** (per impl.md §10.2, §12.1, §12.2):
- One transaction writes each Event's `events` UPSERT and its `event_entity_map` rows; re-flushing the same state is idempotent and preserves `created_at`.
- `source_list` and `event_type_secondary` are computed per §10.2, including the most-recent-`published_at` tie-break.
- Only Events actually written — new, or with an `aggregation_updated_at` newer than the stored row — publish `stream:event_aggregated` (`event_id`, `v="1"`); an unchanged active Event publishes nothing on later flushes.
- A replaced Event is persisted and published before its Redis key is overwritten; a flush holding an older snapshot of that Event doesn't overwrite it.
- Outbox: a crash between commit and publish leaves the Event pending, and the first flush after restart publishes it; a publish that fails while running is retried by the next flush; the outbox read covers only the Events being persisted, never the whole table; a published version is marked, and an older mark never un-marks a newer one.
- An `entity_event_id` already in `event_entity_map` is skipped as a duplicate.
- After the first flush following startup (which sends every active Event), a flush sends only Events whose `aggregation_updated_at` moved since this process last flushed them; a failed flush re-sends the same Events next time.
- A flush failure rolls back and re-raises; the consumer loop logs `aggregation_flush_failed` and keeps running, and the next flush retries from the same Redis state.
- Cancelling the consumer's Task still runs one final flush.
- Unit-tested against mocked asyncpg/redis.
- Integration pass against real Postgres and Redis: drive Events through the consumer, then check the `events`/`event_entity_map` rows and the `stream:event_aggregated` messages across repeated flushes.
- The integration cases from Aggregation: Consume & Merge into Active Events re-run: the same Redis-side results, now with a replaced Event persisted in `events` before its key is overwritten, and a failed flush logging WARNING `aggregation_flush_failed` with the first message still pending.
- Each flush logs INFO `aggregation_flush_completed` with `events_persisted`, `events_published` and `latency_ms` (impl.md §12.1).
**Config note:** `AGG_BATCH_TIMEOUT_MS` and `AGG_DB_FLUSH_INTERVAL_S` must stay far below `SLIDING_WINDOW_HOURS` (design.md §4.5) — an active Event must always be flushed before its Redis TTL can expire it.
**Docs:** impl.md §5.1, §6.2, §10.2, §12.1, §12.2, §12.5, §12.6, §16, §17 (`002`); design.md §4.5, §10.2, §10.4, §13.2 (outbox long-downtime gap).
**Explicitly deferred:** `Event.brief_summary`, `BriefSummary`, and `_row_to_event()`'s `brief_summary` parsing (added by the first Scoring ticket that writes it); reclaim/dead-letter and the `v` check; layer monitoring; Scoring's consumption of `stream:event_aggregated`.

---

### Aggregation: Reclaim, Dead Letter & Schema-Version Check (S)

**Status:** Created
**Depends on:** Aggregation: Flush Active Events & Publish (for `start()`'s `finally: flush()`); Entity Analysis: Reclaim, Dead Letter & Schema-Version Check (for `autoclaim()`, `ReclaimedMessage`, `run_supervised()`)
**Scope:** Recover messages Aggregation leaves un-ACKed, and dead-letter the ones that keep failing or are malformed (missing or unrecognised `v`, missing or invalid `entity_event_id`) (impl.md §6.1's Reclaim and Dead Letter pattern, §12.2).
- `app/config.py`: `AggConfig.max_retry`.
- `AggregationConsumer`: `start()` runs `_consumer_loop()` and `_reclaim_loop()` through `run_supervised()`; `_reclaim_loop()` dead-letters to `stream:aggregation_dead_letter` at `AGG_MAX_RETRY` and otherwise calls the same `_route_one()`; each sweep wrapped in `except Exception` → `aggregation_reclaim_sweep_failed`.
- The message shape check (`v` and `entity_event_id`) immediately after `read_from()`/`autoclaim()`, each message handled in isolation (§6.1's shape-check callout).
**Acceptance criteria** (per impl.md §6.1, §12.2):
- A message left un-ACKed by a failure is reclaimed after `STREAM_CLAIM_TIMEOUT_MS` and processed again through `_route_one()`.
- At `delivery_count >= AGG_MAX_RETRY`, the message is dead-lettered with §6.1's payload and ACKed, without being processed.
- A message whose `v` is missing or unrecognised, or whose `entity_event_id` is missing or not a valid UUID, on either path, is dead-lettered (`delivery_count="1"` on the `read_from()` path) and ACKed; every other message in its batch is still processed normally.
- A failing sweep logs a warning and the loop continues; a cancelled Task still runs the final flush.
- Unit-tested against mocked Redis.
- Integration pass against real Redis/Postgres: a message that fails once is retried and merged; one that keeps failing reaches the dead-letter stream.
**Docs:** impl.md §4.1, §6.1, §12.2; design.md §12.1.
**Explicitly deferred:** layer monitoring; dead-letter consumption (Q-3, Post-MVP).

---

### Aggregation: Layer Monitoring & Health (XS)

**Status:** Created
**Depends on:** Aggregation: Reclaim, Dead Letter & Schema-Version Check (for `run_supervised()` in `start()`); Entity Analysis: Layer Monitoring & Health (for `_make_monitored()`, `get_layer_tasks`)
**Scope:** Register the Aggregation layer's Task for auto-restart and report its real liveness in `GET /v1/health` (impl.md §15.2, §16).
- `app/main.py`: register `"aggregation"` with `_make_monitored()`.
- `app/api/routes/health.py`: `layers.aggregation` reflects whether its Task is running.
**Acceptance criteria** (per impl.md §15.2, §16):
- A crash in `AggregationConsumer.start()` logs `layer_task_crashed` and restarts the Task on the same consumer instance; a cancelled Task is never restarted.
- `layers.aggregation` flips between `ok` and `error` with the Task's state. With `database`/`redis_stream`/`redis_state`/`hk_stock_list` all `ok`, overall status is `degraded`, `HTTP 200`, since the Scoring and Cache layers don't exist yet.
- The Health Check ticket's integration cases re-run with `layers.aggregation` real: with everything up, `degraded`, `HTTP 200`, with `layers.entity_analysis`/`layers.aggregation` `ok` and the other two `error`. Each Postgres/Redis stop or pause gives `unhealthy`, `HTTP 503`, with only the failed field `error` and both real layers still `ok`. Recovery returns to `degraded`, `HTTP 200`, without restarting the app.
- Unit-tested with a fake consumer whose `start()` raises.
**Docs:** impl.md §15.2, §16; design.md §7.1, §12.1.
**Explicitly deferred:** the Scoring and Cache layers' registration.

---

### Aggregation: Post-Release Tuning Validation (S)

**Status:** Created
**Depends on:** every Aggregation ticket above; Deployment: SAPI Container, Compose & Production-like Run (below — the real-traffic run), run for an agreed length.
**Scope:** Answer the Aggregation tuning questions that need real volume, from production logs and Redis/stream metrics — no new instrumentation.
- **Q-1 (Aggregation's share):** `AGG_BATCH_SIZE`/`AGG_BATCH_TIMEOUT_MS`/`AGG_DB_FLUSH_INTERVAL_S`, from throughput, flush frequency, and flush duration.
- **Group permits:** peak live groups vs. `AGG_MAX_CONCURRENT`, and how long the consumer loop waits for a permit (design.md §4.3's known limitation) — sizes `AGG_MAX_CONCURRENT`/`AGG_GROUP_IDLE_TIMEOUT_S`, and shows whether the proper fix is needed sooner.
- **Late EntityEvents:** how often `aggregation_late_entity_event_skipped` fires, and from which sources — decides whether merging them into their older, already-flushed Event is worth building (design.md §4.4).
- **`STREAM_CLAIM_TIMEOUT_MS` vs. group backlog** (impl.md §12.2's tuning note): busiest group's backlog depth × per-item time, against false reclaims.
**Acceptance criteria:**
- Each item has measured numbers and a decision: keep the default, or change it with the reason.
- Changed defaults are applied in `app/config.py` and impl.md §4.1.
- Q-1 is marked resolved in impl.md §18 and design.md §14 once every layer's share is answered.
**Docs:** impl.md §4.1, §12.2, §18; design.md §4.3 (group permits), §4.4 (late EntityEvents), §4.5, §14.
**Explicitly deferred:** the Scoring and Cache layers' shares of Q-1.
**PM note:** run length to be set; can share the production run with Entity Analysis: Post-Release Tuning Validation.

---

### Spike: Scoring LLM Skills (S)

**Status:** Created
**Depends on:** Spike: Entity Analysis LLM Skill (for the pinned `google-genai`/`instructor` versions and the confirmed `generate_structured()` behaviour)
**Scope:** Throwaway scripts in `poc/sapi/` against live Vertex AI, finishing the two Scoring prompts and sanity-checking their starting values before the Skill tickets code against them — no production code.
- **Few-shot examples:** write `BriefSummarySkill`'s 3 examples (financial data, regulatory without numbers, conflicting signals) and `EventLLMScoringSkill`'s 5 (earnings beat, implicit negative guidance, conflicting signals, neutral boilerplate, regulatory investigation) — impl.md §8.3/§8.4 name them but don't contain them.
- **Prompt sanity:** both prompts end to end on realistic aggregated Events (multi-source, single-source, English-source, no-numbers), built by hand from Entity Analysis spike output: Traditional Chinese output, length limits, no invented numbers, `stock_impact_score` direction and spread.
- **Initial-value sanity:** the 200-character input cap against real multi-article Events (Q-8); `output_tokens` against each Skill's max tokens (I-3); whether BriefSummarySkill's temperature 0.1 changes outputs noticeably run to run.
**Acceptance criteria:**
- Every item above has a written answer with evidence (raw responses printed, or measured numbers).
- The few-shot examples are written into impl.md §8.3/§8.4, ready to copy verbatim, and approved.
- Any design-changing answer is written back into impl.md (§8.3/§8.4, §8's table) as a fix and approved before the Skill tickets start.
**Docs:** impl.md §7.1, §8, §8.3, §8.4, §18 (I-3); design.md §9.5, §9.6, §14 (Q-8).
**Explicitly deferred:** calibrating Q-8 and I-3 at volume (Scoring: Post-Release Tuning Validation).
**PM note:** time-box length to be set.

---

### Scoring: Rule Score & Sentiment Aggregation (S)

**Status:** Created
**Depends on:** Aggregation: Sliding-Window Merge Algorithm (for `Event`, `SourceListItem`); Entity Analysis: LLM Entity Extraction (for `EntityEvent`/`AnalysisOutcome`, `VersionedMetadata`)
**Scope:** The pure rule-based half of an Event's score (impl.md §13.3; design rationale: design.md §5.6, §5.8).
- `app/config.py`: `EventTypeScore`, `ScoringConfig.event_type_score` (one `EVENT_TYPE_WEIGHT_*` env var per event type, assembled by `load_config()`, §4.1).
- `app/models/event_score.py`: `RuleScore` with its `base_rule_score` property only — not `LLMScore`/`EventScore`.
- `app/scoring/rule_score.py`: `RULE_SCORE_VERSION`, `compute_rule_score()` (taking a precomputed `SentimentAggregation`), `compute_sentiment_aggregation()`, `SentimentAggregation`.
**Acceptance criteria** (per impl.md §13.3):
- Each of the eight event types maps to its configured score.
- `source_authority_score` is the MAX weight over the Event's sources; a source missing from `source_weights` counts as 0.0.
- `sentiment_strength_score` is the source-weighted mean `sentiment_score` scaled ×10 into `[0,10]`; `weighted_signed_score` uses +1/0/−1 by label; zero total weight gives `(0.0, 0.0)`.
- `source_heat_score` = `min(10, source_count / total_active_sources × 10)`.
- `base_rule_score` is the unnormalised 0.30/0.25/0.15/0.10 weighted sum, in `[0,8]`.
- The result is `VersionedMetadata(version=RULE_SCORE_VERSION, output=RuleScore)`.
- Unit-tested with hand-built Events and EntityEvents. No integration pass — pure functions with no I/O.
**Docs:** impl.md §4.1 (`EventTypeScore`, `event_type_score`), §13.1 (`RuleScore`), §13.3; design.md §5.6, §5.8.
**Explicitly deferred:** fetching source weights and the active-source count (Scoring: Source Weights via Admin); Score Fusion.

---

### Scoring: Source Weights via Admin (M)

**Status:** Created
**Depends on:** Startup & Health: Fetch & Cache HK Stock List (for the shared `httpx` client); Aggregation: Consume & Merge into Active Events (for `RedisStateClient.hset()`/`expire()`)
**Scope:** Read each source's authority weight and the active-source count from `source:config`, refreshing it from Admin on a cold key, with tier-default fallbacks (impl.md §9.2, §13.5; design.md §1.5).
- `app/config.py`: `ScoringConfig.source_config_ttl_s`/`admin_api_url`/`total_active_sources_default`.
- `app/clients/exceptions.py`: `AdminClientException`.
- `app/clients/admin_client.py`: `AdminClient.fetch_sources()` + `create_admin_client()`/`get_admin_client()` (§9.2).
- `app/redis/state_client.py`: `hmget()`/`hlen()` (§6.2) — not `exists()`, which nothing needs.
- `app/scoring/rule_score.py`: `get_source_weights()`, `count_active_sources()`, `_fallback_weights()`, `_SOURCE_TIER`/`_DEFAULT_TIER_WEIGHTS` (§13.5).
- `app/main.py`: `create_admin_client()` (§16).
**Acceptance criteria** (per impl.md §9.2, §13.5):
- A warm `source:config` is served with a single `HMGET`, without calling Admin.
- A cold key — or one that expires between calls — returns nothing from `HMGET`, so Admin is called once, every source is cached with `SOURCE_CONFIG_TTL_S`, and the requested weights are served; it never returns an empty result while Admin is reachable.
- Admin failing (`AdminClientException`) or returning an empty list falls back to P1/P2/P3 tier weights, caches nothing, and (empty list) logs `source_config_empty_from_admin`; an unrecognised `source_name` weighs 0.0.
- Any other exception (e.g. a malformed response) propagates rather than being treated as an Admin outage.
- `count_active_sources()` never calls Admin: `HLEN` when warm, `TOTAL_ACTIVE_SOURCES_DEFAULT` when cold.
- A state-Redis failure propagates from `get_source_weights()` and `count_active_sources()` — no fallback weights, no Admin call — so the caller doesn't ACK and the message is retried (impl.md §13.6).
- `AdminClient` never leaks a raw `httpx` exception.
- Unit-tested against mocked `httpx`/Redis.
- Integration pass against real Redis, with Admin's `GET /v1/sources` (api.md §4.3) mocked — success, empty list, and failure.
**Docs:** impl.md §4.1, §6.2, §9.2, §13.5, §16; api.md §4.3; design.md §1.5, §5.6.
**Explicitly deferred:** the callers (Scoring: Score & Persist Events).

---

### Scoring: Brief Summary Skill (M)

**Status:** Created
**Depends on:** Spike: Scoring LLM Skills (for the few-shot examples and any §8.3 fixes); Entity Analysis: LLM Entity Extraction (for `SkillBase`, the LLM Adapter with `GenerationConfig`); Aggregation: Sliding-Window Merge Algorithm (for `Event`)
**Scope:** Generate an Event's Traditional Chinese summary from its constituent EntityEvents, with an extractive fallback on a schema violation (impl.md §8.3; design rationale: design.md §9.5, §5.9).
- `app/models/event.py`: `BriefSummary` (§3.3) — not yet `Event.brief_summary` (added by its writer, Scoring: Score & Persist Events).
- `app/skills/brief_summary_skill.py`: `BriefSummaryInput` (including `source_weights`), `BriefSummaryResult`, `BriefSummarySkillLog`, `InputSummary`, `BRIEF_SKILL_VERSION`, `_GENERATION_CONFIG` (temperature 0.1, 4096), `build_prompt()` with the verbatim prompt and the spike's few-shot examples, `llm_process()`, business-constraint validation, `fallback_output()`.
**Acceptance criteria** (per impl.md §8.3):
- Input construction: at most 3 articles per source (latest kept); ordered by `source_weights` descending, then latest `published_at`; entries that would exceed 200 characters dropped whole; `input_summary` records counts and both truncation flags.
- Business constraints: `summary_short` ≤30, `summary_full` ≤150, `key_numbers` ≤3 — truncated and logged, not failed.
- `LLMSchemaViolationException` → fallback (highest-weight constituent's `headline`, ties by latest `published_at`; `summary_full = summary_short`; `key_numbers = []`), `skill_log.fallback=True`, `brief_summary_fallback` logged, `failure_reason=None`.
- Rate-limit/provider/auth failures → `failure_reason=type(err)`, `output=None`.
- Every call passes `_GENERATION_CONFIG`.
- Unit-tested with a fake `LLMAdapter`.
- Integration pass against live Vertex AI with the spike's Events.
**Docs:** impl.md §3.3 (`BriefSummary`), §7.1, §8, §8.1, §8.3, §8.5, §8.6; design.md §5.9, §9.5, §9.7, §9.8.
**Explicitly deferred:** calling the Skill, `Event.brief_summary`, and persisting the summary (Scoring: Score & Persist Events).

---

### Scoring: Event LLM Scoring Skill (S)

**Status:** Created
**Depends on:** Scoring: Brief Summary Skill (for `BriefSummary`); Spike: Scoring LLM Skills (for the few-shot examples)
**Scope:** Score an Event's directional stock impact from BriefSummarySkill's output, with a neutral fallback (impl.md §8.4; design rationale: design.md §9.6, §5.9).
- `app/models/event_score.py`: `LLMScore` (§13.1).
- `app/skills/event_scoring_skill.py`: `EventLLMScoringInput`, `EventLLMScoringResult`, `EventLLMScoringSkillLog`, `SCORING_SKILL_VERSION`, `_GENERATION_CONFIG` (temperature 0, 2048), `build_prompt()` with the verbatim prompt and the spike's few-shot examples, `llm_process()`, business-constraint validation, `fallback_output()`.
**Acceptance criteria** (per impl.md §8.4):
- `stock_impact_score` clipped to `[-5, +5]` and `adjustment_reason` truncated to 80, both logged.
- An empty `adjustment_reason`, or `LLMSchemaViolationException`, → fallback (`stock_impact_score=0`, `adjustment_reason=""`), `skill_log.fallback=True` with the right `fallback_reason`, `event_scoring_fallback` logged, `failure_reason=None`.
- `input_from_fallback` is recorded in the skill log, not used in the prompt.
- Rate-limit/provider/auth failures → `failure_reason=type(err)`, `output=None`.
- Unit-tested with a fake `LLMAdapter`.
- Integration pass against live Vertex AI with the spike's Events.
**Docs:** impl.md §7.1, §8, §8.1, §8.4, §8.5, §8.6, §13.1 (`LLMScore`); design.md §5.9, §9.6, §9.7, §9.8.
**Explicitly deferred:** calling the Skill and direction derivation (Scoring: Score & Persist Events).

---

### Scoring: Score & Persist Events (L)

**Status:** Created
**Depends on:** Scoring: Rule Score & Sentiment Aggregation; Scoring: Source Weights via Admin; Scoring: Event LLM Scoring Skill (and through it Brief Summary Skill); Aggregation: Flush Active Events & Publish (for `EventRepository`, `stream:event_aggregated`, migration `002`)
**Scope:** Consume `stream:event_aggregated`, score each Event, write `event_scores`, and publish `stream:event_scored` (impl.md §13.1, §13.2, §13.4, §10.3; design.md §5.2–§5.5, §5.7).
- `app/config.py`: `ScoringConfig.batch_size`/`batch_timeout_ms`/`consumer_name`.
- `app/models/event.py`: `Event.brief_summary`, `brief_summary_aggregation_updated_at`, `brief_summary_fallback`; `_row_to_event()` parses them (§3.3, §10.2).
- `app/models/event_score.py`: `EventScore` (§13.1).
- `alembic/`: `003_create_event_scores.py` (§17).
- `app/persistent/event_repository.py`: `fetch_event()`, `fetch_aggregation_updated_at()`, `update_brief_summary()` (writing the summary's Event version and fallback flag into the JSONB) (§10.2).
- `app/persistent/entity_event_repository.py`: `fetch_entity_events_for_event()` (§10.1).
- `app/persistent/event_score_repository.py`: `EventScoreRepository` + singleton — `persist_score_if_not_stale()` (writing `scored_aggregation_updated_at`), `fetch_scored_aggregation_updated_at()`, `_row_to_event_score()` (§10.3); not `fetch_scores()`.
- `app/scoring/scoring_service.py`: `ScoringService` + singleton — `score_event()` (steps 1–10, source weights read before the Skill starts), `_score_fusion()`, `_is_stale()` (§13.1, §13.4).
- `app/scoring/scoring_consumer.py`: `ScoringConsumer` with one consumer loop calling `_process_one()` directly — `_in_process` guard and the three-tier `try`/`except` (§13.2).
- `app/main.py`: `create_event_score_repository()`, `create_scoring_service()`, the consumer started as a plain Task in `layer_tasks` (§16).
**Acceptance criteria** (per impl.md §13.1, §13.2, §13.4, §13.6):
- A fresh Event is scored end to end: brief summary written, one `event_scores` row with the fused fields and both versioned details, `stream:event_scored` published (`event_id`, `v="1"`), message ACKed.
- A re-score updates the same row, keeping `score_id` and moving `scored_at`/`scored_aggregation_updated_at` — an Event never has more than one `event_scores` row.
- If step 3 fails (e.g. `count_active_sources()`'s Redis read) while BriefSummarySkill runs, the summary is still awaited and stored before the original exception is re-raised (a failed store logs `brief_summary_salvage_failed`); on cancellation the Skill's Task is cancelled instead.
- A retry after a failure past BriefSummarySkill (e.g. EventScoringSkill's LLM call) reuses the stored summary for the same version: no BriefSummarySkill call, `brief_summary_reused` logged, and EventScoringSkill gets the stored fallback flag as `input_from_fallback`.
- A message for a version already scored (`scored_aggregation_updated_at` equals the Event's `aggregation_updated_at`) makes no Skill call and no write: it re-publishes `stream:event_scored`, logs `scoring_already_scored`, and ACKs.
- Direction is `sign(stock_impact_score)`, or the sign of `weighted_signed_score` whenever it's 0 (fallback or a genuine 0); `abs_final_score` per `_score_fusion()`; `SentimentAggregation` is computed once and feeds both Rule Score and Score Fusion; `llm_fallback` is persisted from the EventScoringSkill result.
- Re-aggregation mid-scoring: each checkpoint returns early (no EventScoringSkill call after the first, no write after the second, a no-op UPSERT at the third), nothing published, ACKed.
- A missing Event logs `scoring_event_missing` and ACKs.
- A Skill's rate-limit/provider/auth failure logs its completion event, leaves the message un-ACKed, and the loop continues; a schema violation continues on the fallback, and the score records `llm_fallback`.
- An Event already in `_in_process` is skipped and left un-ACKed; any other exception logs `scoring_process_one_crashed` and leaves it un-ACKed.
- Unit-tested with fake Skills/repositories.
- Integration pass against real Postgres and Redis with faked Skills: seed an Event via Aggregation's tables, publish `stream:event_aggregated` directly, check `event_scores` and `stream:event_scored`; plus one run with live Skills.
**Docs:** impl.md §3.3, §10.1, §10.2, §10.3, §13.1, §13.2, §13.4, §13.6, §16, §17 (`003`); design.md §5.2–§5.5, §5.7, §5.8, §10.3.
**Explicitly deferred:** reclaim/dead-letter and the `v` check; `WorkerPool`/backoff; layer monitoring; `fetch_scores()` and the Cache Layer.

---

### Scoring: Reclaim, Dead Letter & Schema-Version Check (S)

**Status:** Created
**Depends on:** Scoring: Score & Persist Events; Entity Analysis: Reclaim, Dead Letter & Schema-Version Check (for `autoclaim()`, `ReclaimedMessage`, `run_supervised()`)
**Scope:** Recover messages Scoring leaves un-ACKed, and dead-letter the ones that keep failing or are malformed (missing or unrecognised `v`, missing or invalid `event_id`) (impl.md §6.1's Reclaim and Dead Letter pattern, §13.2).
- `app/config.py`: `ScoringConfig.max_retry`.
- `ScoringConsumer`: `start()` runs both loops through `run_supervised()`; `_reclaim_loop()` dead-letters to `stream:scoring_dead_letter` at `SCORING_MAX_RETRY` and otherwise calls the same `_process_one()`; each sweep wrapped in `except Exception` → `scoring_reclaim_sweep_failed`.
- The message shape check (`v` and `event_id`) immediately after `read_from()`/`autoclaim()`, each message handled in isolation (§6.1).
**Acceptance criteria** (per impl.md §6.1, §13.2):
- An un-ACKed message (LLM failure, skipped in-flight duplicate) is reclaimed after `STREAM_CLAIM_TIMEOUT_MS` and scored.
- At `delivery_count >= SCORING_MAX_RETRY`, dead-lettered with §6.1's payload and ACKed, unprocessed.
- A message whose `v` is missing or unrecognised, or whose `event_id` is missing or not a valid UUID, on either path, is dead-lettered (`delivery_count="1"` on the `read_from()` path) and ACKed; every other message in its batch is still processed normally.
- A failing sweep logs a warning and the loop continues.
- Unit-tested against mocked Redis.
- Integration pass against real Redis/Postgres: a message that fails once is retried and scored; one that keeps failing reaches the dead-letter stream.
**Docs:** impl.md §4.1, §6.1, §13.2; design.md §12.1.
**Explicitly deferred:** `WorkerPool`; layer monitoring; dead-letter consumption (Q-3, Post-MVP).

---

### Scoring: Concurrency & Rate-Limit Backoff (S)

**Status:** Created
**Depends on:** Scoring: Reclaim, Dead Letter & Schema-Version Check (both loops dispatch through `WorkerPool`); Entity Analysis: Concurrency & Rate-Limit Backoff (for `WorkerPool`)
**Scope:** Score Events concurrently, bounded by `SCORING_MAX_CONCURRENT`, and pause the layer for `RATE_LIMIT_BACKOFF_S` on a Vertex AI rate limit (impl.md §11.3 reused, §13.2; design.md §12.1).
- `app/config.py`: `ScoringConfig.max_concurrent`.
- `app/main.py`: a second `WorkerPool` instance for Scoring (§16).
- `ScoringConsumer`: takes the pool; both loops gather their batch's dispatches; `except LLMRateLimitException` calls `trigger_backoff()`.
**Acceptance criteria** (per impl.md §11.3, §13.2):
- No more than `SCORING_MAX_CONCURRENT` Events are scored at once, independent of Entity Analysis's pool.
- A rate limit from any Event pauses new Scoring work for `RATE_LIMIT_BACKOFF_S` without pausing Entity Analysis; the message stays un-ACKed.
- Unit-tested with fake coroutines and a controllable clock.
- Integration pass against live Vertex AI at the configured concurrency.
- Each backoff logs WARNING `rate_limit_backoff_started` with `layer` and `backoff_until`, and each gathered batch logs INFO `scoring_batch_completed` with `batch_size` and `latency_ms` (impl.md §11.3).
**Config note:** `RATE_LIMIT_BACKOFF_S * 1000` must stay below `STREAM_CLAIM_TIMEOUT_MS` (§11.3), as for Entity Analysis.
**Docs:** impl.md §4.1, §11.3, §13.2, §16; design.md §12.1.

---

### Scoring: Layer Monitoring & Health (XS)

**Status:** Created
**Depends on:** Scoring: Reclaim, Dead Letter & Schema-Version Check (for `run_supervised()` in `start()`); Entity Analysis: Layer Monitoring & Health (for `_make_monitored()`, `get_layer_tasks`); Aggregation: Layer Monitoring & Health (so `layers.aggregation` is already real)
**Scope:** Register the Scoring layer's Task for auto-restart and report its real liveness in `GET /v1/health` (impl.md §15.2, §16).
- `app/main.py`: register `"scoring"` with `_make_monitored()`.
- `app/api/routes/health.py`: `layers.scoring` reflects whether its Task is running.
**Acceptance criteria** (per impl.md §15.2, §16):
- A crash in `ScoringConsumer.start()` logs `layer_task_crashed` and restarts the Task on the same consumer instance; a cancelled Task is never restarted.
- `layers.scoring` flips between `ok` and `error` with the Task's state. With `database`/`redis_stream`/`redis_state`/`hk_stock_list` all `ok`, overall status is `degraded`, `HTTP 200`, since the Cache layer doesn't exist yet.
- The Health Check ticket's integration cases re-run with `layers.scoring` real: with everything up, `degraded`, `HTTP 200`, with three real layers `ok` and `layers.cache` `error`; each Postgres/Redis stop or pause gives `unhealthy`, `HTTP 503`, with only the failed field `error`; recovery returns to `degraded`, `HTTP 200`, without restarting the app.
- Unit-tested with a fake consumer whose `start()` raises.
**Docs:** impl.md §15.2, §16; design.md §7.1, §12.1.
**Explicitly deferred:** the Cache layer's registration.

---

### Scoring: Post-Release Tuning Validation (S)

**Status:** Created
**Depends on:** every Scoring ticket above; Deployment: SAPI Container, Compose & Production-like Run (below — the real-traffic run), run for an agreed length.
**Scope:** Answer the Scoring tuning questions that need real volume, from the logs the earlier tickets produce (`brief_summary_completed`, `event_scoring_completed`, `llm_calls[]`) and `event_scores` — no new instrumentation.
- **Q-2:** `SCORING_MAX_CONCURRENT`, from 429 frequency and throughput.
- **Q-8:** BriefSummarySkill's 200-character input cap, from `input_summary` truncation rates and summary quality on a hand-checked sample.
- **Q-15:** `source_heat_score`'s `source_count / TOTAL_ACTIVE_SOURCES` formula, from real cross-source corroboration rates.
- **Q-1, Q-9, Q-11 (Scoring's shares):** `SCORING_BATCH_SIZE`/`SCORING_BATCH_TIMEOUT_MS`, `RATE_LIMIT_BACKOFF_S`, and `LLM_API_TIMEOUT_S` for the two Scoring Skills.
- **I-3 (Scoring's share):** both Skills' max tokens against `output_tokens` p95/p99 and any truncation.
- **Fallback rates:** `brief_summary_fallback`/`event_scoring_fallback` frequency — a high rate points at a prompt or schema problem.
**Acceptance criteria:**
- Each item has measured numbers and a decision: keep the default, or change it with the reason.
- Changed defaults are applied in `app/config.py` and impl.md (§4.1, §8's table).
- Answered items are marked resolved in impl.md §18 and design.md §14 (Q-1/Q-9/Q-11 once every layer's share is answered).
**Docs:** impl.md §4.1, §8, §18; design.md §5.6, §14.
**Explicitly deferred:** the Cache layer's share of Q-1.
**PM note:** run length to be set; can share the production run with the other layers' validation tickets.

---

### Cache: Recency & Merge (S)

**Status:** Created
**Depends on:** Scoring: Score & Persist Events (for `EventScore`, `Event.brief_summary`)
**Scope:** The pure half of building the Morning Brief cache — the recency formula, the ranking order, and turning scored Events into cache records merged against the current cache (impl.md §14.1 types, §14.3, §14.4, §14.5 `_to_cache_record()`; design rationale: design.md §6.3, §6.4, §6.6).
- `app/config.py`: `CacheConfig.noise_filter_threshold`/`recency_decay_lambda`.
- `app/cache/recency.py`: `apply_recency()`, `ranking_key()` (§14.3).
- `app/cache/cache_service.py`: `CacheEventDetail`, `CacheLLMScoreDetail`, `CacheScoreDetail`, `CacheRecord` (with `to_dict()`/`from_dict()`), `Candidate`, `_build_candidate_records()`, `_incremental_merge()`, `_to_cache_record()` — not `CacheService` itself.
**Acceptance criteria** (per impl.md §14.1, §14.3–§14.5):
- `apply_recency()` matches the §14.3 formula: recency decays with hours since `aggregation_updated_at`, its 0.20 share is added to the stored base score, `direction` is applied, then fused with `stock_impact_score`.
- `ranking_key()` orders by `abs_final_score`, then `|base_rule_score|`, then `last_seen_at`, then `first_seen_at`.
- Merge: the four §14.4 rows (replace / insert / remove / ignore), decided on the **stored** `abs_final_score`; a cached Event not in this batch keeps its place whatever its recency.
- `_to_cache_record()` fills the display fields and the three internal fields; `llm_score_detail` carries `adjustment_reason`, `score_version`, `scored_at`.
- `CacheRecord.to_dict()`/`from_dict()` round-trip losslessly.
- Unit-tested with fixed timestamps. No integration pass — pure functions with no I/O.
**Docs:** impl.md §4.1, §14.1, §14.3, §14.4, §14.5; design.md §6.3, §6.4, §6.6.
**Explicitly deferred:** reading and writing the cache, the build itself, the API.

---

### Cache: Versioned Cache Store (S)

**Status:** Created
**Depends on:** Aggregation: Consume & Merge into Active Events (for `RedisStateClient`)
**Scope:** Store Morning Brief versions in Redis and switch between them atomically (impl.md §10.4; design.md §6.5, §11.4).
- `app/redis/state_client.py`: `get()`, `mget()`, `incr()`, `multi_exec()` (§6.2).
- `app/persistent/cache_store.py`: `CacheStore` + `create_cache_store()`/`get_cache_store()` — `CachePointer`, `get_current_version()`, `get_current_pointer()`, `incr_version_counter()`, `write_cache_version()`, `read_cache_version()`, and the Redis-error translation (`_read()`, and in `get_current_pointer()`); not `scan_orphaned_versions()`.
- `app/main.py`: `create_cache_store()` (§16).
**Acceptance criteria** (per impl.md §10.4):
- `write_cache_version()` writes the new version, moves the pointer, deletes the old version (none on a first build), and sets `last_updated` in one `MULTI/EXEC` — a reader never sees the pointer on a missing version.
- `incr_version_counter()` only ever increases, across restarts.
- `get_current_version()`/`get_current_pointer()` return `None` before the first build.
- `get_current_pointer()` returns the version and its `last_updated` from one `MGET` — never a version paired with another build's timestamp.
- A Redis connection or timeout failure on any read raises `ServiceUnavailableException`, not a raw `redis` exception.
- Unit-tested against mocked Redis.
- Integration pass against real Redis: atomic switch, old version gone, counter persistence.
**Docs:** impl.md §6.2, §10.4, §15.1; design.md §6.5, §11.4.
**Explicitly deferred:** `scan_orphaned_versions()` (Cache: Orphan Cleanup).

---

### Cache: Build Cache from Scores (M)

**Status:** Created
**Depends on:** Cache: Recency & Merge; Cache: Versioned Cache Store; Scoring: Score & Persist Events (for `stream:event_scored`, `event_scores`, `EventScoreRepository`)
**Scope:** Consume `stream:event_scored` and build a new cache version per batch, incrementally (impl.md §14.1 `build_new_version()`, §14.2; design.md §6.1, §6.2).
- `app/config.py`: `CacheConfig.batch_timeout_ms`/`consumer_name` — no batch-size setting (§14.2).
- `app/persistent/event_score_repository.py`: `fetch_scores(event_ids)` — the by-id branch only (§10.3).
- `app/persistent/event_repository.py`: `fetch_events()` (§10.2).
- `app/cache/cache_service.py`: `CacheService` + singleton — `build_new_version()`, `_refresh_cached_records()`, `_write_new_version()` (UTF-8 JSON via `ensure_ascii=False`; calls `CacheReader.prime()` after a successful write) (§14.1, §14.5).
- `app/cache/cache_reader.py`: `CacheReader` + `create_cache_reader()`/`get_cache_reader()` — `prime()` and `get_records()` (§14.8); `main.py` creates it after `CacheStore`.
- `app/cache/cache_consumer.py`: `CacheConsumer` with a single serial loop — `read_from()` reading everything pending (no `COUNT`), coalesced until `CACHE_BATCH_TIMEOUT_MS` after the first message, `build_new_version()` in `try`/`except` (`cache_build_failed`), batch ACK; no reclaim yet (§14.2).
- `app/main.py`: `create_cache_service()`, the consumer started as a plain Task in `layer_tasks` (§16).
**Acceptance criteria** (per impl.md §14.1, §14.2, §14.7):
- A batch of new scores produces exactly one new version, sorted by `ranking_key()`, with the batch merged per §14.4 and every other cached Event's recency refreshed from its stored inputs — no DB read beyond the batch.
- The batch is ACKed only after the version write succeeds.
- A failed build (DB or Redis) logs `cache_build_failed`, leaves the batch un-ACKed, writes no version, and the loop continues; the layer Task doesn't crash.
- Builds never run concurrently.
- The stored version is UTF-8 JSON (Chinese text not `\u`-escaped), and after each successful write `CacheReader` holds that version's records, so the API needs no parse; a failed write primes nothing.
- A trickle of single messages is coalesced: one build per `CACHE_BATCH_TIMEOUT_MS` since the first message, however many messages arrived — not one build per message.
- Unit-tested against fake repositories and `CacheStore`.
- Integration pass against real Postgres and Redis: seed `events`/`event_scores`, publish `stream:event_scored` directly, check the stored version and pointer across several batches, including one above and one below `NOISE_FILTER_THRESHOLD`.
- Each successful build logs INFO `cache_build_completed` with `cache_version`, `records` and `latency_ms` (impl.md §14.5).
**Docs:** impl.md §4.1, §10.2, §10.3, §14.1, §14.2, §14.5, §14.7, §16; design.md §6.1, §6.2, §6.8.
**Explicitly deferred:** the startup build (Cache: Startup Initial Build); inline reclaim, dead-letter and the `v` check; orphan cleanup; layer monitoring.

---

### Cache: Startup Initial Build (S)

**Status:** Created
**Depends on:** Cache: Build Cache from Scores
**Scope:** Build the first cache version from everything already scored, once at startup, before the Cache consumer starts (impl.md §14.1 `build_initial_version()`, §16; design.md §6.1).
- `app/config.py`: `CacheConfig.full_fetch_page_size`.
- `EventScoreRepository.fetch_scores()`: the keyset-paginated full fetch (`event_ids=None`) (§10.3).
- `CacheService.build_initial_version()` and `_build_full()` (§14.1), and `build_new_version()`'s fallback to `_build_full()` when no version exists (§14.1 step 1).
- `app/main.py`: the startup call before the Cache consumer's Task starts, logging CRITICAL `cache_startup_build_failed` and continuing on failure (§16).
**Acceptance criteria** (per impl.md §14.1, §16):
- No existing version: one full build from every `event_scores` row above `NOISE_FILTER_THRESHOLD`; zero rows still produces an empty, built version (so `GET /v1/morning-brief` returns 200, not 503).
- An existing version (Redis survived a restart) is left untouched.
- The full fetch pages through more than one `CACHE_FULL_FETCH_PAGE_SIZE` page correctly, and the matching Events are fetched in chunks of the same size — no single query carries every scored Event's id.
- A failure logs `cache_startup_build_failed` at CRITICAL and startup continues; the consumer's first batch then finds no version and does the full build instead — every scored Event above the threshold is in the cache, not just that batch. If that full build fails too, the batch stays un-ACKed and the next one tries again.
- Unit-tested against fake repositories.
- Integration pass against real Postgres and Redis.
**Docs:** impl.md §10.3, §14.1, §14.7, §16; design.md §6.1, §6.8.

---

### Cache: Inline Reclaim, Dead Letter & Schema-Version Check (S)

**Status:** Created
**Depends on:** Cache: Build Cache from Scores; Entity Analysis: Reclaim, Dead Letter & Schema-Version Check (for `autoclaim()`, `ReclaimedMessage`)
**Scope:** Recover batches the Cache layer leaves un-ACKed, inside the single serial loop rather than a second coroutine, and dead-letter messages that keep failing or are malformed (missing or unrecognised `v`, missing or invalid `event_id`) (impl.md §14.2; §6.1's pattern, adapted).
- `app/config.py`: `CacheConfig.max_retry`.
- `CacheConsumer._consumer_loop()`: every `STREAM_CLAIM_TIMEOUT_MS`, `autoclaim()` inline; dead-letter at `CACHE_MAX_RETRY` to `stream:cache_dead_letter`; merge the remaining reclaimed ids into the same batch as `read_from()`; the message shape check (`v` and `event_id`) on both paths.
**Acceptance criteria** (per impl.md §6.1, §14.2):
- A batch left un-ACKed by a failed build is reclaimed and built after `STREAM_CLAIM_TIMEOUT_MS`.
- Reclaimed and freshly read ids go into one build — at most one new version per loop iteration.
- At `delivery_count >= CACHE_MAX_RETRY`, dead-lettered with §6.1's payload and ACKed, unprocessed.
- A message whose `v` is missing or unrecognised, or whose `event_id` is missing or not a valid UUID, on either path, is dead-lettered (`delivery_count="1"` on the `read_from()` path) and ACKed; every other message in its batch is still processed normally.
- Unit-tested against mocked Redis.
- Integration pass against real Redis/Postgres: a batch that fails once is rebuilt; one that keeps failing reaches the dead-letter stream.
**Docs:** impl.md §4.1, §6.1, §14.2; design.md §6.8, §12.1.
**Explicitly deferred:** orphan cleanup; layer monitoring; dead-letter consumption (Q-3, Post-MVP).

---

### Cache: Orphan Cleanup (S)

**Status:** Created
**Depends on:** Cache: Build Cache from Scores; Entity Analysis: Reclaim, Dead Letter & Schema-Version Check (for `run_supervised()`)
**Scope:** Periodically delete cache versions left behind by a crash or a failed delete (impl.md §14.6; design.md §12.1 Orphaned Cache Cleanup).
- `app/config.py`: `CacheConfig.orphan_cleanup_interval_s`.
- `app/redis/state_client.py`: `delete()` (§6.2) — `scan()` already exists (Aggregation: Flush Active Events & Publish).
- `CacheStore.scan_orphaned_versions()` (§10.4).
- `CacheService.orphan_cleanup_loop()` (§14.6).
- `CacheConsumer.start()`: runs `_consumer_loop()` and `orphan_cleanup_loop()` through `run_supervised()` (§14.2).
**Acceptance criteria** (per impl.md §14.6):
- Every `CACHE_ORPHAN_CLEANUP_INTERVAL_S`, every version key older than the current version is deleted; `morning_brief:version_counter` is never touched.
- A build committing during a sweep (the pointer moves to a newer version after the sweep read it) never has its new version deleted — versions at or above the one read are left for a later sweep.
- A failing sweep logs `orphan_cleanup_sweep_failed` and the next tick retries; the consumer loop is never affected.
- If either loop raises, `run_supervised()` stops the other before `start()` returns — a restart never leaves two consumer loops running.
- Unit-tested against mocked Redis.
- Integration pass against real Redis with planted orphan keys.
**Docs:** impl.md §4.1, §6.2, §10.4, §14.2, §14.6; design.md §6.8, §12.1.

---

### Cache: Layer Monitoring & Health (XS)

**Status:** Created
**Depends on:** Cache: Orphan Cleanup (for `run_supervised()` in `start()`); Entity Analysis: Layer Monitoring & Health (for `_make_monitored()`, `get_layer_tasks`); Aggregation: Layer Monitoring & Health and Scoring: Layer Monitoring & Health (so `layers.aggregation` and `layers.scoring` are already real)
**Scope:** Register the Cache layer's Task for auto-restart and report its real liveness in `GET /v1/health` — the last layer, so `healthy` becomes reachable (impl.md §15.2, §16).
- `app/main.py`: register `"cache"` with `_make_monitored()`.
- `app/api/routes/health.py`: `layers.cache` reflects whether its Task is running; no `layers.*` field is hardcoded any more.
**Acceptance criteria** (per impl.md §15.2, §16; api.md §3.2):
- A crash in `CacheConsumer.start()` logs `layer_task_crashed` and restarts the Task on the same consumer instance; a cancelled Task is never restarted.
- With everything up, `GET /v1/health` returns `healthy`, `HTTP 200`, all four `layers.*` `ok`.
- Any one layer's Task down → `degraded`, `HTTP 200`, with only that layer `error`.
- The Health Check ticket's integration cases re-run: each Postgres/Redis stop or pause gives `unhealthy`, `HTTP 503`, with only the failed field `error`; recovery returns to `healthy`, `HTTP 200`, without restarting the app.
- Unit-tested with a fake consumer whose `start()` raises.
**Docs:** impl.md §15.2, §16; api.md §3.2; design.md §7.1, §12.1.

---

### Cache: Post-Release Tuning Validation (S)

**Status:** Created
**Depends on:** every Cache ticket above and API: `GET /v1/morning-brief` (below); Deployment: SAPI Container, Compose & Production-like Run (below — the real-traffic run), run for an agreed length.
**Scope:** Answer the Cache and Morning Brief tuning questions that need real volume, from production logs, Redis, and request metrics — no new instrumentation.
- **`NOISE_FILTER_THRESHOLD`:** share of scored Events that make the cache, and whether low-impact noise gets through.
- **`RECENCY_DECAY_LAMBDA`:** whether ranking favours fresh Events the way analysts expect, on a hand-checked sample of Morning Briefs.
- **`TOP_K_DEFAULT`:** whether the default result size fits the client.
- **Q-1 (Cache's share):** `CACHE_BATCH_TIMEOUT_MS`, from build frequency and duration.
- **Cache growth (Q-17):** cache size and `GET /v1/morning-brief` latency over the run — checks the retention policy decided before go-live (design.md §14 Q-17), or, if none was adopted, how fast the cache grows and how often old Events appear in the backfill.
**Acceptance criteria:**
- Each tuning item has measured numbers and a decision: keep the default, or change it with the reason.
- Changed defaults are applied in `app/config.py` and impl.md §4.1.
- Q-17's growth, latency and old-Event-in-backfill numbers are recorded against whatever retention decision was made before go-live.
- Q-1 is marked resolved in impl.md §18 and design.md §14 once every layer's share is answered.
**Docs:** impl.md §4.1, §14, §15.3, §18; design.md §6, §14 (Q-1, Q-17).
**PM note:** run length to be set; can share the production run with the other layers' validation tickets.

---

### API: `GET /v1/morning-brief` (M)

**Status:** Created
**Depends on:** Cache: Recency & Merge (for `CacheRecord`); Cache: Versioned Cache Store (for `CacheStore`); Cache: Build Cache from Scores (for `CacheReader`)
**Scope:** Serve the Morning Brief from the current cache version, as built — no re-scoring per request (impl.md §15.3, §15.4, §3.5; api.md §3.3; design.md §6.3, §7).
- `app/config.py`: `CacheConfig.top_k_default`.
- `app/api/schemas.py`: `MorningBriefQuery`, `SourceListEntry`, `EventDetail`, `RuleScoreDetail`, `LLMScoreDetail`, `ScoreDetail`, `ScoredEventRecord`, `MorningBriefResponse` (§3.5).
- `app/common/error_codes.py`/`exceptions.py`: `MorningBriefCacheUnavailableException` (`SAPI-5001`) and the `MALFORMED_REQUEST`/`VALIDATION_FAILED`/`INTERNAL_ERROR` catalog entries (§15.1, §15.4).
- `app/main.py`: exception handlers for `RequestValidationError` (400, `COMMON-4000`/`4001`), `MorningBriefCacheUnavailableException` (503) and the general `Exception` catch-all (500 `COMMON-5000`) (§15.1).
- `app/api/routes/morning_brief.py`: `get_morning_brief()` (§15.3), reading records through `CacheReader.get_records()`.
**Acceptance criteria** (per impl.md §15.3, api.md §3.3):
- The same cache version always returns the same Events in the same order and with the same scores — the route reads, it never recomputes recency.
- A version is parsed at most once: requests for a version already held never read Redis for its data; a version not held (e.g. after a restart) is read and parsed once, then reused; `prime()` makes a version held without any parse. Requests never change the shared records.
- Watchlist Events first, then the backfill of the best non-watchlist Events up to `k` — each group in the version's stored order, and the response keeps that order rather than re-sorting it as one list.
- `k` omitted → `TOP_K_DEFAULT`; `k` outside 1–50 or `stocks` empty → 400 `COMMON-4001`; malformed request → 400 `COMMON-4000`.
- No version ever built → 503 `SAPI-5001`; a built cache with no matches → 200 with `events: []` and non-null `cache_version`/`last_updated`.
- A build replacing the version between the pointer read and the data read is retried (up to 3 pointer reads), never served as an empty brief; `cache_version`/`last_updated` always belong to the version actually served.
- Redis unreachable → 503 `COMMON-5001`; an unanticipated error → 500 `COMMON-5000`.
- The response never contains the three internal fields, and matches api.md §3.3 field for field.
- Unit-tested against a fake `CacheStore`.
- Integration pass against real Redis with a hand-written cache version.
**Docs:** impl.md §3.5, §14.8, §15.1, §15.3, §15.4, §16; api.md §3.3; design.md §6.3, §6.6, §7.2–§7.4.
**Explicitly deferred:** anything that builds the cache, including the daily refresh (the Cache tickets).

---

### Cache: Daily Refresh Trigger (S)

**Status:** Created
**Depends on:** Cache: Startup Initial Build (for `_build_full()` and `build_new_version()`'s no-version fallback)
**Scope:** Let Admin rebuild the Morning Brief cache once a day so recency moves forward even with no new scores — `POST /v1/admin/morning-brief-refresh`, synchronous, with every cache build serialised by one lock (impl.md §14.9, §15.7; api.md §3.5; design.md §6.9).
- `app/cache/cache_service.py`: `CacheService._build_lock` around `build_new_version()` and `build_initial_version()`; `build_new_version()`/`_build_full()`/`_write_new_version()` return the version written; an empty `event_ids` skips the DB reads (§14.1, §14.9).
- `app/api/schemas.py`: `MorningBriefRefreshResponse` (§3.5).
- `app/api/routes/morning_brief_refresh.py`: `post_morning_brief_refresh()` (§15.7), registered in `create_app()` (§15.1).
**Acceptance criteria** (per impl.md §14.9, §15.7; api.md §3.5):
- A refresh with no new scores writes a new version: every cached Event's recency recomputed as of now, re-sorted, no DB query; returns 200 with `status: "success"` and the new `cache_version`.
- A refresh during a score-triggered build waits for it — the two never overlap, and neither overwrites the other's version.
- No version yet (failed startup build) → the refresh does the full build.
- Redis unreachable → 503 `COMMON-5001`, nothing written, the previous version keeps serving.
- Unit-tested against fake repositories and `CacheStore`, including the concurrent-build case.
- Integration pass against real Redis/Postgres: call the endpoint directly (Admin is not involved), check the new version, its recency, and the pointer.
**Docs:** impl.md §3.5, §14.1, §14.9, §15.1, §15.7; api.md §3.5; design.md §6.9, §13.2.
**Explicitly deferred:** Admin's `MORNING_BRIEF_REFRESH` job (`admin-tad.md` §3.6 — Admin's own implementation).

---

### Deployment: SAPI Container, Compose & Production-like Run (M)

**Status:** Created
**Depends on:** every SAPI feature ticket above — the production-like run needs the whole pipeline (the `Dockerfile` itself only needs Health Check: `GET /v1/health`).
**Scope:** Package SAPI and run it alongside SADI on shared Postgres and Redis — locally and on a production-like VM — ready for each layer's Post-Release Tuning Validation run (impl.md §1, §4.1, §16, §17; design.md §13).
- **Image:** `stock-assistant-pipeline-intelligence/Dockerfile`, mirroring SADI's: `python:3.12-slim`, dependencies from a hash-pinned `requirements.lock` (`uv pip compile --generate-hashes`, including the `google-genai`/`instructor` versions the LLM spike pinned), `app/` and `alembic/`, port 8001.
- **Compose:** a new root-level `docker-compose.yml` running `sadi`, `sapi`, `postgres` and `redis` together (design.md §13.1's shared infrastructure). The `sapi` service:
  - `restart: unless-stopped`, a health check on `/v1/health`, `depends_on` Postgres and Redis being healthy;
  - exactly one instance (design.md §13.2 — Aggregation, the Cache build lock and the parsed-cache memory all assume one process);
  - a `stop_grace_period` long enough for impl.md §16's shutdown sequence: Aggregation's final flush and any cache build in progress.
- **Database:** a `sapi` database next to SADI's `sadi` — created by a Postgres init script on a fresh volume, and by the deploy script when missing, since init scripts never run on an existing volume such as one SADI already uses.
- **Migrations:** `alembic upgrade head`, run by the deploy script (as SADI's does), not on container start.
- **Configuration:** `stock-assistant-pipeline-intelligence/.env.example` listing every SAPI variable in impl.md §4.1 with its default (impl.md §1 names it the single source of truth). Compose sets the service addresses: `DATABASE_URL` (the `sapi` database), `REDIS_STREAM_URL`/`REDIS_STATE_URL`, `SADI_API_URL=http://sadi:8000`, `MWP_ADMIN_API_URL`.
- **Vertex AI credentials:** a service-account key mounted read-only and named by `GOOGLE_APPLICATION_CREDENTIALS`, or the VM's own identity on GCP — never in the image or the repository.
- **Deploy script** for the production-like VM, mirroring SADI's `scripts/volume-test/deploy.sh`: Docker install, `.env`, `docker compose up --build`, create the `sapi` database if missing, migrations, smoke test (`/v1/health` → `healthy`, then one crawled article followed through to `GET /v1/morning-brief`).
- **Scheduled triggers until Admin exists:** a crontab calling `POST /v1/admin/hk-stock-list-sync` and `POST /v1/admin/morning-brief-refresh` daily, plus SADI's crawl triggers so real traffic flows — standing in for Admin's jobs (`admin-tad.md` §1.2, §3.6).
- **SADI's `v` field:** the deployed SADI must include the change adding `v` to `stream:raw_news_cleaned`; without it every article is dead-lettered by Entity Analysis.
**Acceptance criteria:**
- On a clean machine, `docker compose up` brings up SADI, SAPI, Postgres and Redis; after `alembic upgrade head`, SAPI's health check reports `healthy`, `HTTP 200`, once the HK Stock List has loaded.
- The deploy script runs end to end on a fresh VM, is safe to re-run, and creates the `sapi` database on a Postgres volume that already exists.
- An article crawled by SADI appears in `GET /v1/morning-brief` after flowing through all four layers.
- The crontab's two SAPI triggers succeed (`200`) and a new cache version appears after the refresh.
- No credentials or secrets are in the image or the repository.
- Stopping the stack lets SAPI finish its shutdown sequence — no error logs from a cut-off flush or build.
- Only one SAPI instance can run.
**Docs:** impl.md §1, §4.1, §16, §17; design.md §13.1, §13.2; api.md §3.4, §3.5; admin-tad.md §1.2, §3.6.
**Explicitly deferred:** multiple replicas and high availability (design.md §13.2); Admin itself; CI/CD pipelines; production secret management beyond a mounted key file.

---

*— End of Document | SAPI Implementation Plan v0.1 — DRAFT —*
