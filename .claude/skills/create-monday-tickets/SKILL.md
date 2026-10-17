---
name: create-monday-tickets
description: Create monday.com tickets (board items) from an implementation-plan doc (tickets marked "Designed"), verify them in monday, then mark them "Created" in the doc. Also covers updating already-created tickets. Use when the user asks to create, sync, or update tickets from a plan/ticket-breakdown doc.
---

# Create monday Tickets from a Plan Doc

Turn ticket sections in a plan doc (e.g. `<service>/docs/impl-plan.md`) into monday.com board items, verify them, and record the result back in the doc.

The monday tools are MCP tools (`mcp__claude_ai_monday_com__*`). If they are deferred, load them first with ToolSearch (`select:mcp__claude_ai_monday_com__get_user_context,mcp__claude_ai_monday_com__get_board_info,mcp__claude_ai_monday_com__get_board_items_page,mcp__claude_ai_monday_com__create_item,mcp__claude_ai_monday_com__change_item_column_values,mcp__claude_ai_monday_com__all_monday_api`).

---

## 1. Find the plan doc and the ready tickets

1. Use the doc the user names. If none is named, look for ticket-breakdown docs in the repo (e.g. `**/impl-plan.md`); if more than one plausible doc exists, ask which.
2. Read the doc's own conventions section first (status lifecycle, ticket format). Ticket statuses are typically `Designing` → `Designed` → `Created`. **Only `Designed` tickets are ready to create.** Never create `Designing` tickets; never re-create `Created` ones.
3. Re-read the ticket sections right before creating — the user may have edited the doc (including flipping statuses) since you last looked.

## 2. Determine the board and group — confirm before creating

Try to figure it out first:
- `get_user_context` lists the user's relevant boards (the project board so far is **WealthFinder**, `5031953275`).
- `get_board_info` (with `filters.columns.only: true`) gives the board's groups and column ids.
- `get_board_items_page` shows which group existing items from the same plan doc live in.
- Groups are per layer (e.g. System Setup, Entity Analysis, Aggregation Layer, Scoring Layer). Match the ticket's layer prefix to a group.

Then:
- **If you found a likely board + group:** state it (with the reason) and get explicit confirmation from the user before creating anything.
- **If you can't tell, or the layer has no group yet:** ask the user — offer the groups you found as options, or propose a new group name.

Once confirmed for a plan doc in this session, reuse it for later tickets from the same doc — but re-check the group still exists (the group id is what matters).

Also check the board for an item with the same name before creating, to avoid duplicates.

## 3. Translate the ticket section into a monday item

**Name:** the ticket heading, with markdown backticks removed (e.g. `Health Check: GET /v1/health (M)`).

**Item description:** the ticket body goes into the item's **description**, never into an update (comment). Carry over the ticket's content faithfully — don't rewrite, summarize, or add design. Write it as markdown:

```markdown
**Service:** <service>

**Depends on:** <link(s) to dependency items, or "nothing"> (<what it needs from them>)

## Scope
...
## Acceptance criteria
...
## Docs
...
## Explicitly deferred
...
```

Include any other sections the ticket has (e.g. an integration test plan) the same way.

Markdown notes:
- Separate `**Service:**` and `**Depends on:**` with a blank line — a single newline merges them into one paragraph.
- Bullets, numbered lists, `##` headings, bold, and inline code all convert to native description blocks. Text colors/highlights are not supported.
- Link an item with its URL: `[<item name>](https://<account>.monday.com/boards/<board id>/pulses/<item id>)`.

### Content rules for monday tickets

Plan docs are ordered; a monday board is not. When copying a ticket into monday:

