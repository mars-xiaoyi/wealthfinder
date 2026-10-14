---
name: create-asana-tickets
description: Create Asana tickets from an implementation-plan doc (tickets marked "Designed"), verify them in Asana, then mark them "Created" in the doc. Also covers updating already-created tickets. Use when the user asks to create, sync, or update Asana tickets from a plan/ticket-breakdown doc.
---

# Create Asana Tickets from a Plan Doc

Turn ticket sections in a plan doc (e.g. `<service>/docs/impl-plan.md`) into Asana tasks, verify them, and record the result back in the doc.

The Asana tools are MCP tools (`mcp__asana__*`). If they are deferred, load them first with ToolSearch (`select:mcp__asana__asana_list_workspaces,mcp__asana__asana_typeahead_search,mcp__asana__asana_get_project_sections,mcp__asana__asana_get_tasks,mcp__asana__asana_get_task,mcp__asana__asana_create_task,mcp__asana__asana_update_task,mcp__asana__asana_set_task_dependencies`).

---

## 1. Find the plan doc and the ready tickets

1. Use the doc the user names. If none is named, look for ticket-breakdown docs in the repo (e.g. `**/impl-plan.md`); if more than one plausible doc exists, ask which.
2. Read the doc's own conventions section first (status lifecycle, ticket format). Ticket statuses are typically `Designing` → `Designed` → `Created`. **Only `Designed` tickets are ready to create.** Never create `Designing` tickets; never re-create `Created` ones.
3. Re-read the ticket sections right before creating — the user may have edited the doc (including flipping statuses) since you last looked.

## 2. Determine the Asana project and section — confirm before creating

Try to figure it out first:
- `asana_list_workspaces`, then `asana_typeahead_search` (`resource_type: project`) to list candidate projects.
- `asana_get_project_sections` and `asana_get_tasks` (with `opt_fields: name,memberships.section.name`) on the likely project.
- Strong signals: a section where tickets from the same plan doc already live; a project/section name matching the service or phase; the plan doc naming a project.

Then:
- **If you found a likely project + section:** state it (with the reason) and get explicit confirmation from the user before creating anything.
- **If you can't tell:** ask the user — offer the projects/sections you found as options.

Once confirmed for a plan doc in this session, reuse it for later tickets from the same doc — but re-check the section still exists (it may have been renamed; the section GID is what matters).

Also check the project for a task with the same name before creating, to avoid duplicates.

## 3. Translate the ticket section into an Asana task

**Name:** the ticket heading, with markdown backticks removed. Use plain characters (`&`, not `&amp;`) — the name field is not HTML and escaping it produces a literal `&amp;`.

**Description (`html_notes`):** carry over the ticket's content faithfully — don't rewrite, summarize, or add design. Structure:

```html
<body><strong>Service:</strong> <service>
<strong>Depends on:</strong> <link(s) to dependency tasks, or "nothing"> (<what it needs from them>)

<h2>Scope</h2>...<ul><li>...</li></ul>
<h2>Acceptance criteria</h2>...<ul><li>...</li></ul>
<h2>Docs</h2>...
<h2>Explicitly deferred</h2>...
</body>
```

Include any other sections the ticket has (e.g. an integration test plan) the same way.

HTML constraints (Asana rejects anything else):
- Allowed tags only: `body, strong, em, u, s, code, ol, ul, li, a, blockquote, pre, h1, h2, hr, img`. No attributes on anything except `<a>`.
- Must be well-formed XML with a single `<body>` root. Escape `&`, `<`, `>` in text (`&amp;`, `&lt;`, `&gt;`).
- Markdown backticks → `<code>`, bold → `<strong>`, bullets → `<ul><li>`, numbered lists → `<ol><li>`.
- Link a task by GID: `<a data-asana-gid="<gid>"/>` — Asana expands it to a titled link.

### Content rules for Asana tickets

Plan docs are ordered; Asana is not. When copying a ticket into Asana:

1. **No ordering words.** Drop or rephrase "next ticket", "previous ticket", "the previous tickets' deferred sections", "below/above ticket", etc. They mean nothing in Asana.
2. **Minimize references to other tickets outside "Depends on".** If one is genuinely needed (e.g. "extends the integration suite from X"), use an Asana link to that task — never a bare ticket name.
3. **Never reference a future ticket** (one not yet created, or downstream of this one). Drop pointers like "(next ticket)", "owned by whichever slice…", "X gets built there", "(<Some Future Ticket>)", and phase or timing notes such as "(needs <prerequisite>, Phase N+)". Keep the deferred *item* itself, with nothing about who owns it or when it lands. Pure doc references (e.g. `design.md §9.2`, an open-question ID) are fine to keep.
4. **"Depends on" uses links.** Every dependency must already exist in Asana; link it by GID. If a dependency hasn't been created yet, stop and flag it rather than creating the ticket with a dangling reference.
5. **No "Source:" footer** pointing back to the plan doc.
6. Don't set assignee, due dates, or custom fields (team, priority, …) unless the user asks. The one exception is the **Status** custom field: every new ticket gets Status = `Created` (see §4).

These rules apply only to the Asana copy. **Do not edit the plan doc's wording** to match — the doc keeps its own ordered phrasing.

## 4. Create

- Create tasks **one at a time, sequentially** (`asana_create_task` with `project_id` + `section_id`). Parallel writes have dropped connections and lost responses.
- Create in dependency order so each task's dependencies exist and can be linked.
- After creating a task that has dependencies, also set the real Asana dependency: `asana_set_task_dependencies(task_id, [dependency gids])`.
- Set the task's **Status** custom field to `Created` — the default status for every new ticket. Look up the field and option GIDs from the `custom_fields` in the create response (field named `Status`, enum option named `Created`); don't hardcode them. Pass it either in `asana_create_task` or afterwards with `asana_update_task`, as a JSON string: `custom_fields: '{"<status field gid>": "<Created option gid>"}'`.

**If a call errors or the response is lost:** do not blindly retry. Re-read (`asana_get_task`, or `asana_get_tasks` on the project to find it by name) to see whether the write actually landed, then retry only what's missing.

## 5. Verify

For every created task, `asana_get_task` with `opt_fields: name,memberships.section.name,dependencies.name,html_notes,custom_fields.name,custom_fields.display_value` and check:
- Status custom field is `Created`.
- Name is exact (no `&amp;` or other escaping artifacts).
- It's in the confirmed project and section.
- Dependencies are set and the "Depends on" link resolved to the right task.
- The description matches the ticket content, obeys the content rules in §3, and has nothing truncated.

Fix any problem (`asana_update_task`) and re-verify before moving on.

## 6. Mark "Created" in the plan doc

Only after verification, change that ticket's status line from `Designed` to `Created` (e.g. `**Status:** Designed` → `**Status:** Created`). Change only the status lines of verified tickets — touch nothing else in the doc. Then grep the status lines to confirm.

## 7. Report

Give a short summary: each created ticket with its Asana link, where it was placed, dependencies set, anything fixed during verification, and which tickets were skipped (still `Designing`, or blocked on a missing dependency).

---

## Updating existing tickets

When asked to change tickets already in Asana (e.g. apply a new content rule, remove a line):

1. **Re-fetch each task's current `html_notes` first.** The user may have edited it directly in Asana (e.g. added an integration test plan). Build the update from that live version and preserve everything the user added — change only what was asked.
2. If the change is more than mechanical, list the exact proposed edits per ticket and wait for approval before writing.
3. Update one task at a time; re-read after any error before retrying.
4. Verify each updated task as in §5.
5. Don't touch the plan doc unless the user asks.
