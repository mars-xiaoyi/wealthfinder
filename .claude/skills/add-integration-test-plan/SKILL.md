---
name: add-integration-test-plan
description: Review monday.com tickets whose Status is still the default (`Not Started`, or empty — freshly created) against their plan doc (e.g. <service>/docs/impl-plan.md), correct any that drifted, and for the ones that match write an integration test plan into the ticket's description and set its Status to "Planned". Works for any service in the repo. Use when the user asks to review/check created tickets, add integration test plans to tickets, or move tickets to Planned.
---

# Add Integration Test Plans to monday Tickets

For every monday ticket whose Status is still the board default — `Not Started`, or empty (created, not yet reviewed): check that it still matches its plan doc. If it doesn't, correct it. If it does, write an integration test plan into the ticket's description and set its Status to `Planned`.

The monday tools are MCP tools (`mcp__claude_ai_monday_com__*`). If they are deferred, load them first with ToolSearch (`select:mcp__claude_ai_monday_com__get_user_context,mcp__claude_ai_monday_com__get_board_info,mcp__claude_ai_monday_com__get_board_items_page,mcp__claude_ai_monday_com__change_item_column_values,mcp__claude_ai_monday_com__all_monday_api`).

monday tickets in this repo are created by the `create-monday-tickets` skill. **Read `.claude/skills/create-monday-tickets/SKILL.md` §3 and §4 first.** Its markdown notes and content rules (no ordering words, other tickets referenced only by item link, no future-ticket pointers) apply to everything this skill compares and writes, and §4 has the description-write mutation.

---

## 1. Find the unreviewed tickets

1. Find the board: the one the user names, else the project board from `get_user_context` (so far **WealthFinder**, `5031953275`). If several boards are plausible, ask.
2. `get_board_info` with `filters.columns.only: true`: record the Status column id (`project_status` on WealthFinder) and check it has a `Planned` label. If not, stop and ask — don't create labels on your own.
3. `get_board_items_page` with `includeColumns: true`, following `nextCursor` while `has_more`. Keep items whose Status is **`Not Started` or empty**. `Not Started` is the board's default label for a new item, so it means unreviewed. Items with any other Status (`Planned`, `Working on it`, …) are past this step.
4. For each one, `get_board_items_page` with `itemIds: [<id>]`, `includeItemDescription: true`. Work from this live description, because the user may have edited the ticket directly in monday.

If no item is `Not Started` or empty, say so and stop.

## 2. Find each ticket's source section

- The ticket's `Service:` line and name tell you which plan doc it came from. Search for ticket-breakdown docs (e.g. `**/impl-plan.md`) and match the ticket heading to the item name, ignoring backticks.
- Re-read the doc now. It may have changed since the ticket was created.
- If no section matches, or more than one does, flag the ticket and skip it. Don't guess.

## 3. Compare: does the ticket still match the doc?

Compare **substance, not text**. The monday copy legitimately differs from the doc because of the content rules: ordering words are dropped, dependencies become links, and there is no "Source:" footer. Those differences are not mismatches.

It **is** a mismatch when the two disagree on something an implementer would act on: scope items, acceptance criteria, doc `§` pointers, deferred items, dependencies, or the size in the name. Missing or truncated sections count too, and so does a content-rule violation (e.g. a "next ticket" pointer).

**On a mismatch:**
- The plan doc is the source of truth. List the exact differences per ticket.
- Mechanical drift (a missing bullet, a stale `§` number, truncation, a content-rule violation): rewrite the ticket's description to match the doc (see §5 for how). Preserve anything the user added in monday that the doc doesn't contradict.
- If the monday side looks like a deliberate edit, or the doc itself looks wrong, don't overwrite either side. Show both versions and ask.
- Leave a corrected ticket's Status as it was (`Not Started` or empty); don't plan it in the same run. Report it so the user can review the correction first. A re-run will plan it.

**On a match:** continue to §4.

## 4. Design the integration test plan

### Gather the facts first

- Read every spec section the ticket's **Docs** line points to (the service's `implementation.md` / `system-design.md`, `docs/api.md`, etc.). Every expected value in the plan, such as HTTP status, `error_code`, field values, log event names, and config env vars, must come from the spec. Don't invent any.
- Read the repo's existing integration-test conventions for that service's stack. Examples: test directory layout, pytest markers and `addopts` in `pytest.ini`/`pyproject.toml`, `conftest.py` fixtures, the JUnit `@Tag`/Testcontainers setup for Java services. Follow whichever exists. If the service has no tests yet, copy the closest sibling service's convention and say so in the plan.
- Check the ticket's **Explicitly deferred** list. The plan tests only what this ticket builds.

### What an integration test means here

- **Real dependencies and the real app lifecycle.** Use real DB/cache/broker containers and real upstream services, and start the app through its real startup path (e.g. the FastAPI lifespan, or a Spring context). The only stand-ins allowed are for **failure injection at the boundary**: stopping or pausing a container, or pointing an upstream URL/constant at an unreachable or wrong endpoint.
- Tests that hit **external live services** are opt-in, using the repo's live marker or tag. Their reference data must be stable (records unlikely to change or disappear upstream), with a note to swap a reference that stops being valid rather than weaken the assertion.
- Put a hard wall-clock timeout on any test where the failure mode is a hang.

### Expected values: run the spec's rule over the whole response

Don't read an overall result (a status, a summary flag) straight off the ticket's acceptance criteria. Work it out by applying the spec's own rule to **every** field in the response, including any field this ticket hardcodes or stubs because the real check belongs to a later ticket. A stubbed failure value counts exactly like a real one.

- If a stub forces a failure or degraded overall result, cases can't use the overall result to tell success from failure. Have them assert on the individual fields instead, and state the overall value the stub forces.
- If that rule gives a different result from what the acceptance criteria say, it's an ambiguity (below).

