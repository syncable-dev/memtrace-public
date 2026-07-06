---
name: memtrace-docs-search
description: "Search official Memtrace documentation with semantic full-text search. Use when the user wants to find doc pages, locate a guide, or discover which docs cover a topic (fleet, MCP, CLI, enterprise MemDB, skills). Calls search_docs on memtrace.io. Do not grep local files or web-search for Memtrace product docs."
---

## Overview

`search_docs` queries the hosted Memtrace docs corpus (same index as memtrace.io/docs
and the Ask AI widget). Returns ranked **chunks** — not full pages. Follow with
`read_doc` when you need the complete page.

Umbrella routing: `memtrace-docs` workflow.

## Quick Reference

| Tool | Best for |
|------|----------|
| `search_docs` | Find which doc pages/sections match a topic |
| `read_doc` | Load full page text after search hits |

## `search_docs` parameters

| Param | Required | Default | Notes |
|---|---|---|---|
| `query` | yes | — | Natural-language search string |
| `limit` | no | 8 | Max chunks to return |

```json
{ "query": "deploy MemDB helm azure", "limit": 8 }
```

## Steps

### 1. Search

```json
{ "query": "memtrace rail enable hook", "limit": 6 }
```

### 2. Inspect results

Each hit includes:

```json
{
  "slug": "cli/rail",
  "pageTitle": "memtrace rail & route",
  "h2Title": "Subcommands",
  "h2Id": "enable-sub",
  "excerpt": "…",
  "distance": 0.12
}
```

Lower `distance` ≈ better match.

### 3. Read full pages when needed

```json
{ "slug": "cli/rail" }
```

Slug uses forward slashes: `enterprise/memdb-deploy`, `mcp/tools`, `getting-started`.

## Common slugs

| Topic | Slug |
|---|---|
| Getting started | `getting-started` |
| MCP tools list | `mcp/tools` |
| MCP docs tools | `mcp/docs-tools` |
| Agent skills | `mcp/skills` |
| Deploy MemDB (enterprise) | `enterprise/memdb-deploy` |
| Connect to shared MemDB | `enterprise/connect` |
| memtrace connect CLI | `cli/connect` |
| Rail | `cli/rail` |

## Offline behavior

If `ok: false`, the docs API is unreachable. Report the error; do not fall back to
hallucinated documentation. Local code-graph tools are unaffected.

## Hand off

- Natural-language "how do I…" → `memtrace-docs-ask` (`ask_docs`) instead
- Found the right slug → `memtrace-docs-read` (`read_doc`)
- User's **source code** → `memtrace-first` / `memtrace-search` (different corpus)
