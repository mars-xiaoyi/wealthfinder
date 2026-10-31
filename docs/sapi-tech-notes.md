# SAPI — Tech Notes

Learning notes from designing SAPI, the Redis Streams–based pipeline in this repo (Entity Analysis → Aggregation → Scoring → Cache). SAPI's binding design lives in `stock-assistant-pipeline-intelligence/docs/` (`impl.md` = `implementation.md`, `design.md` = `system-design.md` below); nothing here is a design decision for it.

- **Part 1** gives SAPI's technical highlights for a reader new to the project: what the system does, then what was decided and why, with measured results where there are any. It stands alone, without the design docs. Its LLM items come from two spikes, whose full write-ups are `poc/sapi/fuzzy-match/README.md` and `poc/sapi/llm-skill/README.md`.
- **Part 2** covers the general topics behind those highlights, for future investigation. Each ends with questions worth digging into.

---

# Part 1 — Technical highlights

## 0. The system in one minute

**What it is.** Mars Wealthfinder turns Hong Kong financial news into a ranked *Morning Brief*: the stock-moving events an investment researcher should read first. SAPI is its intelligence service. It takes cleaned articles from four sources (HKEX exchange filings, Ming Pao, AASTOCKS, Yahoo Finance HK). For each article it works out which listed companies the article is about. It then groups related articles into one story per stock, scores each story with rules plus an LLM, and serves the ranked list.

**Pipeline.** Four layers. Each is an independent consumer, the layers are connected by Redis Streams (a persistent message queue with at-least-once delivery), and Postgres is the system of record:

```
article → Entity Analysis (LLM: which companies, what kind of event, what sentiment)
        → Aggregation    (merge mentions of the same stock and event type into one story)
        → Scoring        (rules + LLM)
        → Cache          → Morning Brief API
```

Stack: Python 3.12, asyncio, FastAPI, Redis Streams, PostgreSQL, Vertex AI (Gemini).

**Terms used below.**

| Term | Meaning |
|---|---|
| **Mention** | one company in one article, with its event type and sentiment (`EntityEvent` in the code) |
| **Story** | all mentions of one stock and one event type within a sliding time window, up to a maximum total span (`Event` in the code) |
| **Group** | the key a story is built under: (stock, event type). Each group has at most one live story. |
| **Flush** | Aggregation keeps live stories in Redis, and periodically writes them to Postgres and tells Scoring which ones changed |
| **HK Stock List** | the exchange's official list of about 2,800 listed equities, used to verify stock codes |
| **Acknowledge / redeliver** | a consumer marks a message done. A message not acknowledged within a timeout is redelivered, and after N deliveries it goes to a dead-letter queue. |

**Why it's hard.**
- **An LLM in the hot path:** every article costs money and seconds, the provider rate-limits, and the output is non-deterministic, so it has to be verified.
- **Company names in two languages and two scripts:** abbreviations, traditional vs simplified Chinese, the same company listed on more than one counter.
- **At-least-once delivery across Postgres and Redis, which share no transaction:** every step must survive crashes and duplicate messages without losing or double-counting anything.

Sections A and B are the strongest stories. A covers the LLM design and how it was evaluated (results measured on real articles). B covers reliability problems caught in design review, before any of that code was written.

---

## A. LLM design and evaluation

### A1. One LLM call plus checks in code: more accurate, and with the model choice in A2, 8× cheaper and ~35× faster at p95

**Problem.** For each article, the system must find the listed companies it is about (by stock code) and classify the event and its sentiment. The original design had the LLM verify each company by calling a stock-lookup tool in a loop, then make a second call to produce the final answer.

**Approach.** A time-boxed spike against the live Vertex AI API compared five design and model combinations on 127 real articles that no design was tuned on. The labels came for free: articles print stock codes next to company names. Each combination was measured on accuracy, cost, tail latency and failure rate.

**Finding.** The tool loop was solving a problem the inputs had already solved. Articles almost always print the code, and the model already knows most HKEX codes: 94% of its tool calls only confirmed a code it already had. The check itself is deterministic, so it moved out of the model. One structured-output call returns each company with its code. Ordinary code then accepts the code if the official list's name for it agrees with the model's, or if the article prints it, and otherwise falls back to a name search (A3).

| Design and model | Accuracy (entity F1) | $ per 1,000 articles | p95 latency per article |
|---|---|---|---|
| Tool loop, Gemini 3.8 Flash | 0.930 | 8.74 | 217 s |
| One call + code checks, same model | 0.947 | 2.98 | 130 s |
| **One call + code checks, Gemini 3.1 Flash-Lite (A2)** | **0.962** | **1.14** | **6 s** |

The design change alone (first two rows) made the step more accurate and 3× cheaper. The model change (A2) did the rest. The final configuration had zero failed calls on this set. The loop exactly as first specified, with default settings, cost $23.09 per 1,000 articles on an earlier test set.

**Trade-off accepted.** About 6% of real articles name companies without printing any code (mostly sector round-ups). Those rely on the model's memory plus the name search. In a worst-case test with every code stripped, F1 is 0.913 against the loop's 0.984.

**Why it's subtle.** "Let the model check its work with a tool" sounds like the robust choice. The loop also had failure modes of its own. In one tool-calling mode the model must call a tool every turn, so the loop never ends. In the other it sometimes repeated a lookup until it hit the round limit. Dense market round-ups took 4–6 rounds of 10–16 lookups each.

**Shows:** questioning an accepted design with measurements, moving deterministic work out of the model, and stating the trade-off explicitly. Background: §2.8.

### A2. Choosing the model by what the pipeline pays for

**Problem.** Which Gemini model should run on every article? The instinct is "the strongest one we can afford".

**Approach.** Each candidate was measured on four things: accuracy against a quality ceiling, cost per article, tail latency, and failure rate under the real (shared) quota. In the same one-call design:

| Model | Entity F1 | $ per 1,000 articles | Failed attempts (all spike runs) |
|---|---|---|---|
| Gemini 3.8 Flash (low thinking) | 0.947 | 2.98 | 7.6% of 1,376 attempts |
| **Gemini 3.1 Flash-Lite** | **0.962** | **1.14** | **0.6% of 838** (p95 8 s) |

During one ~20-minute provider capacity episode, 3.8 Flash failed all four attempts on 74 of 127 articles. Gemini 3.1 Pro was only run in the original tool loop, where it scored F1 0.703 at $41.33 per 1,000 articles, missing up to 30 companies on dense round-ups, so it went no further.

**The noise floor.** For event type and sentiment, Pro served as the reference. Flash-Lite agreed with Pro on 89% of event types and 91% of sentiments, but Pro agrees with *itself* (two runs on the same articles) on only 95% and 97%. That self-agreement is the ceiling, and a gap smaller than the reference's own disagreement isn't evidence of a quality difference. Routing exchange filings to the bigger model bought nothing measurable.