### Stubs that hide the success path: re-run in the ticket that makes them real

If a stub means this ticket can't exercise the real success result, the ticket that replaces the stub with the real implementation must re-run this ticket's cases:
- Add an acceptance criterion and a test case to **that** ticket: "re-run the integration cases from <link to this ticket>". Its expected values are worked out the same way, with that ticket's remaining stubs still in place.
- The reference goes from the later ticket back to the earlier one, which the content rules allow because it's a dependency. The earlier ticket never points forward.
- If the later ticket isn't on the board yet, add the criterion to its plan doc section only, so it carries over when the ticket is created.

### Case checklist

Go through these and include each one that applies to the ticket:

| Area | Typical cases |
|---|---|
| Happy path | Everything up gives the documented success response. The body validates against the documented schema. |
| Each dependency failing alone | **Unreachable** (`docker stop`, connection refused) and **unresponsive** (`docker pause`, a black-hole address, where the connection hangs). Assert that the right component reports the failure and the others don't. Use separate instances when two logical connections would otherwise share one container. |
| Recovery | Bring the dependency back. The next call succeeds **without restarting the app**, which proves the check runs live rather than from a cache. |
| Startup behaviour | A mandatory dependency down makes startup fail. A tolerant dependency down still lets the app start and degrade as documented: fallback state, log event at its level, health status. |
| Error classification | Each documented failure mode maps to its own HTTP status and `error_code` (e.g. resource/availability vs parse/code issue), driven by a real response rather than a mock. |
| State on failure | State documented as "left untouched" really is unchanged: same object or rows, same size, and the health check is unaffected. |
| Config actually applied | A timeout or limit set via env is observable end-to-end, e.g. it fails faster than the library default. |
| Repeat / idempotency | Repeated calls don't duplicate or pile up state. Refresh-style operations rebind or replace as the spec says. |
| Shape drift | For external data sources, assert structural invariants of the live response, since a fixture can't catch upstream changes. |

Leave out cases the ticket doesn't build (deferred items), and don't restate unit-test scenarios.

### Ambiguity: ask before writing that ticket's plan

If the ticket's acceptance criteria and the spec disagree in a way that changes what a case would assert, it is a design decision and not yours to settle silently. A common case: the criteria promise a success result, but a field this ticket stubs forces a failure result under the spec's rule. Ask the user (AskUserQuestion), offering both readings, before writing that ticket's plan. Continue with the other tickets meanwhile.

Once the user decides:
1. Update that ticket's acceptance criteria to the decided version in **both** the plan doc and monday. They are now the source of truth for the plan.
2. Write the plan from the updated criteria.
3. Re-check every ticket the decision affects, especially the one that will replace the stub (see above). Update its criteria and test plan too.

## 5. Write the plan into the ticket

Add one section at the end of the **live** description, after the last existing section, leaving everything else unchanged:

```markdown
## Integration test plan

**Setup:** <real deps, how the app is started, failure-injection method, live opt-in marker if any, test file location>.

1. **<case name>:** <trigger> → <exact expected result>.
...
```

- Each case states its **trigger** and its **observable expected result** (status code, field values, `error_code`, log event, state identity or size). Avoid vague wording like "works correctly" or "handles errors".
- Follow the content rules: to build on another ticket's suite, link that item by URL. Never write "the previous ticket's suite".
- If the ticket already has an `Integration test plan` section, don't overwrite it. Show what you would change and ask.
- Don't copy the plan into the plan doc. It lives only in monday unless the user asks otherwise.

**How to write it.** monday can't append to an item description (`add_content_to_doc_from_markdown` rejects description ids), and `set_item_description_content` **replaces** the whole description. So:
1. Rebuild markdown from the live description blocks read in §1. Each block's `content` is a delta: `normal text` → paragraph, `medium title` → `##`, `large title` → `#`, `small title` → `###`, `bulleted list` → `- `, `numbered list` → `1. `, with `indentation` → nested list; attributes `bold` → `**…**`, `italic` → `*…*`, `code` → `` `…` ``, `link` → `[…](url)`. If a block type or attribute has no markdown equivalent (table, image, color, …), stop and ask — rewriting would lose it.
2. Append the test plan section and write the result with `set_item_description_content` (mutation in `create-monday-tickets` §4, markdown via `variables`).
3. Then set Status: `change_item_column_values` with `{"<status column id>": {"label": "Planned"}}`.

Update tickets **one at a time, sequentially**. If a call errors or its response is lost, re-read the item before retrying.

## 6. Verify

Re-read each updated item (`get_board_items_page` with `itemIds`, `includeItemDescription: true`, `includeColumns: true`) and check:
- Status is `Planned`.
- The test plan section is present and complete, every case as its own list block. Item links point to the right tickets.
- Every block from the live version you read in §1 is still there, in order, with the same type, text, and formatting (block ids change on rewrite; compare content, not ids).
- Description blocks come back in pages of 25. If the read stops at exactly 25 blocks, fetch the rest with `all_monday_api`: `query { items(ids: [<id>]) { description { blocks(limit: 25, page: 2) { type content } } } }` (then page 3, …). Compare the total against the `block_ids` count from the write before calling anything truncated.

Fix any problem and re-verify.

## 7. Report

Keep it short:
- Per ticket: matched or corrected (and what changed), whether a plan was added, and the new Status.
- A one-line summary of each plan's coverage.
- Any ambiguity still waiting for the user, with your recommended reading.
- Tickets skipped and why (no matching doc section, plan already present, corrected and waiting for review).

Don't change status lines in the plan doc. Its lifecycle may not include `Planned`. If it doesn't, mention it once and offer to add it.