1. **No ordering words.** Drop or rephrase "next ticket", "previous ticket", "the previous tickets' deferred sections", "below/above ticket", etc. They mean nothing on the board.
2. **Minimize references to other tickets outside "Depends on".** If one is genuinely needed (e.g. "extends the integration suite from X"), link that item — never a bare ticket name.
3. **Never reference a future ticket** (one not yet created, or downstream of this one). Drop pointers like "(next ticket)", "owned by whichever slice…", "X gets built there", "unlike the <Other> ticket's …", "(<Some Future Ticket>)", and phase or timing notes such as "(needs <prerequisite>, Phase N+)". Keep the deferred *item* itself, with nothing about who owns it or when it lands. Pure doc references (e.g. `design.md §9.2`, an open-question ID) are fine to keep.
4. **"Depends on" uses links.** Every dependency must already exist on the board; link it by item URL. If a dependency hasn't been created yet, stop and flag it rather than creating the ticket with a dangling reference.
5. **No "Source:" footer** pointing back to the plan doc.
6. **Don't set column values** — Status, Owner, Priority, dates, timeline — unless the user asks. New tickets keep the board's default Status (`Not Started`, or empty). The one column set by default is **Dependent On** (see §4).

These rules apply only to the monday copy. **Do not edit the plan doc's wording** to match — the doc keeps its own ordered phrasing.

## 4. Create

Create items **one at a time, sequentially**, in dependency order so each item's dependencies exist and can be linked. For each ticket:

1. `create_item` with `boardId`, `groupId`, `name`, and `columnValues: "{}"`.
2. Write the description with `all_monday_api`:
   ```graphql
   mutation ($item_id: ID!, $markdown: String!) {
     set_item_description_content(item_id: $item_id, markdown: $markdown) { success block_ids error }
   }
   ```
   Pass the markdown via `variables`, not inlined in the query. This **replaces** the whole description.
   - **Known issue:** on a brand-new item this mutation can fail with `INTERNAL_SERVER_ERROR` because the item's description document doesn't exist yet. Check with `query { items(ids: [<id>]) { description { id } } }`. If there's no description document, stop and ask the user to open the item once in monday (which creates it), then retry. Never fall back to posting the body as an update.
3. If the ticket has dependencies, set the **Dependent On** column (`dependency_mm86r1ns` on the WealthFinder board — confirm the id with `get_board_info`) with `change_item_column_values`: `{"<column id>": {"item_ids": [<dependency item ids>]}}`.

**If a call errors or the response is lost:** do not blindly retry. Re-read (`get_board_items_page` with `itemIds`, or `searchTerm` by name) to see whether the write actually landed, then retry only what's missing.

## 5. Verify

For every created item, `get_board_items_page` with `itemIds: [<id>]`, `includeItemDescription: true`, `includeColumns: true`, and check:
- Name is exact (no escaping artifacts).
- It's on the confirmed board, in the confirmed group.
- Dependent On lists the right items, and the "Depends on" links point to them.
- No column values were set beyond Dependent On (Status is the board default: `Not Started` or empty).
- The description matches the ticket content, obeys the content rules in §3, and has nothing truncated — every section heading and bullet is present as its own block.
- The item has no updates carrying ticket content.

Fix any problem and re-verify before moving on.

## 6. Mark "Created" in the plan doc

Only after verification, change that ticket's status line from `Designed` to `Created` (e.g. `**Status:** Designed` → `**Status:** Created`). Change only the status lines of verified tickets — touch nothing else in the doc. Then grep the status lines to confirm.

## 7. Report

Give a short summary: each created ticket with its monday link, the group it was placed in, dependencies set, anything fixed during verification, and which tickets were skipped (still `Designing`, or blocked on a missing dependency).

---

## Updating existing tickets

When asked to change tickets already on the board (e.g. apply a new content rule, remove a line):

1. **Re-fetch each item's current description first** (`get_board_items_page` with `includeItemDescription: true`). The user may have edited it directly in monday (e.g. added an integration test plan). Build the new markdown from that live version and preserve everything the user added — change only what was asked. Since `set_item_description_content` replaces the whole description, anything left out of the markdown is lost.
2. If the change is more than mechanical, list the exact proposed edits per ticket and wait for approval before writing.
3. Update one item at a time; re-read after any error before retrying.
4. Verify each updated item as in §5.
5. Don't touch the plan doc unless the user asks.
