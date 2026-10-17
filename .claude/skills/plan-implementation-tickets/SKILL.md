---
name: plan-implementation-tickets
description: Split an approved design (system-design / implementation docs) into implementation tickets in a plan doc (e.g. <service>/docs/impl-plan.md) — draft the next ticket(s), decide spike-vs-code for open questions, surface design gaps found while slicing, and check a layer/feature for complete coverage. Use when the user asks to generate/draft/split the next ticket(s), break a design into tickets, decide whether something needs a spike, or check whether a layer's tickets cover everything. Works for any service in the repo. Produces "Designing" tickets; create-monday-tickets takes them from "Designed" onward.
---

# Plan Implementation Tickets

Turn an already-approved design into tickets in a plan doc. The plan doc introduces **no new design** — it says which part of the spec each ticket builds and points at the binding `§` sections. When slicing exposes a design gap, the fix goes into the design docs (with approval), never silently into a ticket.

---

## 1. Load context before drafting anything

1. **Plan doc:** use the one the user names; otherwise look for `**/impl-plan.md` (ask if more than one fits). Read its conventions section ("How to use this doc" or similar) in full — ticket format, status lifecycle, test layout, independence rules. Those conventions override this skill where they differ.
2. **Existing tickets:** read all of them. Note statuses, dependencies, and every **Explicitly deferred** line — the next ticket usually comes from what the most recent ticket deferred.
3. **Source-of-truth docs:** the service's `system-design.md` (rationale) and `implementation.md` (buildable spec), plus any API spec. Read the *full* sections the next ticket touches — config classes, the classes/methods involved, error-handling tables, flowcharts, and the Open Questions section. Skimming is how gaps get missed.
4. **Re-read the plan doc before every edit** — the user often edits it (statuses, wording) between turns. Treat on-disk changes as deliberate.

## 2. Pick and slice the next ticket

- **One ticket = one PR = a vertical slice of observable behavior**, not a technical layer. Name it after the behavior (`Stock Resolution: lookup_stock`, `Persist & Publish Entity Events`), not the layer.
- **Build only what this slice needs.** List the exact config fields, methods, and types it adds — e.g. "`DatabaseClient.transaction()` and `fetch_all()` only, not `execute()`". A class grows method-by-method as tickets need it; shared infra has no ticket of its own.
- **Size target S/M; L at most.** If a draft reaches XL, split it. Good split lines found in practice:
  - A pure, independently testable unit (a lookup/matching function with its own log event and tests) → its own ticket.
  - A safety gate/guard that only *matters* once a later ticket lands → its own small ticket, ordered before that later ticket (see §5).
  - Persistence/publishing vs. the computation that produces the data → separate tickets.
  - Reliability machinery (reclaim/dead-letter/retry), concurrency/backoff, and monitoring/health → separate tickets.
- **When a ticket ships without something the full spec has,** say what happens meanwhile and why that's acceptable (e.g. "ACKs after analysis without persisting — same as the previous ticket ACKing after a fetch"). If it isn't acceptable, that piece belongs in this ticket.

## 3. Decide spike vs. code for every open question the slice touches

Classify each related open question / unverified assumption:

| Answer could change… | Treatment |
|---|---|
| An interface, control flow, algorithm, or SDK usage (e.g. "does the SDK expose X", "does tool-calling mode Y ever terminate", "does library Z accept this message history") | **Spike first**, as its own ticket the coding ticket depends on |
| Whether a default is catastrophically wrong (a timeout every call exceeds, a round limit that drops data, a token cap that truncates) | **Cheap sanity check inside the spike**; set the initial value from it |
| Only a tuning number, safe at any plausible value | **Code with the default; calibrate from production logs** in a post-release validation ticket |
| An external fact checkable in minutes (provider quota model, a documented limit) | A one-line item in the related spike — not an acceptance criterion on a coding ticket, especially if the fact may not exist (e.g. no fixed quota) |

