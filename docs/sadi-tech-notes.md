# SADI — Tech Points, Challenges & Self-Learning Notes

Personal notes on the Stock Assistant Data Ingestion (SADI) service, written for
technical-skill review / interview prep. See
[stock-assistant-data-ingestion/progress.md](stock-assistant-data-ingestion/progress.md)
and
[stock-assistant-data-ingestion/docs/implementation.md](stock-assistant-data-ingestion/docs/implementation.md)
for the authoritative specs this is drawn from.

---

## 1. The challenges (what actually made this hard)

**a) Four sources, one interface, wildly different failure modes.**
`BaseCrawler` has to abstract over: browser-automation pagination (HKEX),
RSS+full-page-scrape (Ming Pao), plain HTTP+CSS-selector (AAStocks), and
RSS+content-extraction (Yahoo HK) — plus HKEX uniquely fans out into parallel PDF
downloads. The challenge isn't scraping any one of these; it's finding an
abstraction (`CrawlResult{successes, failures}`) that doesn't leak source-specific
concerns upward into `CrawlService`.

**b) Anti-bot detection was adversarial, not a config bug.**
Ming Pao returned Cloudflare's "Attention Required" page for 100% of requests. The
fix required knowing *why* headless Chromium gets fingerprinted
(`chromium_headless_shell` vs full `channel="chromium"`, the `navigator.webdriver`
tell, TLS/header fingerprints) — see
[implementation.md:1076](stock-assistant-data-ingestion/docs/implementation.md#L1076).
Months later the block came back on a different network, and the team decided to
**abandon** full-article fetch entirely and fall back to RSS teaser text
([progress.md:91](stock-assistant-data-ingestion/progress.md#L91)) — a real "know
when to stop fighting the adversary" call.

**c) A misleading symptom (HTTP 429) with a non-obvious root cause.**
Yahoo HK silently saved 0 rows while `stream:crawl_completed` still reported
SUCCESS. The obvious hypothesis — rate limiting — was wrong. Root cause was a
shared `httpx.AsyncClient` sending the *default* `python-httpx/x.y` User-Agent on
every request ([progress.md:90](stock-assistant-data-ingestion/progress.md#L90)).
A hard bug class: the error code lied about the mechanism.

**d) Silent data loss vs. silent data corruption — two different failure shapes.**
- HKEX regex date parsing matched the wrong DOM text and produced
  `published_at=None` for 100% of rows before it was caught by an audit
  ([progress.md:107, Q-8](stock-assistant-data-ingestion/progress.md#L107)).
- A timezone bucketing bug was caught *before* shipping, while designing the
  daily-volume SQL query for the SADI volume test: grouping by `published_at`
  under Postgres's UTC session default would silently misfile anything
  published HKT 00:00–07:59 into the previous day.

Neither of these throws an exception — both need an engineer who distrusts "it ran
without errors."

**e) Deciding what *not* to build yet.**
Two open questions (Q-2 body-length threshold, Q-9 DB round-trip batching) were
deliberately left unresolved rather than guessed at, and a volume-test plan was
designed specifically to generate the missing data instead of debating it further.
The plan itself was later revised to *shrink* scope (dropped production-sizing as a
goal) once the team realized SAPI doesn't exist yet, so hardware sizing now would be
optimizing a step that might get thrown away.

---

## 2. Where the engineering skill actually lives

| Skill | Where it shows up |
|---|---|
| Root-causing through a misleading symptom | 429 debugging (1c) — required an isolated A/B request, not trusting the error label |
| Designing for exactly-once semantics on top of an at-least-once transport | Redis Streams ack-after-both-writes + `XAUTOCLAIM` reclaim ([implementation.md:1719-1795](stock-assistant-data-ingestion/docs/implementation.md#L1719)); dedup via DB `UNIQUE` constraint instead of app-level locking |
| Exception-translation / anti-corruption layering | `DatabaseClient` is the only module allowed to import `asyncpg`; every driver exception maps to a driver-agnostic type ([implementation.md:417-454](stock-assistant-data-ingestion/docs/implementation.md#L417)) |
| Distinguishing transient vs. permanent failure | Retry-with-backoff only on connection errors, never on constraint violations — a classification table, not blanket retry |
| Adversarial/anti-detection engineering | Cloudflare fingerprint evasion (1b) |
| Recognizing "ran successfully" ≠ "worked" | (1c) and (1d) — both bugs passed at the HTTP-status level |
| Timezone-correctness under implicit defaults | (1d), second bullet — Postgres session TZ silently defaulting to UTC |
| Scoping a data-gathering exercise instead of guessing | Q-2/Q-9 handling, and the volume-test revision itself (1e) |
| Infra cost/risk trade-off reasoning | Server choice for the volume test — free ARM (Oracle) rejected due to unvalidated arm64 compatibility risk on an *unattended* 2-week run, spot instances rejected for reclaim risk, landed on GCP trial credit |

---

## 3. Where to go deeper technically

Pick based on what kind of interview you're prepping for:

- **Distributed systems / messaging**: Redis Streams consumer groups
  (`XREADGROUP`, `XACK`, `XAUTOCLAIM`) — read
  [implementation.md §6 and §9.2-9.3](stock-assistant-data-ingestion/docs/implementation.md#L554),
  then study delivery guarantees generally (at-most-once vs at-least-once vs
  effectively-once, and why idempotency is what actually gets you "exactly once").
- **Anti-bot / browser automation internals**: dig into how Cloudflare
  fingerprints headless browsers (TLS JA3, `navigator.webdriver`, CDP detection)
  beyond what's in `browser_manager.py` — a deep, interview-differentiating niche
  few candidates know cold.
- **Async Python internals**: why `feedparser` (sync) gets wrapped in
  `asyncio.to_thread()`, how `asyncio.gather()` composing a queue-reader + N
  workers + a reclaim-loop behaves under backpressure, what happens if a worker
  coroutine raises uncaught.
- **Postgres specifics**: `pg_stat_statements` for real query-cost analysis (used
  for Q-9), `ON CONFLICT ... RETURNING` semantics, `AT TIME ZONE` bucketing
  gotchas — all concretely exercised here, not abstract.
- **Production debugging methodology**: reconstruct the 429 bug and the HKEX
  date-parsing bug as a story — symptom, wrong hypothesis, how it was actually
  isolated, fix, regression check. That's the strongest interview material; it's
  a real "tell me about a bug you debugged" answer with no embellishment needed.
- **System design trade-offs**: the volume-test-plan's server-selection reasoning
  and the Q-9 "measure before batching" decision are good raw material for "how do
  you decide when to add complexity" questions.

---

## 4. Future self-learning

### In progress

- **Resolve Q-9 (DB round-trip batching) with real data.** The volume test
  currently running (see
  [volume-test-plan.md](stock-assistant-data-ingestion/docs/volume-test-plan.md),
  status DRAFT) is collecting `pg_stat_statements` numbers specifically to answer
  this. Once real per-record latency × real daily volume is known, decide whether
  batching is worth it, and if so implement it — batch idempotency checks,
  fetches, and inserts, and work out per-item error handling and partial-batch
  ACK semantics. Partial failure inside a batch is a genuinely meaty
  distributed-systems problem on its own, and this one is self-contained and
  bounded — a good next exercise once the data lands.

### Future

- **Observability — currently the weakest layer.** SADI's health check is binary
  (ok/error, no `degraded` state) and there's no metrics/tracing anywhere in the
  stack. Good self-study arc: add Prometheus metrics (consumer-group lag on
  `stream:raw_news_inserted` is explicitly flagged as the "leading candidate
  signal" for a future `degraded` state —
  [implementation.md:869-872](stock-assistant-data-ingestion/docs/implementation.md#L869)),
  wire up Grafana, then read up on distributed tracing (OpenTelemetry) for
  tracking one article's journey crawl → clean → NLP → brief across services.

- **Infrastructure-as-code.** The volume-test GCP VM was created by hand with a
  manual checklist. Terraform-ing that VM + firewall rule + docker-compose deploy is a small,
  realistic first IaC project with an existing manual process to compare against.

- **CI/CD.** No pipeline exists yet — tests run manually (`pytest -m live` is
  opt-in and manual). Setting up GitHub Actions to run the unit suite on PRs, and
  separately a scheduled/manual trigger for the live integration tests, is a
  natural next step and a common interview topic ("how do you gate deploys").

- **AuthN/AuthZ.** SADI explicitly has none — "single-user MVP... only ever
  called by other internal MWP services"
  ([implementation.md:719-721](stock-assistant-data-ingestion/docs/implementation.md#L719)).
  Once more than one service calls in, that assumption breaks. Good self-study:
  service-to-service auth patterns (mTLS vs signed JWT vs API keys) and what
  changes in the error-handling doc's `detail` field once callers aren't fully
  trusted (right now it says both `message` and `dev_message` are safe to return
  because *no* external caller exists — that reasoning has an expiry date).

- **Load/chaos testing beyond the volume test.** The current volume-test plan
  measures *steady-state* throughput. A natural follow-up once that data exists:
  inject failures (kill a worker mid-batch, drop Redis briefly) and verify the
  reclaim-loop/redelivery logic actually recovers — turning the "ack only after
  both writes succeed" design contract from a documented invariant into something
  you've watched survive a real fault.
