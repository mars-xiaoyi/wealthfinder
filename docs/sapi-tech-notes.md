# SAPI — Tech Notes

Learning notes from designing SAPI, the Redis Streams–based pipeline in this repo (Entity Analysis → Aggregation → Scoring → Cache). SAPI's binding design lives in `stock-assistant-pipeline-intelligence/docs/` (`impl.md` = `implementation.md`, `design.md` = `system-design.md` below); nothing here is a design decision for it.

- **Part 1** lists concrete problems found while designing SAPI: what went wrong, why it was easy to miss, and the fix. §1.11–§1.16 come from the two Entity Analysis spikes (`lookup_stock` fuzzy matching, the EntityAnalysisSkill LLM design), whose full write-ups are `poc/sapi/fuzzy-match/README.md` and `poc/sapi/llm-skill/README.md`. Part 1 ends with the project's development tooling (§1.17).
- **Part 2** covers the general topics behind them, for future investigation. Each ends with questions worth digging into.

---

# Part 1 — Tech items highlighted from SAPI

## 1.1 Wait or spawn, and when to check a pause

`WorkerPool.dispatch()` caps concurrent Entity Analysis LLM calls with an `asyncio.Semaphore`. The spec didn't say whether `dispatch()` waits for the work or spawns it in the background, and its docstring and prose disagreed on whether the rate-limit pause is checked before or after acquiring a slot.
- **Why it was easy to miss:** both read as implementation details, but one choice decides where backpressure lives, and the other is a time-of-check vs time-of-use race: a worker that checked "not paused" before waiting for a slot starts work during a pause.
- **Fix:** `dispatch()` waits for the work, and callers gather a whole batch. The pause is checked after acquiring, in a loop (impl.md §11.3).
- **Topic:** §2.1.

## 1.2 A lost publish that a retry could never recover

Entity Analysis writes an article's EntityEvents to Postgres, then publishes them. A crash between the two left the message unacknowledged. On redelivery the LLM ran again, every insert hit `ON CONFLICT DO NOTHING`, and conflicting rows were dropped from the result, so the never-published EntityEvent stayed unpublished for good.
- **Why it was easy to miss:** the idempotent insert, the thing that made redelivery "safe", is exactly what hid the missing publish. And because LLM output isn't deterministic, a rerun may not even produce the same entities.
- **Fix:** write one article's rows in one transaction; before running the LLM, check whether rows already exist for the article, and if so republish their IDs instead of reanalysing (impl.md §11.1 step 3, design.md §3.4). The next layer absorbs the duplicate signal.
- **Topics:** §2.2, §2.4.

## 1.3 A pause longer than the lease dead-letters healthy messages

With `RATE_LIMIT_BACKOFF_S=60`, a backoff longer than `STREAM_CLAIM_TIMEOUT_MS` lets the reclaim loop claim messages read before the pause. Their delivery count rises toward `ENTITY_ANALYSIS_MAX_RETRY` without a single real attempt.
- **Why it was easy to miss:** the broker counts deliveries, not attempts, and the lease clock starts at delivery, not when work starts.
- **Fix:** a deployment constraint, `RATE_LIMIT_BACKOFF_S * 1000 < STREAM_CLAIM_TIMEOUT_MS` (impl.md §11.3); the real values are tuned from production (Q-9).
- **Topic:** §2.3.

## 1.4 An in-progress marker that behaved like "done"

Aggregation keeps an in-memory set, `_enqueued`, so the same EntityEvent isn't processed twice at once. As first designed, an ID was removed only by the success callback. A failed DB read or merge left it behind, so every redelivery was skipped as "already in progress" until it was dead-lettered. A few seconds of DB trouble became a permanently lost EntityEvent.
- **Why it was easy to miss:** the success path is the one you test. The leak shows only when something fails, recovers, and the message is redelivered. The set also lives on an object that survives the layer's auto-restart.
- **Fix:** a failure callback that releases the ID without acknowledging, on every exit path (impl.md §12.1, §12.2).
- **Topic:** §2.4.

## 1.5 Skipping unchanged rows silently dropped publishes

