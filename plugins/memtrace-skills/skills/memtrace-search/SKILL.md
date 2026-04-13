---
name: memtrace-search
description: "Use when the user asks to find code, search for a function, locate a symbol, look up where something is defined, search across repos, find implementations, or needs to discover where a piece of logic lives before making changes"
allowed-tools:
  - mcp__memtrace__find_code
  - mcp__memtrace__find_symbol
  - mcp__memtrace__list_indexed_repositories
user-invocable: true
---

## Overview

Find code using hybrid BM25 full-text + semantic vector search with Reciprocal Rank Fusion. Works for both natural-language queries and exact symbol names. This is the primary discovery tool — use it before calling relationship or impact analysis tools.

## Quick Reference

| Tool | Best For |
|------|----------|
| `find_code` | Natural-language queries ("authentication middleware", "retry logic"), broad searches |
| `find_symbol` | Exact identifier names ("getUserById", "PaymentService"), when you know the name |

## Steps

### 1. Choose the right search tool

- **Know the exact name?** → Use `find_symbol` with `fuzzy: true` for typo tolerance
- **Describing behaviour?** → Use `find_code` with a natural-language query
- **Searching all repos?** → Omit `repo_id` from either tool

### 2. Execute the search

**find_code parameters:**
- `query` — natural-language or exact text (required)
- `repo_id` — scope to a single repo (optional; omit to search all)
- `kind` — filter by symbol type: Function, Class, Method, Interface, APIEndpoint, APICall
- `limit` — max results (default 10)
- `as_of` — ISO-8601 timestamp for time-travel search

**find_symbol parameters:**
- `name` — exact or partial symbol name (required)
- `fuzzy` — enable Levenshtein correction (default false)
- `repo_id` — scope to a single repo (optional)
- `kind` — filter by symbol type
- `file_path` — filter by file path substring

**Success criteria:** Results include `file_path`, `start_line`, `kind`, and relevance `score`.

### 3. Use results for next steps

Save the symbol `id` from results — pass it to:
- `analyze_relationships` to map callers/callees
- `get_symbol_context` for a 360-degree view
- `get_impact` to assess blast radius before changes

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| Searching without indexing first | Call `list_indexed_repositories` to verify the repo is indexed |
| Using find_symbol for vague queries | Use `find_code` for natural-language; `find_symbol` is for exact names |
| Ignoring the `kind` filter | Narrow results with kind=Function, kind=Class etc. to reduce noise |
| Re-searching to get more context | Use the symbol `id` with `get_symbol_context` instead of re-searching |
