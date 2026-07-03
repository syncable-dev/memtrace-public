---
name: memtrace-search
description: "Find source code with Memtrace hybrid BM25+semantic search: symbols, functions, classes, types, constants, definitions, implementations, logic, or error strings inside code. Use when the user wants to find, search, locate, or look up code or asks where code lives. Do not use Grep, Glob, rg, find, or manual file search for code discovery. If Memtrace returns 0 results, broaden the Memtrace query and diagnose/reindex; do not switch to grep."
allowed-tools:
  - mcp__memtrace__find_code
  - mcp__memtrace__find_symbol
  - mcp__memtrace__get_source_window
  - mcp__memtrace__list_indexed_repositories
  - Read
metadata:
  author: "Syncable <support@syncable.dev>"
  version: "1.0.0"
  category: development
---

## Overview

Find code using hybrid BM25 + semantic search (RRF). Primary discovery tool — use before relationship or impact analysis.

## Quick Reference

| Tool | Best For |
|------|----------|
| `find_code` | Natural-language queries, broad searches |
| `find_symbol` | Exact identifier names |
| `get_source_window` | Bounded source read when harness lacks `Read(offset, limit)` |

> **Parameter types:** Numbers must be JSON numbers — not strings.

## `find_code` parameters

| Param | Required | Default | Notes |
|---|---|---|---|
| `query` | yes | — | Natural language or symbol text |
| `repo_id` | no | all repos | |
| `limit` | no | 20 | Max 100 |
| `file_path` | no | — | Path/directory substring filter |
| `as_of` | no | now | ISO-8601 time-travel |
| `include_diagnostics` | no | false | Set true for `id`, `score` in results |

**No `kind` param on `find_code`** — use `find_symbol(kind=...)` to filter by symbol type.

```json
{ "query": "authentication middleware", "repo_id": "memdb", "limit": 20 }
```

## `find_symbol` parameters

| Param | Required | Default | Notes |
|---|---|---|---|
| `name` | yes | — | Symbol name to search |
| `repo_id` | no | all repos | |
| `fuzzy` | no | false | API field exists; currently exact-match in backend |
| `edit_distance` | no | 2 | Only when fuzzy enabled |
| `kind` | no | — | `Function`, `Class`, `Method`, etc. |
| `file_path` | no | — | Path substring filter |
| `limit` | no | 10 | Max 50 |

```json
{ "name": "validateToken", "repo_id": "memdb", "file_path": "auth" }
```

Full parameter spec for every Memtrace tool: [references/mcp-parameters.md](../../references/mcp-parameters.md).

## Steps

### 1. Choose the tool

- Exact name → `find_symbol`
- Behaviour description → `find_code`
- All repos → omit `repo_id`

### 2. Execute search

Result shape: see [Output](#output) below.

### 3. Hand off to graph tools

Save **`name`**, **`scope_path`**, and **`file_path`** — **not** internal IDs:

```json
{ "repo_id": "memdb", "symbol": "validateToken" }
{ "repo_id": "memdb", "target": "validateToken", "direction": "both" }
{ "repo_id": "memdb", "target": "validateToken", "query_type": "find_callers" }
```

Read source only when editing — bounded `Read(offset, limit)` at returned lines.

### Multi-word queries

1. Try verbatim `find_code` query.
2. If weak, fan out: camelCase, snake_case, domain identifiers.
3. Dedupe top hits by `file_path:start_line`.

## Output

One `find_code` / `find_symbol` result entry:

```json
{
  "name": "validateToken",
  "kind": "Function",
  "file_path": "src/auth/token.rs",
  "start_line": 42,
  "scope_path": "auth::token"
}
```

`score` (and `id`) appear only with `include_diagnostics: true`.

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| `find_code(kind=...)` | **`kind` only on `find_symbol`** |
| Passing symbol `id` to graph tools | Use **`name`** as `symbol` / `target` |
| Assuming `fuzzy: true` always works | Backend is exact-match today — try spelling variants |
| Skipping `list_indexed_repositories` | Verify repo is indexed first |