The Aggregation flush upserts every active Event and publishes `stream:event_aggregated` so Scoring re-scores it. To stop re-scoring unchanged Events, the write skipped rows whose `aggregation_updated_at` hadn't advanced, and only written rows were published. A crash between commit and publish then made the next flush a no-op, so the Event was never scored.
- **Why it was easy to miss:** each half was correct on its own. "The retry is a no-op" plus "publish only what I just wrote" is what drops the message.
- **Fix:** a transactional outbox as a column, `events.published_aggregation_updated_at`. The write makes a row pending in the same transaction; every flush publishes whatever is pending, then marks it (impl.md §10.2, §12.1; design.md §4.5).
- **Topic:** §2.5.

## 1.6 An older snapshot overwriting newer state

A flush reads a snapshot of every active Event from Redis, then writes it. In between, a group could merge again and have its Event replaced and persisted with newer state. The flush then wrote the older snapshot over it, and nothing ever corrected it, because the Event's Redis slot now held its successor.
- **Why it was easy to miss:** "write only if it changed" (`IS DISTINCT FROM`) looks like enough, but older is also different.
- **Fix:** write only if newer: `WHERE events.aggregation_updated_at < EXCLUDED.aggregation_updated_at` (impl.md §10.2).
- **Topic:** §2.2 (conditional writes).

## 1.7 One Redis slot per group lost a replaced Event's last merges

Each group keeps one active Event in one Redis key. When an article fell outside the sliding window or broke the hard cap, the new Event overwrote that key, and every merge into the old Event since the last flush was lost.
- **Why it was easy to miss:** the hard-cap path is a normal, frequent case, not an edge case, and the key layout made the overwrite invisible.
- **Fix:** persist and publish the replaced Event before overwriting its key (impl.md §12.1).

## 1.8 A TTL that could expire before any flush

Each merge resets the active Event's TTL to `min(window, remaining lifespan)`. Near the hard cap the remaining lifespan can be seconds, shorter than the flush interval, and `int()` can round it to `0`, which makes `EXPIRE` delete the key at once. Acknowledged merges then vanished unflushed.
- **Why it was easy to miss:** the existing rule (flush interval far below the window) assumed the TTL is about the window, which isn't true near the cap.
- **Fix:** add a grace period of two flush intervals, so every key outlives at least one flush. Merging is unaffected, because the decision comes from the Event's own span (impl.md §12.4).

## 1.9 A late article splitting a live story

`merge_or_create()` starts a new Event whenever an article falls outside the window, including when it's *older* than the active Event. A late backfill article would take over the group's slot, close the live Event early, and make the next current article start a third Event, splitting one story into pieces.
- **Why it was easy to miss:** out-of-order handling was designed for small skews (min/max on timestamps), not for an article a day late.
- **Fix, for now:** log `aggregation_late_entity_event_skipped` and skip it. Merging it into its older Event waits until logs show how often it happens (design.md §4.4).

## 1.10 A concurrency cap that mostly limited idle workers

Aggregation runs one `GroupCoroutine` per `(exchange, stock_code, event_type_primary)` group, and each live group holds one `AGG_MAX_CONCURRENT` permit until it has been idle for `AGG_GROUP_IDLE_TIMEOUT_S` (impl.md §12.1, §12.3). A morning with more distinct active stocks than permits makes `process_entity_event()` wait for a permit inside the consumer loop, which also runs the flush triggers (§12.2), so intake and flushing stall together. Nothing is lost, but the output lags.
- **Why it was easy to miss:** the cap looks like a resource limit, but it counts live objects, not running work, and waiting for it on the read path turns a capacity issue into "data stopped being persisted".
- **Status:** open — size `AGG_MAX_CONCURRENT` above the peak number of active groups, and measure it in production.
- **Topic:** §2.6.

## 1.11 A fuzzy matcher that was worse than no matcher at all

impl.md §4.2's first `_fuzzy_match` scored stock names by character overlap with the query, above a `NAME_MATCH_MIN_OVERLAP_RATIO` of 0.6. On 475 labeled mentions from real articles it got 263 right, 80 wrong and missed 132. Used as Entity Analysis's name-search fallback, it lowered entity F1 from 0.962 (no name search at all) to 0.954, through 19 wrong matches.
- **Why it was easy to miss:** the algorithm looks reasonable, and its threshold looks like the knob that would fix any problem. A wrong match is silent: it yields a real, plausible stock code and nothing downstream flags it. Character overlap ignores order, but Chinese abbreviations are *ordered subsequences* of the full name (農行 ⊂ 農業銀行, 國壽 ⊂ 中國人壽). Generic words (地產, 銀行) and single Latin letters overlap with almost everything, and ties were broken by file order. And the fallback runs for only ~1.5% of entities, so the damage is invisible in the aggregate unless you measure it on its own.
- **Fix:** S8, an ordered-subsequence matcher on normalized names. It has no threshold, ranks by how compact the match is, and treats a tie between companies as a miss: 431 right, 12 wrong, 32 missed, and it holds on two held-out sets (impl.md §4.2, design.md §9.2).
- **Topic:** §2.7.