**Decision.** Gemini 3.1 Flash-Lite. Known weaknesses, accepted and handled: it rates some negative exchange filings as neutral (a share placing at a 19.5% discount, for example), and some of its summaries exceed the 100-character limit, which code truncates.

**Shows:** evaluating models on cost, tail latency and reliability rather than benchmark quality alone, using self-agreement as a noise floor, and not over-buying capability. Background: §2.8.

### A3. Matching company names: replacing an intuitive algorithm with a measured one

**Problem.** When the model's stock code doesn't check out, the system searches the official list by company name. The original algorithm scored names by shared characters above a 0.6 threshold. On 475 labeled mentions from real articles it got 263 right, 80 wrong and missed 132. Inside the full pipeline it added 19 wrong matches and gained nothing.

**Why it's subtle.**
- **A wrong match is silent.** It returns a real, plausible stock code, and nothing downstream flags it. A miss only drops one mention.
- **Chinese abbreviations keep the full name's characters in order, taken from anywhere in the name.** 農行 is short for 農業銀行 (Agricultural Bank of China), and 國壽 for 中國人壽 (China Life). Counting shared characters ignores order. Generic words (地產 "property", 銀行 "bank") match almost everything.
- **The data had traps of its own:**
  - The same company is listed on both an HKD and an RMB counter with the same name, so the two tied and file order picked the winner.
  - Variant characters (恆/恒) look identical to a reader but not to `==`.
  - The order of the normalization steps changed what a rule actually did.
- **The fallback runs for only ~1.5% of companies,** so overall accuracy hides its damage unless it is measured on its own.

**Approach.**
- **Research:** the literature confirms that Chinese abbreviations are ordered subsequences of the full name, and that normalization matters at least as much as the choice of similarity measure.
- **A labeled set built from free labels:** the `name(code)` pairs articles print, plus exchange filing headers, foreign tickers as "should not match" cases, and hand-collected hard cases.
- **Ten rounds of changes,** each kept or rejected on the development half by *correct − 2 × wrong*, because a wrong answer costs more than no answer. Breaking ties by lowest stock code, for example, was rejected: it turned 9 misses into 3 right answers and 6 wrong ones.

**Decision.**
- **The matcher:** ordered-subsequence matching on normalized names (script and width folding, legal suffixes and generic words stripped), with no threshold, and a tie between companies counts as a miss.
- **Data clean-up at load:** duplicate counters are dropped when the list loads.
- **Shared normalization:** one normalizer serves both name checks.

**Result.**

| Set | Old algorithm (right / wrong / missed) | New (right / wrong / missed) |
|---|---|---|
| 475 labeled mentions | 263 / 80 / 132 | **431 / 12 / 32** |
| Held-out exchange filings, scored once | 1 / 2 / 46 | **42 / 2 / 5** |
| Held-out news and filings, scored once against a pass bar set in advance | 106 / 10 / 82 | **187 / 2 / 9** (1.0% wrong; the bar was ≤ 3%) |

