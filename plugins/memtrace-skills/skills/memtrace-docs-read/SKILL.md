---
name: memtrace-docs-read
description: "Read the full plain-text body of an official Memtrace docs page by slug. Use when you know the page path (from search_docs, ask_docs citations, or user link) and need complete content — CLI reference, enterprise deploy guide, MCP tool tables. Calls read_doc or memtrace://docs/* resources on memtrace.io."
---

## Overview

`read_doc` fetches the full rendered text of one docs page. Use after `search_docs`
or `ask_docs` when excerpts are not enough — long flag tables, deploy runbooks,
MCP tool inventories.

Alternative: MCP resource `memtrace://docs/<slug>` via `read_resource` if your
client prefers resources over tools.

Umbrella routing: `memtrace-docs` workflow.

## `read_doc` parameters

| Param | Required | Notes |
|---|---|---|
| `slug` | yes | Path without `/docs/` prefix |

```json
{ "slug": "enterprise/memdb-deploy" }
```

Valid examples:

- `getting-started`
- `cli/start`
- `mcp/tools`
- `enterprise/connect`
- `cli/rail` (hidden from sidebar but indexed)

## Response shape

```json
{
  "ok": true,
  "slug": "enterprise/memdb-deploy",
  "title": "Deploy MemDB",
  "body": "…full plain text…"
}
```

## Steps

### 1. Resolve slug

From user URL `https://memtrace.io/docs/cli/connect` → slug `cli/connect`.

From `ask_docs` citations array → use slug directly.

From unknown topic → `search_docs` first, then `read_doc` on top hit.

### 2. Read and summarize

Read the `body`, answer the user, keep `/docs/<slug>` links when citing.

### 3. Multi-page topics

Enterprise deploy + engineer connect:

```json
{ "slug": "enterprise/memdb-deploy" }
```

then

```json
{ "slug": "enterprise/connect" }
```

## When NOT to use read_doc

| Situation | Use instead |
|---|---|
| User asked a natural-language question | `ask_docs` first |
| You don't know which page | `search_docs` first |
| User's **source code** in their repo | `memtrace-search` / `memtrace-first` |

## Offline behavior

`ok: false` → docs API unreachable. Do not substitute local README guesses.