## 1.12 Data quirks decided more cases than the algorithm

Several of §4.2's errors came from the HK Stock List and the text itself, not from the scoring:
- **Duplicate counters.** RMB lines (`80700 騰訊控股－Ｒ`) carry the same name as the HKD line, so they tied with it and won by file order. `－Ｔ` lines are trading-only counters for foreign stocks. Both are now dropped when the list loads.
- **Variant characters.** 恆/恒 and 滙/匯 are the same character to a reader but not to `==`. The LLM spike's own normalizer lacked 恆→恒, so the model's *correct* `00012|恆地` failed verification until it was added.
- **The order of normalization steps changes what a rule means.** Traditional→simplified (`t2s`) runs before the generic-tail regex, which is written in traditional characters, so in practice it strips only the tails spelled the same in both scripts (控股, 股份). "Fixing" it measured worse, so the docs specify the measured form.
- **Some mismatches can't be solved by string matching at all.** A mainland issuer's English legal name ("Shandong Gold Mining Co., Ltd.") is not a translation of HKEX's English short name, and 群核's listed Chinese name is the English "MANYCORE TECH".
- **Why it was easy to miss:** reading the code shows you the algorithm. The quirks show up only when it runs on the real list and real mentions.
- **Fix:** one normalizer, applied at load to the list and per call to the query, shared by both name checks in entity verification (impl.md §4.2). Lesson from the third bullet: once a pipeline has been measured, a "bug" in it is part of what was measured. Fixing it is a new experiment, not a cleanup.
- **Topic:** §2.7.

## 1.13 A tool-calling loop that the task didn't need

impl.md §8.2 originally had the model call a `lookup_stock` tool in a function-calling loop to verify each company, then make a second, structured-output call (via Instructor) for the final answer. The spike replaced it with **one structured-output call**: the model returns each company with its code and name, and code verifies each one against the list (name agrees with the listed name for that code → the article prints the code → name search). On 127 unseen articles: F1 0.962 vs 0.930, $1.14 vs $8.74 per 1k articles, p95 6 s vs 217 s per article. §8.2 as first written cost $23.09 per 1k.
- **Why it was easy to miss:** "let the model check its own work with a tool" sounds like the robust design. But the check was deterministic, and the inputs already held the answer: articles almost always print the code, and the model knows most HKEX codes (237 of the 252 lookups it made were codes, not names). The tool turned a lookup into a multi-turn conversation, and the loop brought failure modes of its own:
  - `function_calling_config.mode=ANY` forces a call on every turn, so the loop's normal exit (no tool call) is unreachable.
  - `AUTO` sometimes re-queried an already-verified code until the round cap.
  - Dense market wraps took 4–6 rounds at 10–16 lookups per turn.
- **Fix:** the one-call design (impl.md §8.2, design.md §9.2, §9.4). The accepted cost: articles that name companies without printing any code rely on the model's memory plus the name search (F1 0.913 with every code stripped, against 0.984 for the loop). About 6% of real articles are like that.
- **Topic:** §2.8.

## 1.14 The bigger model lost

| Model and design | Entity F1 | $ / 1k articles | Reliability |
|---|---|---|---|
| 3.1-pro-preview, tool loop | 0.703 | 41.33 | missed 7, 21 and 30 companies on three dense wraps |
| 3.8-flash (thinking LOW), one call | 0.947 | 2.98 | 7.6% of 1,376 attempts failed; in a ~20-minute capacity episode, 74 of 127 articles failed all 4 attempts |
| **3.1-flash-lite, one call** | **0.962** | **1.14** | 0.6% of 838 attempts failed, p95 8 s |

