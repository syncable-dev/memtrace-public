---
name: memtrace-search
description: "Use when the user asks to find code, search for a function, locate a symbol, look up where something is defined, search across repos, find implementations, or needs to discover where a piece of logic lives before making changes"
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

> **Parameter types:** numbers must be JSON numbers, not strings. `limit: 20` is correct; `limit: "20"` returns `MCP error -32602: expected usize`.

**`find_code` parameters:**
- `query` — string, required. Natural-language or exact text.
- `repo_id` — string, optional. Scope to a single repo (omit to search all).
- `kind` — string, optional. Filter by symbol type: `"Function"`, `"Class"`, `"Method"`, `"Interface"`, `"APIEndpoint"`, `"APICall"`.
- `limit` — **integer**, optional. Max results. Default `20`, capped at `100`.
- `as_of` — string, optional. ISO-8601 timestamp for time-travel search (e.g. `"2026-04-01T00:00:00Z"`).
- `file_path` — string, optional. File path or directory substring to constrain results (e.g. `"cli/commands"` or `"auth.py"`).

**`find_symbol` parameters:**
- `name` — string, required. Exact or partial symbol name (e.g. `"ValidateToken"`).
- `fuzzy` — boolean, optional. Enable Levenshtein correction. Default `false`.
- `edit_distance` — **integer**, optional. Maximum Levenshtein edit distance for fuzzy search. Default `2`, capped at `2`.
- `repo_id` — string, optional. Scope to a single repo.
- `kind` — string, optional. Filter by symbol type (e.g. `"Function"`, `"Class"`, `"Variable"`).
- `file_path` — string, optional. Filter by file path substring.
- `limit` — **integer**, optional. Max results. Default `10`, capped at `50`.

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
