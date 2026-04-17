---
name: memtrace-search
description: "Discover where code lives in an indexed codebase — find a function by name, locate behaviour described in natural language, or retrieve a symbol `id` for downstream graph tools. USE as the first step any time you need a symbol `id` and only have a name, description, or partial identifier. DO NOT USE once you already have a symbol `id` (→ memtrace-relationships / memtrace-impact), for graph-wide architecture questions (→ memtrace-graph), or for what-changed-over-time queries (→ memtrace-evolution)."
---

## What this gives you

Hybrid search over the code knowledge graph: Tantivy BM25 + semantic vector embeddings + Reciprocal Rank Fusion. Returns ranked symbols with `id`, `file_path`, `start_line`, `kind`, and `score`. The `id` is the entry point into every other Memtrace tool.

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

## CRITICAL: parameter types are strict

Full schema for every Memtrace tool: **`../../references/mcp-parameters.md`**. Most common pitfalls here:

* `limit`, `edit_distance` are JSON numbers. `limit: "20"` fails with `MCP error -32602: invalid type: string "20", expected usize`.
* `fuzzy` is a boolean — `true` not `"true"`.
* `kind` is a case-sensitive enum: `"Function"` not `"function"`.

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