Classification was close across all of them. Lite agreed with Pro on 0.890 of event types and 0.908 of sentiments, while Pro agrees with *itself* on only 0.954 and 0.972: that is the ceiling. Routing HKEX announcements to 3.8-flash bought nothing measurable.
- **Why it was easy to miss:** models are usually compared on quality alone. In a pipeline, once quality is good enough, tail latency, failure rate under a shared quota, and cost per unit of work decide. A gap smaller than the reference model's disagreement with itself is noise.
- **Fix:** `gemini-3.1-flash-lite` (impl.md §4.1, §8). Accepted trade-offs: it under-calls some negatives in formal HKEX announcements, and some of its summaries exceed 100 characters, which §8.2's validation truncates.
- **Topic:** §2.8.

## 1.15 SDK and platform behaviour that only shows up live

None of these were in the docs the design was written from. All came from calling Vertex AI (details in impl.md §4.1, §7.1, §7.3):
- **No default timeout.** google-genai calls hung for ~260 s until `HttpOptions(timeout=ms)` was set; a hang then comes back as `504 DEADLINE_EXCEEDED`. Without `retry_options` the SDK makes exactly one attempt.
- **A library's retry broke the protocol.** Instructor 1.17's re-ask sends the validation error back as a *model* turn, and Gemini rejects any request ending in a model turn (`400`). Native structured output (`response_json_schema`) already enforces fields and types, so Instructor was dropped.
- **Thinking tokens.** They are reported separately, billed as output, and counted against `max_output_tokens`. A max sized from visible output alone can truncate the answer.
- **Where and how much.** Current Gemini models answer only on the `global` endpoint. Quota is a shared on-demand pool: 429s arrived at ≤ 4 concurrent calls, and there is no fixed RPM to size `ENTITY_ANALYSIS_MAX_CONCURRENT` against.
- **Inputs are not the size you expect.** One cleaned HKEX announcement was 2,069,932 characters, so the body is capped at 30,000 before the call.
- **Caching wasn't a cost lever.** Explicit context caching needs ≥ 4,096 tokens, and the prompt is ~1,800.
- **Why it was easy to miss:** documentation describes the happy path. Defaults like "no timeout" and "one attempt" only surface when a call hangs.
- **Topic:** §2.9.

## 1.16 Benchmark figures that didn't reproduce

The LLM spike's PR review re-ran every figure from the committed runs, and several didn't hold:
- **Label leakage.** The "every code stripped" test set kept the gold code in a synthetic HKEX title (`HKEX 公告 {code}`) and in a `Stock code (if listed) 00595` line. The models looked those codes up directly.
- **A moving input.** The scripts read the prompt from implementation.md. Once the doc was updated with the new design, they no longer ran the prompt that produced the results. The prompt is now a frozen copy (`prompt_v1.md`).
- **Different denominators.** One model's failure rate was per successful call, the other's per attempt.
- **Stale or misattributed cells.** Some came from an earlier verifier state, one result belonged to a different variant, and re-running the split script would have overwritten a hand-corrected split.
- **Data hygiene.** Third-party article titles had been committed inside run records, and the raw export wasn't gitignored.
- **Why it was easy to miss:** each number was right when it was written. The drift came from later changes to the inputs (prompt, verifier, normalizer) without a re-run. And a "codes stripped" set built by regex is only as clean as its patterns.
- **Fix:** freeze inputs, derive every split and figure from a script, state one denominator, scan constructed sets for leaked labels, and re-score stored outputs after every verifier change.
- **Topic:** §2.10.

## 1.17 Development tooling

