---
name: add-integration-test-plan
description: Review Asana tickets whose Status is "Created" against their plan doc (e.g. <service>/docs/impl-plan.md), correct any that drifted, and for the ones that match write an integration test plan into the ticket and move its Status to "Planned". Works for any service in the repo. Use when the user asks to review/check Created tickets, add integration test plans to Asana tickets, or move tickets to Planned.
---

# Add Integration Test Plans to Asana Tickets

For every Asana ticket with Status `Created`: check that it still matches its plan doc. If it doesn't, correct it. If it does, write an integration test plan into the ticket and set its Status to `Planned`.

The Asana tools are MCP tools (`mcp__asana__*`). If they are deferred, load them first with ToolSearch (`select:mcp__asana__asana_list_workspaces,mcp__asana__asana_get_projects,mcp__asana__asana_get_tasks,mcp__asana__asana_get_task,mcp__asana__asana_update_task`).

Asana tickets in this repo are created by the `create-asana-tickets` skill. **Read `.claude/skills/create-asana-tickets/SKILL.md` §3 first.** Its HTML constraints and Asana content rules (no ordering words, other tickets referenced only by GID link, no future-ticket pointers) apply to everything this skill compares and writes.

---

## 1. Find the `Created` tickets

1. `asana_get_projects` with `opt_fields: name,custom_field_settings.custom_field.name` to find the project and its `Status` custom field. If several projects carry one, ask which, or use the project the user names.
2. `asana_get_tasks` on the project with `opt_fields: name,completed,memberships.section.name,custom_fields.name,custom_fields.display_value,custom_fields.enum_options.name,custom_fields.enum_options.gid`, following `next_page` until it is exhausted. Keep incomplete tasks whose `Status` display value is `Created`.
3. From the same response, record the `Status` field GID and the option GID for `Planned`. Never hardcode GIDs; they differ per workspace. If no `Planned` option exists, stop and ask.
4. `asana_get_task` each one with `opt_fields: name,html_notes,notes,dependencies.name`. Work from this live `html_notes`, because the user may have edited the ticket directly in Asana.

If nothing is `Created`, say so and stop.

## 2. Find each ticket's source section

- The ticket's `Service:` line and name tell you which plan doc it came from. Search for ticket-breakdown docs (e.g. `**/impl-plan.md`) and match the ticket heading to the Asana name, ignoring backticks.
- Re-read the doc now. It may have changed since the ticket was created.
- If no section matches, or more than one does, flag the ticket and skip it. Don't guess.

## 3. Compare: does the ticket still match the doc?

Compare **substance, not text**. The Asana copy legitimately differs from the doc because of the content rules: ordering words are dropped, dependencies become links, and there is no "Source:" footer. Those differences are not mismatches.

It **is** a mismatch when the two disagree on something an implementer would act on: scope items, acceptance criteria, doc `§` pointers, deferred items, dependencies, or the size in the name. Missing or truncated sections count too, and so does a content-rule violation (e.g. a "next ticket" pointer).

**On a mismatch:**
- The plan doc is the source of truth. List the exact differences per ticket.
- Mechanical drift (a missing bullet, a stale `§` number, truncation, a content-rule violation): update the ticket's `html_notes` to match the doc. Preserve anything the user added in Asana that the doc doesn't contradict.
- If the Asana side looks like a deliberate edit, or the doc itself looks wrong, don't overwrite either side. Show both versions and ask.
- Leave a corrected ticket at `Created`; don't plan it in the same run. Report it so the user can review the correction first. A re-run will plan it.

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
- The reference goes from the later ticket back to the earlier one, which the Asana content rules allow because it's a dependency. The earlier ticket never points forward.
- If the later ticket isn't `Created` yet, add the criterion to its plan doc section only, so it carries over when the ticket is created.

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
1. Update that ticket's acceptance criteria to the decided version in **both** the plan doc and Asana. They are now the source of truth for the plan.
2. Write the plan from the updated criteria.
3. Re-check every ticket the decision affects, especially the one that will replace the stub (see above). Update its criteria and test plan too.

## 5. Write the plan into the ticket

Append one section to the **live** `html_notes`, after the last existing section and before any trailing footer, leaving everything else byte-for-byte unchanged:

```html
<h2>Integration test plan</h2><strong>Setup:</strong> <real deps, how the app is started, failure-injection method, live opt-in marker if any, test file location>.
<ol><li><strong><case name>:</strong> <trigger> → <exact expected result>.</li>...</ol>
```

- Each case states its **trigger** and its **observable expected result** (status code, field values, `error_code`, log event, state identity or size). Avoid vague wording like "works correctly" or "handles errors".
- Follow the content rules: to build on another ticket's suite, link that ticket with `<a data-asana-gid="<gid>"/>`. Never write "the previous ticket's suite".
- If the ticket already has an `Integration test plan` section, don't overwrite it. Show what you would change and ask.
- Don't copy the plan into the plan doc. It lives only in Asana unless the user asks otherwise.

Update tickets **one at a time, sequentially**. Send `html_notes` and the Status change in the same `asana_update_task` call:
`custom_fields: {"<status field gid>": "<Planned option gid>"}`.
If a call errors or its response is lost, re-read the task before retrying.

## 6. Verify

`asana_get_task` each updated ticket and check:
- Status is `Planned`.
- The test plan section is present, complete, and well-formed. Dependency links resolved to the right tasks.
- Everything outside the new section is unchanged from the live version you read in §1.

Fix any problem and re-verify.

## 7. Report

Keep it short:
- Per ticket: matched or corrected (and what changed), whether a plan was added, and the new Status.
- A one-line summary of each plan's coverage.
- Any ambiguity still waiting for the user, with your recommended reading.
- Tickets skipped and why (no matching doc section, plan already present, corrected and waiting for review).

Don't change status lines in the plan doc. Its lifecycle may not include `Planned`. If it doesn't, mention it once and offer to add it.