Rules:
- **Spikes:** throwaway scripts (e.g. under `poc/<service>/`), no production code. Acceptance = every question has a written answer with evidence (printed raw output or measured numbers), and every design-changing answer is **written back into the design docs as a fix (not an open question) and approved** before the dependent coding ticket starts. Add a time-box PM note.
- **Split independent spikes** (e.g. "LLM behaviour" vs. "fuzzy matching against reference data") so they run in parallel. Give each its own input data — don't make one spike consume the other's output, or they stop being independent.
- **Never put a post-release check on a coding ticket.** A ticket closes at merge; a "validate after release" line on it has no owner and no trigger. Collect those into one **Post-Release Tuning Validation** ticket per layer/feature (scope: each question, the log/metric it's measured from; acceptance: numbers + keep-or-change decision per item, defaults updated in code and docs, questions marked resolved). Coding tickets point at it with "Validated by: …".

## 4. Hunt for design gaps while slicing

Slicing is where hidden gaps surface. For each draft, actively check:

- **Failure sequences across ticket boundaries.** Walk crash/failure between every pair of side effects (write → publish → ACK). Ask: on redelivery, is anything lost or duplicated? (Found in practice: DB insert committed, publish lost, redelivery hits `ON CONFLICT DO NOTHING` → event never published.)
- **Claims about other components.** If the design relies on "the downstream layer handles duplicates", open the downstream spec and verify it — note *both* the immediate case and the later case (e.g. duplicate arriving after the original was flushed/expired).
- **Non-determinism on retry.** A redelivery that re-runs an LLM or other non-deterministic step may not reproduce the original output; prefer "detect already-done work and replay its result" over "redo it".
- **Cross-service contracts vs. real code.** Check that the other service actually produces what this design consumes (open its code, not just its docs). (Found in practice: consumer validates a `v` version field the producer never sent → every message would be dead-lettered.)
- **Acceptance criteria that can't fail.** Given what's hardcoded or not built yet at that point, could the stated outcome be observed either way? Rewrite to something that actually distinguishes pass from fail.
- **Underspecified contracts.** "Await vs. spawn", "check before vs. after acquiring", a payload field with no source on one code path — anything an implementer would have to invent.
- **Contradictions and stale references** between docs (or within one), uncovered while reading — stale imports, wrong module paths, rows that contradict another section.
- **Shared infrastructure collisions** (e.g. two services' migration-version tables in one database).

**Every gap is surfaced as a decision, not folded into the ticket.** Present: the concrete failure scenario, 2–3 options with trade-offs, a recommendation. After approval, write the fix into the design docs first — rationale in system-design, code-level spec in implementation (don't duplicate between them) — then update the affected tickets and any renumbered step references. A small, purely mechanical fix the user has already approved can be applied directly.

## 5. Dependencies, ordering, and independence

- **Depends on** lists only what the ticket actually needs from earlier tickets, each with "(for X)".
- **Ordering constraints** that aren't data dependencies get stated explicitly on the ticket: "Must land before <ticket> — without it, <concrete harm>". Decide the position by asking when the harm first becomes real (a readiness gate is harmless while nothing is persisted; it must precede persistence).
- **Service independence (unless the plan doc says otherwise):** no implementation ticket depends on another internal service being built or deployed. In integration tests, anything another internal service provides (its API, its stream messages) is **mocked**; the service's own infra (DB, cache, queues) is **real**; third-party APIs whose real behaviour is the ticket's risk are called **live**. The only exception is a production-like volume/validation run. If a cross-service contract fix is needed, make it in the other service separately — don't add it as a ticket dependency; note release ordering for the go-live checklist instead.
- **Don't reference artifacts no ticket creates.** If an acceptance criterion mentions a file or resource that nothing builds and no test uses (e.g. an env template), remove the reference rather than inventing a ticket for it.

## 6. Ticket format

Follow the plan doc's existing format exactly. The format this skill was built from:

```markdown
### <Area>: <Behavior> (<XS|S|M|L>)

**Status:** Designing
**Depends on:** <ticket> (for <what>); … | nothing
**Scope:** <one sentence: the behavior + `§` pointers>.
- `<module>`: <exactly which classes/methods/fields, "only"/"not X" where it matters> (§x.y).
- …
**Acceptance criteria** (per impl.md §…):
- <observable, failable outcome>
- Unit-tested against <mocks>.
- Integration pass against <real infra / mocked services / live third-party>.
**Config note:** <deployment constraint, only if one exists>
**Docs:** impl.md §…; design.md §…. Validated by: <validation ticket> (<question IDs>).
**Explicitly deferred:** <items a dev must NOT do here, optionally naming the ticket that picks each up>.
**PM note:** <judgment call not settled by the docs, e.g. time-box>
```

Writing rules:
- **Point, don't restate.** Signatures, schemas, SQL, and pseudocode live in the spec; the ticket names *which* parts and *how much*. No "Files" line.
- **Acceptance criteria are testable and can fail.** Name the exact log events, payload fields, status codes, and the specific edge cases (empty result, redelivery, mid-operation failure, exhaustion of retries).
- **Explicitly deferred is a note to the developer** ("don't build this here"). Naming the owning ticket is optional — don't churn earlier tickets just to add pointers. But do fix a pointer that has become *wrong* (e.g. "next ticket builds X" after X was split out).
- New tickets are always written as **Designing**. Only the user moves a ticket to `Designed`; `create-monday-tickets` moves it to `Created`. Don't edit `Created` tickets' content.

## 7. Approval workflow

1. **Draft in chat first.** Show the full ticket(s), then a short numbered list of the scope decisions and any gaps found (§4), each with a recommendation. Don't write to the plan doc yet.
2. Wait for explicit approval. Apply requested changes; if the user only asks a question, answer it and re-ask — a question is not approval.
3. On approval: design-doc fixes first (if any), then insert the ticket(s) in the plan doc in dependency order, then fix any now-stale references in earlier tickets and renumbered step references.
4. Report what changed, file by file, and anything deliberately left untouched (and why).

## 8. Coverage check ("did we cover everything for <layer>?")

Map the spec to tickets, item by item — don't answer from memory:
- Every implementation-doc section for the layer (classes, methods, singletons, wiring in the entry point, migrations, health/monitoring fields).
- **Every config field** → the ticket that adds it (a table is the clearest output).
- Every row of the layer's error-handling table → the ticket whose acceptance criteria cover it.
- Every related open question → a spike, a coding ticket, or the validation ticket. An open question with no owner is a gap.
- Cross-cutting checks: contracts with other services against their real code; criteria referencing artifacts nothing creates; criteria that can't fail; stale references.

Report "covered" only after the mapping is complete; list each remaining gap with a recommended fix and ask before applying.