- **uv** — creates the virtualenv and installs pinned dependencies. Chosen because it is much faster than pip and the repo already uses it (SADI's `requirements.lock` is compiled with `uv pip compile`).
- **pytest + pytest-asyncio** — runs the unit tests; pytest-asyncio lets `async def` tests run without a per-test decorator (`asyncio_mode = "auto"`). Chosen because pytest is the Python standard and SADI already uses both, so tests look the same across services.
- **ruff** — lints and formats the code in one tool, replacing flake8, isort and black. Chosen because a single fast tool with one config is easier to keep consistent than three. One choice follows from it: FastAPI route dependencies are written as `Annotated[T, Depends(...)]`, FastAPI's recommended form, which also satisfies ruff's `B008` rule against function calls in argument defaults.
- **mypy (strict)** — checks types statically. Chosen to enforce the repo rule that values crossing a module boundary are strongly typed; strict mode rejects untyped functions and implicit `Any` in `app/`. Tests are checked too, but may leave their functions unannotated.

pytest, ruff and mypy are configured in the service's `pyproject.toml`; test-only dependencies are pinned in `requirements-test.txt`.

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

SAPI: §1.1.

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

SAPI: §1.2 (an idempotent insert hiding a lost publish), §1.6 (a version check that has to mean "newer", not just "different").

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

SAPI: §1.3.

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

SAPI: §1.2 (patterns 2 and 4 together in Entity Analysis), §1.4 (pattern 4 without a failure callback).

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

SAPI: §1.2, §1.5.

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

SAPI: §1.10.

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
- **Measure the component inside its pipeline.** §4.2's matcher looked mediocre in isolation, but only end-to-end F1 showed it was *worse than nothing* (§1.11).

### What rules can't do
- Irregular abbreviations (中海油 = 中國海洋石油) need an **alias table**: the standard remedy, but it needs upkeep, and can't be tested fairly on the data it was seeded from.
- Cross-language names need the code, the other-language name, or a knowledge base.

### Cost
Precompute the comparison forms at load, pre-filter candidates cheaply (here, names whose characters aren't nested with the query's), and index where you can (English word prefix → stocks). S8 runs at p99 ~5 ms over 2,798 stocks, small enough to stay on the event loop.

SAPI: §1.11, §1.12.

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

SAPI: §1.13, §1.14.

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
- **Know who retries.** The SDK (`retry_options`), a wrapper library, and your own code can each retry. Pick one owner, or the attempts multiply. Libraries can also retry in ways the provider rejects (Instructor's model-turn re-ask, §1.15).
- **A paid call is a side effect you can't undo** (§2.4, pattern 5). Cap attempts, and avoid redoing a call whose result was already stored (§1.2).

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

SAPI: §1.15.

### To investigate
- Provisioned throughput vs on-demand pricing, and the break-even volume.
- Hedged requests for tail latency, and why they're risky for paid, non-idempotent calls.
- Batch prediction APIs: cheaper asynchronous processing for work that isn't latency-sensitive.
- LLM observability: per-call token, latency and cost logs, and alerting on cost drift.
- Provider fallback (a second model or region) and how it interacts with evaluated quality.

---

## 2.10 Running a spike as a measured experiment

A spike answers a design question with evidence. Both Entity Analysis spikes worked because they were run like small experiments, and the PR review of one showed how easily such an experiment drifts (§1.16).

### Build the test set from real data
- **Look for labels the data gives you for free:** news articles print `name(code)`, and HKEX announcements print the issuer's code in the header. Foreign tickers (`name(TICKER.US)`) give negatives, cases where the right answer is "no match".
- **Add hand-collected hard cases** (abbreviations, ambiguous prefixes, A/H names), and adjudicate by reading wherever the automatic gold has gaps.

### Keep tuning data and judging data apart
- **Split dev and test by a hash of the query or the company,** so the same company never sits in both halves. Tune on dev; look at test only to confirm.
- **Score a held-out set exactly once,** on a frozen version, against a pass bar written down *before* scoring. The fuzzy-matching spike's bar was "more right than `WRatio`, and at most 3% wrong".
- **Reserve untouched data for whoever implements the design** (here, 288 articles neither spike read).
- **Record rejected rounds with their numbers** (S9, S10), so nobody has to re-run them to know why they were dropped.

### Measure in context, cheaply
Component accuracy and end-to-end accuracy can disagree (§1.11). **Store raw model outputs, then replay them offline** with one component swapped: the fuzzy-matching spike compared five fallbacks on the LLM spike's stored outputs with no API calls and no LLM randomness between variants.

### Make every figure reproducible
- Freeze the inputs: prompt copies, list snapshots, commit hashes.
- Derive every split and number from a committed script, never from a hand edit.
- State one denominator per metric (per attempt vs per success).
- Scan constructed test sets for leaked labels.
- Re-score after any change to a shared component.
- Commit no third-party text, and gitignore raw exports.

### Small samples
127 articles is small. 0.954 vs 0.962 is a handful of entities. Read the actual errors behind a difference before believing the score, and treat gaps within the reference model's own disagreement as ties.

SAPI: §1.11–§1.16.

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