The matcher answers in p99 ~5 ms over the whole list, so it doesn't need a worker thread. What string matching can't solve (irregular abbreviations, mainland companies' English legal names) is documented, with an alias table as the known remedy.

**Shows:** replacing intuition with labeled data, designing for the asymmetric cost of errors, experimental discipline (held-out sets, a pre-set pass bar, rejected rounds recorded), and knowing where a technique stops working. Background: §2.7.

### A4. Making the numbers behind a decision reproducible

**Problem.** In review, every figure in the LLM spike's write-up was re-derived from the committed run records, and several didn't hold:
- **Label leakage.** The "every code stripped" worst-case set still contained the answer for some exchange filings: in a synthetic title, and in a `Stock code (if listed) 00595` line. The models used it.
- **A moving input.** The evaluation scripts read the prompt live from the design doc. Once the doc was updated with the new design, they no longer ran the prompt that had produced the results.
- **Inconsistent denominators.** One model's failure rate was per successful call, the other's per attempt.
- **Stale or misattributed figures.** One figure came from an earlier version of the verifier, and one result was credited to the wrong variant.
- **Data hygiene.** Third-party article titles had been committed inside run records.

**Why it's subtle.** Each number was right when it was written. The drift came from later changes to shared inputs (prompt, verifier, normalizer) without a re-run. And a "codes stripped" test set built with regular expressions is only as clean as its patterns.

**Decision.**
- **Frozen inputs:** a versioned prompt copy and a snapshot of the stock list.
- **Scripted figures:** every split and figure is derived by a script, and each metric states one denominator.
- **Leakage checks and re-scoring:** constructed test sets are scanned for leaked labels, and stored model outputs are re-scored offline after every verifier change.
- **No third-party text** is committed.

**Shows:** treating the numbers behind a design decision like code that needs review, and handling data governance. Background: §2.10.

### A5. De-risking an external API before committing to a design

**Problem.** The design was written from documentation. Running it against the live API before any production code was written turned up six behaviours the docs didn't make obvious:
- **No default timeout.** SDK calls hung for ~260 s. With an explicit timeout a hang becomes a clean `504`, and the SDK makes exactly one attempt unless retries are configured.
- **A library's retry broke the protocol.** The structured-output library (Instructor) retried by sending the error back as a *model* message, which Gemini rejects. The API's native structured output already enforces the schema, so the library was dropped.
- **Hidden tokens.** "Thinking" tokens are billed as output and count against the output limit, so a limit sized from visible output can truncate answers.
- **Region and quota.** Current models are served only from the `global` endpoint. The quota is a shared pool: rate limits arrived at 4 or fewer concurrent calls, and there is no fixed requests-per-minute figure to size concurrency against.
- **Input size.** One exchange filing was 2 million characters, so input is capped at 30,000 before the call.
- **A cost lever that didn't apply.** Prompt caching needs ≥ 4,096 tokens, and the prompt is ~1,800.

**Shows:** treating a third-party dependency as unknown until measured, and turning each surprise into an explicit setting (timeout, retry owner, token budget, input cap). Background: §2.9.

---

## B. Reliability in an at-least-once pipeline

All of these were caught while reviewing the design, before implementation. Each one is a way to lose or corrupt data silently.

### B1. An idempotent insert that hid a lost message

**Problem.** Entity Analysis saves an article's mentions to Postgres, then publishes one message per mention to Aggregation. A crash between the two leaves the incoming article message unacknowledged, so it is redelivered. As first designed, the retry ran the LLM again and re-inserted. Every insert was a no-op because the rows already existed, and only newly inserted rows were published. So the lost message was never sent, and those mentions never reached a story.

**Why it's subtle.** The idempotent insert, the thing that made retries "safe", is exactly what hid the missing publish. And LLM output is non-deterministic, so a rerun might not even find the same companies.

**Decision.** Save an article's mentions in one transaction. On every delivery, first check whether the article already has saved mentions. If it does, republish their IDs and skip the LLM entirely. The next layer already ignores duplicates.

**Shows:** reasoning about the crash point between two systems, and making a retry recover from saved state instead of redoing paid, non-deterministic work. Background: §2.2, §2.4.

### B2. The dual-write problem: a transactional outbox in one column

**Problem.** Each flush writes live stories to Postgres and publishes "re-score this story" to Scoring. To avoid paying for LLM re-scoring of unchanged stories, the write skipped unchanged rows and only what it wrote was published. A crash after the commit but before the publish meant the retry found nothing changed, wrote nothing and published nothing, so the story was never scored.

**Why it's subtle.** Each half was correct on its own. It is the classic dual-write problem: Postgres and Redis share no transaction, so "save" and "notify" can't be made atomic directly.

**Decision.** A transactional outbox, kept as a column instead of a separate table. In the same transaction as the write, a story is marked "pending publish": its last-change timestamp is newer than its last-published one. After the commit, every pending story is published and then marked published. A crash leaves the marker in place, and the next flush publishes it.

**Outcome.** Every committed change is published at least once, and unchanged stories still cost no LLM calls.

**Shows:** recognising a known distributed-systems pattern and fitting it with the least machinery (one column, no separate relay process). Background: §2.5.

### B3. An "in progress" marker that behaved like "done"

**Problem.** Aggregation keeps an in-memory set of mentions currently being processed, so a redelivered copy isn't processed twice at once. An entry was removed only on success. A failure, such as a few seconds of database trouble, left it behind. Every redelivery was then skipped as "already in progress" until the message was dead-lettered, so a brief outage became permanently lost data.

**Why it's subtle.** The success path is the one you test. The leak only appears when something fails, recovers, and the message is redelivered. The set also lives on an object that survives the layer's automatic restart.

**Decision.** Release the marker on every exit path. A failure releases it without acknowledging, so the retry runs normally. General rule: an in-memory guard is for concurrency only, never for correctness.

**Shows:** thinking in three states (not started / in progress / done) rather than two, and designing for the fail–recover–redeliver path. Background: §2.4.

### B4. An older snapshot overwriting newer data

**Problem.** A flush reads a snapshot of all live stories from Redis, then writes them. In between, a story could be closed, replaced, and saved with newer state. The flush then wrote the older snapshot over it, and nothing would ever correct it.

**Why it's subtle.** "Write only if changed" looks like enough, but an older version is also "changed".

**Decision.** A conditional write that applies only if the incoming version is newer, compared by last-change timestamp. This is optimistic concurrency control, with no locks.

**Shows:** spotting a lost-update race, and choosing versioned conditional writes over locking. Background: §2.2.

### B5. Acknowledged work that lived only in Redis

Live stories sit in Redis between flushes, after their input messages have been acknowledged, so the queue can no longer redeliver them. The design had two ways to lose that state:
- **Replacement.** Each group has one Redis key for its live story. When a mention falls outside the time window, or the story reaches its maximum span (a frequent, normal case), the new story overwrote the key. Every merge into the old story since the last flush was lost. **Fix:** save and publish the replaced story before overwriting.
- **Expiry.** Each live story's key expires after `min(window, remaining span)`. Near the maximum span the remaining time can be a few seconds, shorter than the flush interval, and rounding to whole seconds can make it 0, which deletes the key at once. **Fix:** add a grace period of two flush intervals, so every key outlives at least one flush. Merge decisions use the story's own timestamps, so they are unaffected.

**Why it's subtle.** Once a message is acknowledged, the broker can no longer help. The safety rule "the expiry ≈ the window, which is far longer than the flush interval" stops holding at the edge of the story's lifespan.

**Shows:** following a piece of state through its whole lifecycle, and checking the edge cases of time arithmetic.

### B6. A rate-limit pause that dead-lettered healthy messages

**Problem.** On a rate-limit response (HTTP 429), the whole worker pool pauses for 60 s. Messages already read wait without being processed. The queue's redelivery timeout treats them as stalled, so it redelivers them and counts another delivery each time, and after N deliveries they are dead-lettered without a single real attempt.

**Why it's subtle.** The broker counts deliveries, not attempts, and its timeout starts at delivery, not when work starts. Two settings that are each sensible interact badly.

**Decision.** A documented deployment constraint: the pause must be shorter than the redelivery timeout. The real values are tuned from production.

**Shows:** reasoning about interactions between independent timeouts. Background: §2.3.

---

## C. Concurrency

### C1. Where backpressure lives, and a check-then-act race

**Problem.** LLM calls are capped by a semaphore. The spec didn't say whether dispatching work waits for it to finish or spawns it in the background. Its two descriptions also disagreed on whether the rate-limit pause is checked before or after a worker takes a slot.

**Why it's subtle.** Both read as implementation details, but they aren't:
- **Spawning in the background** puts no limit on how much is read. Work piles up in memory, and its redelivery timeouts keep running (see B6).
- **Checking the pause before taking a slot** is a time-of-check/time-of-use race. A worker sees "not paused" and waits for a slot. Meanwhile another worker hits the rate limit. The first worker then starts *during* the pause.

**Decision.** Dispatch waits, and the reader processes one batch at a time. The pause is checked after taking the slot, in a loop, right before the call.

**Shows:** precision about concurrency semantics, and finding races in a spec before they become code. Background: §2.1.

### C2. A concurrency cap that mostly limited idle workers (open, by choice)

**Problem.** Aggregation runs one serial worker per group, so a story is only ever updated by one task at a time. Workers are capped by a semaphore, but each worker holds its permit for its whole life, including the idle time before it shuts down. On a busy morning with more active stocks than permits, a new group waits for a permit inside the read loop, and that same loop also triggers flushes. So intake and saving stall together. Nothing is lost, but the output lags.

**Why it's subtle.** The cap looks like a resource limit, but it counts live objects, not running work. And waiting for it on the read path turns a capacity issue into "data stopped being saved".

**Decision.** A deliberate scope call:
- **For the first release:** size the cap above the measured peak number of active groups.
- **Deferred, with a written plan:** the proper fix (hold a permit only while working) changes the worker lifecycle. It is documented as a known limitation, with the metric to watch after release.

**Shows:** scoping a fix, and shipping a documented mitigation instead of an unmeasured redesign. Background: §2.6.

---

## D. Domain semantics

### D1. A late article splitting a live story

**Problem.** A new story starts whenever a mention falls outside the time window, including when it is *older* than the live story (a crawler backfilling a day late). The late mention would take the group's only live slot and close the live story early. The next current article would then start a third story, so one story would be split into pieces.

**Why it's subtle.** Out-of-order handling was designed for small timing skews (taking min/max of timestamps), not for an article a day late.

**Decision.** Log and skip late mentions for now. Merging them into the older story they belong to waits until the logs show how often this happens.

**Shows:** handling a rare case simply, and adding instrumentation before building for it.

---

## Appendix — Development tooling

- **uv** manages the virtualenv and pinned dependencies: much faster than pip, and the repo already uses it.
- **pytest + pytest-asyncio** run the tests, the same as the sibling service.
- **ruff** lints and formats in one tool, replacing flake8, isort and black.
- **mypy (strict)** enforces the repo rule that any value crossing a module boundary is strongly typed.

All four are configured in the service's `pyproject.toml`. Test-only dependencies are pinned in `requirements-test.txt`.

---

# Part 2 — Topics for future investigation

## 2.1 Bounded concurrency in a consumer

A consumer reads messages and processes them, often calling something slow or rate-limited (an LLM, a third-party API, a DB). It needs a concurrency cap so it doesn't overload that dependency. The core question is **where the backpressure lives**: what stops the reader from pulling more messages than the workers can handle?

### Three dispatch models

| Model | How it works | Backpressure | Main problem |
|---|---|---|---|
| **A. Batch and wait** | Read a batch, process it concurrently (up to the cap), wait for all of it, then read again. | The read itself. At most one batch in hand. | **Head-of-line blocking.** The slowest message in a batch holds up the next read while other slots sit idle. |
| **B. Acquire a slot, then spawn** | Wait for a free slot *before* taking the next message, start its work in the background, and keep reading. | The slot limit, enforced before reading. At most `cap` messages in flight. | **Task lifecycle.** Background work lives outside the reader. Shutting down or restarting the consumer must also cancel and wait for that work, or orphans keep running next to the new instance. |
| **C. Spawn everything** | Every message read becomes a task immediately. Tasks queue on the concurrency limit. | None. The reader is never slowed down. | **Unbounded queue.** Messages pile up in memory waiting for a slot. With brokers that track per-message timeouts, this causes a second problem (§2.3.2). |

Takeaways:
- **A semaphore limits concurrency, not queue length.** Model C looks bounded but isn't. A cap only creates backpressure if the reader waits for it.
- Model A is the simplest correct option and keeps *structured concurrency*: all work is a child of the loop, so cancellation propagates naturally. You pay in throughput. A batch size close to the cap reduces the cost.
- Model B is the classic worker-pool shape and keeps every slot busy. Structured-concurrency tools (Python's `asyncio.TaskGroup`, Trio nurseries, Kotlin coroutine scopes, Java's `StructuredTaskScope`) give B's throughput without losing A's cancellation guarantees.
- Thread-pool worlds have the same three models. Java's `ThreadPoolExecutor` with an unbounded queue is model C. A bounded queue with `CallerRunsPolicy` turns it into backpressure.

### Pausing a pool, and check ordering (a TOCTOU race)

When a downstream dependency says "slow down" (HTTP 429, quota exceeded), the right reaction is usually a **pool-wide pause**: one worker hitting the limit means the others will hit it too. A shared `paused_until` deadline that every worker respects before starting work is the usual approach.

There are two possible orders:
1. **Check the pause, then wait for a slot.** A worker checks (not paused) and waits for a slot. Meanwhile another worker hits the limit and sets the pause. When the first worker gets the slot, it starts immediately, *during* the pause. This is a classic time-of-check vs time-of-use (TOCTOU) race.
2. **Get the slot, then check in a loop until the deadline has passed.** The check happens right before the work. The loop also covers a second trigger extending the deadline mid-sleep. Holding a slot while sleeping is harmless, because the whole pool is paused anyway.

General rule: **check a condition as close as possible to the action it guards, with nothing that can yield in between.** The same rule is behind any check-then-insert dedup guard.

SAPI: §C1.

### To investigate
- Structured concurrency: what it guarantees, and why "no orphan tasks" is a correctness property, not just tidiness.
- Adaptive concurrency: AIMD (additive increase, multiplicative decrease, as in TCP congestion control) and Netflix's `concurrency-limits` library, i.e. tuning the cap from observed latency and errors instead of a fixed number.
- Rate limiting vs concurrency limiting: token bucket / leaky bucket (requests per second, which is what most API quotas measure) vs a semaphore (requests in flight). When you need both.
- Backoff strategies: exponential backoff with jitter, and the "thundering herd" when every worker wakes at the same instant once a shared deadline expires.
- Circuit breakers (closed / open / half-open) as a more general version of a pool-wide pause.

---

## 2.2 Delivery guarantees

Every message system promises one of three things. The difference is **when the consumer acknowledges** a message relative to **when it does the work**.

### At-most-once: acknowledge first, then process
- A crash after the ACK but before the work finishes **loses the message**. Nothing redelivers it.
- No duplicates, and the simplest to build.
- Fits data where loss is acceptable: metrics samples, best-effort cache refreshes, telemetry.

### At-least-once: process first, then acknowledge
- A crash after the work but before the ACK means **the message is delivered again**, so the work runs twice.
- Nothing is lost, as long as unacknowledged messages are eventually redelivered.
- The default for most production pipelines. Problems this model brings, and must solve:
  - **Duplicates.** Every handler must be idempotent, or protected by deduplication.
  - **Poison messages.** A message that always fails is redelivered forever, blocking or wasting capacity. You need a retry limit + dead-letter queue (DLQ).
  - **Redelivery storms.** A transient outage leaves many messages unacknowledged, all redelivered together once it recovers, which can push the system straight back into overload.
  - **Duplicates passed downstream.** If the handler publishes to another queue before acknowledging, a crash between the publish and the ACK publishes again. The duplicate propagates, so every downstream consumer must also be idempotent.
  - **Ordering.** Redelivery breaks ordering: a retried message can be processed after newer ones.

SAPI is at-least-once at every layer: Redis Streams consumer groups, `XACK` only after a terminal outcome, and a retry limit routing to each layer's own `stream:*_dead_letter`.

### Exactly-once: really "effectively once"
- True exactly-once *delivery* is impossible over an unreliable network: a sender can't tell "message lost" from "acknowledgement lost". This is the Two Generals problem.
- What systems actually provide is **exactly-once effect**: at-least-once delivery + a handler where running twice has the same result as running once.
- Common ways to get it:

| Technique | How it works | Strength |
|---|---|---|
| **Idempotency key + unique constraint** | The handler's write is keyed on a natural or message ID; the DB rejects the duplicate (`INSERT … ON CONFLICT DO NOTHING`). | Strong: survives restarts. Usually the real guarantee. |
| **In-memory in-flight set** | Skip a message whose ID is already being processed. | Weak: only catches concurrent duplicates in one process, lost on restart. An optimisation, not a guarantee. |
| **Inbox / processed-IDs table** | Record each processed message ID in the same transaction as the business write; skip if already present. | Strong, and generic. Costs an extra write per message. |
| **Conditional writes / version checks** | Only write if the data's version still matches what was read (optimistic concurrency). | Strong for "latest state wins" data. |
| **Transactional outbox** | Write the business row and an "outgoing message" row in one DB transaction; a separate relay publishes from the outbox. | Removes the "saved but not published" crash window on the producer side. |
| **Broker transactions** | e.g. Kafka's transactional producer commits consumer offsets and output messages atomically. | Strong, but only while every effect stays inside the broker. An external API call is still at-least-once. |

SAPI: §B1 (an idempotent insert hiding a lost publish), §B4 (a version check that has to mean "newer", not just "different").

### Side effects you can't make idempotent
Some work can't be safely repeated or undone: an LLM call costs money, an email is sent, a payment is charged. Ways to handle it:
- Pass an **idempotency key to the provider** if it supports one (Stripe-style APIs do).
- **Record intent before acting, and the result after**, so a retry can see that the call already happened.
- Accept the duplicate cost, but bound it with a retry limit.

### To investigate
- The Two Generals problem and the FLP impossibility result: what they say and what they don't.
- Kafka's idempotent producer and transactions (EOS), and exactly where their guarantee stops.
- The transactional outbox and the inbox pattern, with change-data-capture (e.g. Debezium) as the outbox relay.
- The saga pattern for multi-step workflows where some steps can't be rolled back.
- Designing idempotency keys: natural keys vs message IDs, and what happens when the same logical event arrives under two different message IDs.

---

## 2.3 Redelivery mechanics: leases and timeouts

At-least-once brokers need a way to decide that a consumer has "given up" on a message, so it can be handed to someone else. Almost all of them use a **lease**: a message handed to a consumer is reserved for a time window. If it isn't acknowledged within that window, it becomes available again.

| Broker | Lease mechanism | Heartbeat / extension |
|---|---|---|
| Redis Streams | Pending Entries List (PEL) with an idle timer per message; another consumer reclaims with `XCLAIM`/`XAUTOCLAIM` after `min_idle_ms`. | Re-`XCLAIM` the message to yourself, which resets its idle time. |
| AWS SQS | Visibility timeout: the message is hidden from other consumers until it expires. | `ChangeMessageVisibility` |
| Google Pub/Sub | Ack deadline | `ModifyAckDeadline` (client libraries extend it automatically) |
| RabbitMQ | The message is redelivered when the consumer's channel closes; plus a configurable consumer ack timeout. | Keep the connection alive (no per-message extension). |
| Kafka | No per-message lease. Each partition is owned by one consumer, which must poll again within `max.poll.interval.ms` or it is removed from the group and its partitions are reassigned. | Poll regularly; do slow work elsewhere and pause the partition. |

### 2.3.1 Delivery count vs attempt count
Most brokers count **deliveries**: how many times a message was handed out. The retry limit and DLQ decision are usually based on that count. But a delivery is not always a real attempt. A message can be delivered, sit unprocessed, time out, and be delivered again, without the handler ever running.

### 2.3.2 The lease trap
The lease clock starts **when the message is delivered**, not when work on it starts. Anything that delays the start of work uses up the lease:
- **Queuing inside the consumer.** With dispatch model C (§2.1), messages wait in memory for a slot. Under load they outlive the lease, get redelivered *while still queued*, and their delivery count climbs. Eventually **healthy messages are dead-lettered** without ever failing.
- **Pausing the consumer.** During a pool-wide pause (§2.1), messages already received sit idle. If the pause is longer than the lease, they get redelivered and counted too. Repeated pauses can push them to the DLQ.
- **Slow processing.** Work that takes longer than the lease is redelivered to another consumer while the first is still running. The result is two concurrent copies of the same work.

Ways around it:
- Bound the time between receiving a message and starting its work: only take messages you can start soon (model A or B).
- Set the lease well above the longest expected wait + processing time, including any pause.
- Extend the lease for long-running work (the heartbeat column above).
- Base the DLQ decision on **real attempts**, tracked by the application, not the broker's delivery count.
- On a pause, stop reading new messages, so the pause doesn't age messages you've already taken.

SAPI: §B6.

### To investigate
- How each broker in the table really behaves at the edges: what happens to in-flight work when a lease expires, and whether the old consumer finds out.
- Fencing tokens (Martin Kleppmann's distributed-locking critique): stopping a "zombie" consumer whose lease expired from writing stale results.
- Kafka consumer-group rebalancing, and cooperative vs eager rebalance protocols.
- DLQ operations: how to inspect, fix, and replay dead-lettered messages safely, and what "safely" means once the original order is lost.

---

## 2.4 Succeed once, retry on failure

The practical goal behind §2.2's "effectively once": **a message's work must succeed at most once, yet a message that failed must still be redelivered and processed again.** These two requirements pull against each other. Any guard strong enough to stop a second success can also block a legitimate retry, and any guard loose enough to allow retries can let a second success through.

### Three states, not two

For any message ID, "has it been processed?" has three answers, not two:

| State | Meaning | Where it must live |
|---|---|---|
| **Done** | Succeeded. Never process again. | **Durable** storage, written atomically with the work's effect. |
| **In progress** | Someone is working on it right now. | Anywhere, but it **must be released or expire** on every exit path. |
| **Not done** | Never tried, or tried and failed. | The default: the absence of the other two. |

Almost every bug here comes from mixing up "in progress" and "done":
- **In-progress treated as done.** An in-progress marker that's never released (a crash, or a failure path that forgets to clear it) blocks every retry. A transient failure becomes a permanent loss.
- **Done treated as in-progress.** A "done" marker kept only in memory disappears on restart, so the work runs again.

### Patterns, from strongest to weakest

**1. Make the effect naturally idempotent.** If running twice has the same result as running once, no guard is needed for correctness. Examples: `UPSERT` instead of `INSERT`, adding to a set instead of appending to a list, "set X to 5" instead of "add 5 to X". Prefer this whenever the effect allows it. Everything below exists for effects that can't be made idempotent.

**2. A "done" marker in the same transaction as the effect** (the inbox pattern, or an idempotency key with a unique constraint).
```
BEGIN
  INSERT INTO processed_messages (id) VALUES ($1)   -- unique: a conflict means already done → skip
  <business write>
COMMIT
ACK
```
Success commits both, so the message is never processed again. A failure or crash rolls back both, so the redelivery retries cleanly. This is the standard answer, but it **only works when the effect and the marker live in the same transactional store**. A natural key on the business row itself (e.g. `UNIQUE (source_url, stock_code)`) can serve as the marker without a separate table.

**3. A durable claim with a lease**, for effects that can't share a transaction with the marker (external API calls, long-running jobs). Each message gets a record with `status`, `owner`, `lease_until`:
- **Claim** with one conditional write: `UPDATE … SET status='processing', owner=$me, lease_until=now()+T WHERE id=$1 AND (status='failed' OR (status='processing' AND lease_until < now()))`. Zero rows updated means someone else holds it, or it's done.
- **Success:** `status='done'`. **Failure:** `status='failed'`, so it's immediately claimable again. **Crash:** the lease expires, and the next claim takes over.
- Add **fencing tokens** (an attempt number that rises with each claim, checked on the final write), so a worker whose lease expired can't overwrite a newer attempt's result. Stripe-style idempotency keys and most job schedulers work this way.

**4. An in-memory in-flight set: for concurrency only, never for correctness.** A cheap layer in front of patterns 2 or 3 that stops two copies running at the same moment in one process (the same message read twice, or reclaimed while still queued). It must follow the in-progress rules above:
- **Release it in a `finally`**, not on success, when the work runs in one scope.
- When work is handed off asynchronously (queued for a worker, run by a scheduler), pair the success callback with an **explicit failure callback**. Both release; only success acknowledges.
- Optionally give entries a **timeout**, longer than the longest legitimate processing time, so a leaked entry can't block a message forever.
- Be careful if the set lives on an object that survives a restart of the processing loop: stale entries survive with it.

**5. Effects that can't be undone or safely repeated** (charging a card, sending an email, a paid LLM call): **record intent before acting and the result after**, so a retry can see the call already happened. Pass an **idempotency key to the provider** if it supports one. Otherwise accept a bounded number of duplicates, capped by the retry limit.

### Why the release path is easy to get wrong
- The success path is the one you test. A leaked in-progress marker only shows up when something fails, then recovers, then the message is redelivered.
- Release often happens far from where the marker was set: a callback, on another task, after the work finishes. Each exit path needs its own release, and the failure paths are easy to forget.
- When work is handed off asynchronously, a failure inside that work may never reach the code that owns the marker.
- **Test the redelivery path explicitly:** fail once, recover, redeliver, and assert that the second delivery actually runs and succeeds only once.

SAPI: §B1 (patterns 2 and 4 together in Entity Analysis), §B3 (pattern 4 without a failure callback).

### To investigate
- The inbox pattern vs idempotency keys vs natural keys: when a separate processed-messages table is worth its extra write.
- Lease-based claims in practice: job-queue tables using `SELECT … FOR UPDATE SKIP LOCKED`, and how Stripe's idempotency keys handle a request still in progress.
- Fencing tokens (Martin Kleppmann's distributed-locking critique) and why a lease alone isn't enough.
- Distributed in-flight guards (a Redis `SET NX` with a TTL), and the expiry trade-off: too short and two workers run at once, too long and a crash blocks retries.
- How frameworks handle it: Celery's `acks_late`, Sidekiq's unique-jobs locks, Temporal's activity retries and heartbeats.
- Monitoring: alerting on messages whose delivery count rises while no processing attempt is logged.

---

## 2.5 Writing to a database and publishing a message together (the dual-write problem)

A service often has to do two things in response to one event: **save a change to its database and tell other services about it** (publish to a queue or stream). If both must happen, or neither, you have the *dual-write problem*. It appears whenever two systems don't share one transaction: a SQL database and a message broker, a database and a cache, a database and a search index, or a store with weak transactions (most NoSQL stores across documents).

### Why there's no simple fix

There is no shared commit. Whatever order you choose, a crash between the two steps leaves them disagreeing:

| Order | Crash between the steps leaves… | Consequence |
|---|---|---|
| **Write, then publish** | Data saved, message never sent | Downstream never learns about the change: a **silent loss**. A retry often can't tell the message is missing, especially if the write is idempotent and the retry is a no-op. |
| **Publish, then write** | Message sent, data not saved | Downstream acts on a change that doesn't exist. |
| **Publish inside the open transaction, before commit** | Message sent, commit fails | Same as above, plus a race even without a crash: a fast consumer can read the message and query the row *before the commit is visible*, and act on the old data. |

Distributed transactions (two-phase commit, XA) would solve it in theory, but most brokers and caches don't support them (Redis, Kafka without its own transactions, SQS), and where they exist they're slow and operationally fragile. In practice they're avoided.

### The standard answers

**1. Transactional outbox.** Record "this needs publishing" **in the same database transaction** as the change, then publish from that record afterwards.
```
BEGIN
  <business write>
  INSERT INTO outbox (payload) …          -- or set a "pending" column on the business row
COMMIT
-- later (same process right after commit, or a separate relay):
publish(payload); mark the outbox row as sent
```
The database transaction makes "changed" and "needs publishing" atomic. A crash before publish leaves the outbox row, so the relay publishes it on the next pass. A crash after publish but before marking it sent causes a **duplicate**, so delivery is **at-least-once** and consumers must be idempotent (§2.2, §2.4). Variants:
- A separate `outbox` table, or a column on the business row (e.g. `published_version` vs `current_version`; the row is pending while they differ). The column works when "latest state" is all that matters; a table is needed when every individual event must go out.
- The relay can be the same code path right after commit, a periodic sweeper, or both (publish immediately, and sweep for anything a crash left behind).

**2. Change data capture (CDC).** Don't publish from application code at all. A tool reads the database's own change log (Postgres WAL, MySQL binlog) and turns committed changes into messages. Debezium is the common example. Atomicity is natural (only committed changes appear), at the cost of extra infrastructure. Often combined with an outbox table, so the published messages have a deliberate shape rather than mirroring table rows.

**3. Make one system the source of truth, and derive the other.** If the downstream can rebuild its view from the database (re-read the latest row, re-index periodically), a lost message only delays it. Event sourcing turns this around: the event log *is* the source of truth, and database tables are derived from it.

**4. Listen-to-yourself.** Publish first, and have the same service consume its own message to perform the database write. The broker becomes the source of truth, at the cost of a delay before your own write lands.

Takeaways:
- **"In the same transaction" really means "recorded in the same transactional store."** You can't make two systems commit together, but you can make the *intent to publish* commit together with the data.
- The outbox gives **at-least-once**, never exactly-once. Pair it with idempotent consumers.
- Watch for **"the retry is a no-op, so nothing gets re-sent"**: an idempotent write that skips unchanged rows, combined with "publish only what I just wrote", silently drops messages lost to a crash. The pending marker is what breaks that link.

SAPI: §B1, §B2.

### To investigate
- Outbox relay designs: polling with `SELECT … FOR UPDATE SKIP LOCKED`, Postgres `LISTEN/NOTIFY`, or CDC on the outbox table. Ordering guarantees with multiple relay instances.
- Debezium's outbox event router, and the cost of running CDC infrastructure.
- Kafka transactions (producer + consumer offsets in one transaction) and where they stop helping: once a database outside Kafka is involved.
- The same problem for caches: cache-aside invalidation races (write DB, delete cache key) and why "delete after commit" plus a short TTL is the usual compromise.
- Sagas: when one business operation spans several services' databases, and compensating actions replace rollback.

---

## 2.6 Per-key serial workers: what the concurrency cap actually limits

Some work must run **one at a time per key, but concurrently across keys**: updates to the same account, the same document, the same aggregate. The usual shape is a worker (an actor, a queue, a coroutine) per active key, created on demand and torn down when idle. Kafka partitions, Akka/Orleans actors, and "keyed" executors all follow this pattern.

A cap is still needed, so that a burst of distinct keys doesn't spawn unbounded workers. The trap is **what the cap counts**.

### Counting workers vs counting work

| Cap counts… | A permit is held… | Behaviour under a burst of distinct keys |
|---|---|---|
| **Live workers** | For the worker's whole life, including while it sits idle waiting for the idle timeout | Permits run out even though almost nothing is running. New keys wait until some *idle* worker times out. Throughput collapses to "one new key per idle timeout". |
| **In-flight work** | Only while a unit of work is executing | Idle workers cost nothing. New keys proceed as soon as any work finishes. |

Counting live workers looks like a resource limit, but most of what it limits is idleness. It also couples two unrelated settings: the cap now has to exceed the peak number of keys active within any idle-timeout window, so a longer idle timeout (chosen to avoid worker churn) silently shrinks effective capacity.

### Don't block the reader on it

Where the wait for a permit happens matters as much as what the permit counts. If the **reader loop** waits for a permit, everything else that loop does waits too: reading new messages, and any timer-driven housekeeping it runs (flushes, heartbeats, lease extensions). A capacity problem in one place then shows up as a stall somewhere unrelated, such as "data stopped being persisted".

Ways around it:
- Count in-flight work instead of live workers, or give idle workers no permit at all.
- Evict idle workers early under pressure (an LRU of idle workers) instead of only on a timeout.
- Run housekeeping on its own timer or task, so it can't be starved by the read path.
- If the reader must wait, wait with a deadline, and run housekeeping while waiting.

General rule: **a concurrency limit should guard the scarce resource itself** (connections, CPU, an API quota), **not a proxy for it** (number of objects alive).

SAPI: §C2.

### To investigate
- How actor runtimes bound per-key actors: Akka's passivation, Orleans' grain deactivation and activation limits.
- Kafka's model, where the key→partition mapping fixes concurrency at the partition count instead of per key, and its trade-offs (hot partitions, rebalancing).
- Keyed executors and striped locks (e.g. Guava's `Striped`), which serialise per key with a fixed pool and no per-key workers.
- Measuring it: peak live workers vs permits, and time spent waiting for a permit on the read path.

---

## 2.7 Matching names against a reference list

Mapping a free-text mention ("騰訊", "Tencent", "騰訊控股有限公司") to exactly one record in a reference list is called *entity linking* or *record linkage*. It comes up with company names, people, addresses and product catalogues. Three questions decide most of the result: how you normalize, which matcher shape fits how the names actually vary, and what a wrong answer costs compared with no answer.

### Normalize first

Normalization often does more than the matcher. In the fuzzy-matching spike, exact match after normalization alone was right 126 times on the dev half with zero wrong answers. Stripping legal suffixes added 19 more, still with none wrong. Typical steps:
- **Script and width folding:** traditional→simplified (OpenCC), full-width→ASCII (Unicode NFKC does the width part), casefolding.
- **Variant characters** that NFKC doesn't fold (恆/恒, 滙/匯).
- **Legal suffixes** (有限公司, Limited, Co., Ltd.) and **generic tails** (控股, 集團, Holdings, Group).
- **List-specific markers and duplicate rows** (HKEX's `－Ｒ`/`－Ｔ` counters).

Apply the *same* function to both sides, and prepare the list side once at load, not on every call.

### Pick the matcher that models how names vary

| Family | Handles well | Fails on |
|---|---|---|
| Exact after normalization | spelling variants the normalizer folds | anything shortened |
| Edit distance (Levenshtein, Jaro-Winkler) | typos, one-character differences | abbreviations: 農行 vs 農業銀行 is a large edit |
| Token measures (`token_set_ratio`, `WRatio`) | reordered words, Latin names | CJK text has no word boundaries |
| Character n-gram TF-IDF + cosine | fast approximate search over large lists | short queries share n-grams with many names |
| **Ordered subsequence** | **Chinese abbreviations, which keep characters in order from anywhere in the name** | short real names inside longer ones (瑞銀 ⊂ 華泰瑞銀) |

General rule: **choose the similarity measure from the process that generates the variation** (typing errors, abbreviation, translation, reordering), not from what a library offers by default.

### A wrong answer costs more than no answer

A miss drops one mention. A wrong match invents a false entity that flows on silently. Score matchers with an asymmetric utility (the spike used correct − 2 × wrong), and prefer designs that can say "don't know":
- **A tie is a miss.** Breaking ties by lowest stock code turned 9 misses into 3 right answers and 6 wrong ones, so it was rejected.
- **Who else can recover a miss?** In Entity Analysis the article's printed code and the model's own knowledge come first, so the name search only needs to be right when it answers, not to answer often.
- **Measure the component inside its pipeline.** §4.2's matcher looked mediocre in isolation, but only end-to-end F1 showed it was *worse than nothing* (§A3).

### What rules can't do
- Irregular abbreviations (中海油 = 中國海洋石油) need an **alias table**: the standard remedy, but it needs upkeep, and can't be tested fairly on the data it was seeded from.
- Cross-language names need the code, the other-language name, or a knowledge base.

### Cost
Precompute the comparison forms at load, pre-filter candidates cheaply (here, names whose characters aren't nested with the query's), and index where you can (English word prefix → stocks). S8 runs at p99 ~5 ms over 2,798 stocks, small enough to stay on the event loop.

SAPI: §A3.

### To investigate
- Record-linkage theory: the Fellegi–Sunter model, and blocking/indexing for large lists.
- Unicode normalization forms (NFC vs NFKC), CJK compatibility and variant characters, and OpenCC's configurations (`t2s` vs `hk2s`).
- Learned entity linking: dense retrieval plus a cross-encoder re-ranker, and when it beats rules on a list this small.
- Alias tables and knowledge bases (Wikidata company aliases): sourcing, upkeep, and evaluating them without leakage.
- Romanization and pinyin matching for names that appear only in English.
- Precision–coverage curves as a way to choose an operating point for a matcher.

---

## 2.8 LLMs in a pipeline: extract with the model, verify in code

An LLM step in a data pipeline is best treated as an unreliable but capable extractor whose output code checks. The design question is **which part only the model can do, and which part code can do deterministically.**

### Split the work
- **The model:** read prose, decide which companies an article is about, classify events and sentiment, recall facts it knows (an HKEX code).
- **Code:** check that the code exists, that the name agrees with the listed name, that the article actually prints the code, that enums and lengths are valid, and dedup. These checks are cheap, deterministic and testable, and they catch hallucinated entities.
- **Ground the output in the source text** wherever possible (the "article prints the code" check). It is the cheapest hallucination detector there is.

### One structured call or a tool loop?

| | One structured-output call, then code checks | Tool-calling loop |
|---|---|---|
| Round trips | one | one per round, plus a final answer |
| Cost and latency | bounded and predictable | grow with how dense the input is |
| Termination | trivial | must be designed: forced-call modes never stop, free modes can repeat calls, so you need round caps |
| Needed when | the answer can be checked after the call | the model must fetch information it can't know, and it can't be checked afterwards |

Rule: **if verification can run after the call, run it after the call.** A tool earns its place when it gives the model information it needs to *reason* further, not when it only confirms something code can confirm.

### Choosing a model
- **Set a ceiling first:** run the strongest model against itself. A gap smaller than its self-disagreement isn't a quality difference.
- **Then compare what the pipeline pays for:** cost per unit of work (with thinking tokens billed as output), tail latency (p95 and max, not the mean), and failure rate under real load and a shared quota.
- Choose the **smallest model that clears the bar**, and route to a bigger one only for a slice where routing measurably helps.

### Schema-valid is not correct
Structured output guarantees the shape: fields, types, enums. It doesn't guarantee the content. Keep business validation (length limits, allowed combinations, cross-field rules) in code after the call, and decide per rule whether to fix (truncate) or reject.

SAPI: §A1, §A2.

### To investigate
- Constrained decoding: how JSON-schema output is enforced at the token level, and what it does to quality.
- Evaluating LLM output: agreement ceilings, Cohen's kappa, and LLM-as-judge pitfalls (position and self-preference bias).
- Model cascades and routing (e.g. FrugalGPT): when a cheap-first, escalate-on-doubt design pays off.
- Prompt and skill versioning in production, and how a prompt change is rolled out and compared (design.md §9.1.1).
- Thinking or reasoning budgets: when they help extraction tasks, and what they do to cost and tail latency.

---

## 2.9 Calling a hosted LLM API in production

A hosted LLM is a slow, rate-limited, paid, non-deterministic remote dependency. Everything that applies to a remote API applies, and a few things are specific to LLMs.

### Timeouts and retries
- **Always set a timeout explicitly.** An SDK default can be "none", and one hung call then holds a worker for minutes.
- **Know who retries.** The SDK (`retry_options`), a wrapper library, and your own code can each retry. Pick one owner, or the attempts multiply. Libraries can also retry in ways the provider rejects (Instructor's model-turn re-ask, §A5).
- **A paid call is a side effect you can't undo** (§2.4, pattern 5). Cap attempts, and avoid redoing a call whose result was already stored (§B1).

### Classify errors by what to do next

| Error | Meaning | Action |
|---|---|---|
| 429 | rate limit or quota | pause the whole pool, then retry (§2.1) |
| 5xx / 504 | provider trouble or a deadline hit | retry with backoff, bounded |
| other 4xx, schema failures | the request itself is wrong | don't retry; fix the code or prompt |
| 401 / 403 | credentials or project | alert a human |

SAPI's mapping is in impl.md §7.2.

### Tokens, quotas and inputs
- **Count all three token kinds:** input, visible output and thinking. Thinking counts against `max_output_tokens`.
- **Know the quota model.** Fixed RPM gives you a number to size concurrency against. A dynamic shared pool returns 429s at low concurrency and gives no such number. Provisioned throughput buys a guarantee. Also check which regions actually serve the model.
- **Bound the input before the call.** Real documents can be orders of magnitude larger than test ones.
- **Check feature thresholds before counting on them.** Context caching has a minimum prompt size.

SAPI: §A5.

### To investigate
- Provisioned throughput vs on-demand pricing, and the break-even volume.
- Hedged requests for tail latency, and why they're risky for paid, non-idempotent calls.
- Batch prediction APIs: cheaper asynchronous processing for work that isn't latency-sensitive.
- LLM observability: per-call token, latency and cost logs, and alerting on cost drift.
- Provider fallback (a second model or region) and how it interacts with evaluated quality.

---

## 2.10 Running a spike as a measured experiment

A spike answers a design question with evidence. Both Entity Analysis spikes worked because they were run like small experiments, and the PR review of one showed how easily such an experiment drifts (§A4).

### Build the test set from real data
- **Look for labels the data gives you for free:** news articles print `name(code)`, and HKEX announcements print the issuer's code in the header. Foreign tickers (`name(TICKER.US)`) give negatives, cases where the right answer is "no match".
- **Add hand-collected hard cases** (abbreviations, ambiguous prefixes, A/H names), and adjudicate by reading wherever the automatic gold has gaps.

### Keep tuning data and judging data apart
- **Split dev and test by a hash of the query or the company,** so the same company never sits in both halves. Tune on dev; look at test only to confirm.
- **Score a held-out set exactly once,** on a frozen version, against a pass bar written down *before* scoring. The fuzzy-matching spike's bar was "more right than `WRatio`, and at most 3% wrong".
- **Reserve untouched data for whoever implements the design** (here, 288 articles neither spike read).
- **Record rejected rounds with their numbers** (S9, S10), so nobody has to re-run them to know why they were dropped.

### Measure in context, cheaply
Component accuracy and end-to-end accuracy can disagree (§A3). **Store raw model outputs, then replay them offline** with one component swapped: the fuzzy-matching spike compared five fallbacks on the LLM spike's stored outputs with no API calls and no LLM randomness between variants.

### Make every figure reproducible
- Freeze the inputs: prompt copies, list snapshots, commit hashes.
- Derive every split and number from a committed script, never from a hand edit.
- State one denominator per metric (per attempt vs per success).
- Scan constructed test sets for leaked labels.
- Re-score after any change to a shared component.
- Commit no third-party text, and gitignore raw exports.

### Small samples
127 articles is small. 0.954 vs 0.962 is a handful of entities. Read the actual errors behind a difference before believing the score, and treat gaps within the reference model's own disagreement as ties.

SAPI: §A1–§A5.

### To investigate
- Bootstrap confidence intervals and paired tests (McNemar's test) for small evaluation sets.
- Overfitting to a dev set through repeated tuning: adaptive data analysis, the "garden of forking paths", and reusable holdouts.
- Pre-registration: why fixing the pass bar in advance matters.
- A taxonomy of data leakage (Kaufman et al., "Leakage in Data Mining").
- LLM evaluation tooling (promptfoo, Inspect, OpenAI Evals) vs the hand-rolled scripts used here.

---

## Further reading
- Martin Kleppmann, *Designing Data-Intensive Applications*: chapters 8 (distributed system problems), 9 (consistency), 11 (stream processing).
- Gregor Hohpe & Bobby Woolf, *Enterprise Integration Patterns*: Idempotent Receiver, Dead Letter Channel, Guaranteed Delivery, Competing Consumers.
- Confluent's posts on Kafka exactly-once semantics.
- microservices.io (Chris Richardson): Transactional Outbox, Idempotent Consumer, Saga.
- Chip Huyen, *AI Engineering*: the chapters on evaluation methodology and on building inference pipelines around a model.
- Peng, Yu & Dredze, "An Empirical Study of Chinese Name Matching and Applications" (ACL 2015), and the study of Chinese abbreviations as ordered subsequences at [arXiv 1712.06289](https://ar5iv.arxiv.org/html/1712.06289): the two papers behind the fuzzy-matching spike's direction.
- Kaufman, Rosset & Perlich, "Leakage in Data Mining: Formulation, Detection, and Avoidance" (KDD 2011).
