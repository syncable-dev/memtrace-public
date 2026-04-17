---
name: memtrace-quality
description: "Find code-quality issues in an indexed codebase — dead code, complexity hotspots, repo-wide stats. USE when the user asks for dead code / unused functions, cyclomatic complexity, refactoring candidates, code smells, or repo counts. DO NOT USE for blast-radius analysis of a specific change (→ memtrace-impact), for graph-wide architecture / centrality questions (→ memtrace-graph), or for executing the refactor itself (→ memtrace-refactoring-guide)."
---

## What this gives you

Pure structural quality signals from the graph — no style rules, no lint. Dead-code = zero incoming CALLS edges (with exclusions). Complexity = out-degree (callees) proxy + true cyclomatic when requested per-symbol. Repo stats = node/edge/community/process counts.

## Tools

| Tool | Use when |
|------|----------|
| `get_repository_stats` | One-shot repo counts — nodes, edges, communities, processes |
| `find_dead_code` | List symbols with zero callers across a repo |
| `find_most_complex_functions` | Top-N out-degree hotspots across a repo |
| `calculate_cyclomatic_complexity` | True McCabe complexity for ONE specific symbol |

## CRITICAL: parameter types are strict

Full schema for every Memtrace tool: **`../../references/mcp-parameters.md`**.

`limit`, `min_complexity`, `threshold` are JSON numbers. `include_tests` is a JSON boolean (`true`/`false`, not `"true"`). String `repo_id` must be quoted.

## `get_repository_stats` — parameter schema

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | From `list_indexed_repositories` |
| `branch_name` | string | no | `"main"` | |

## `find_dead_code` — parameter schema

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `branch_name` | string | no | `"main"` | |
| `include_tests` | boolean | no | `false` | `true` also flags unused test helpers |
| `limit` | integer | no | `50` | Max results |
| `kinds` | array of string | no | all | Filter, e.g. `["Function","Method"]` |

Exported symbols and process entry points are excluded automatically — public APIs never appear as "dead".

## `find_most_complex_functions` — parameter schema

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `branch_name` | string | no | `"main"` | |
| `limit` | integer | no | `10` | Top-N to return |
| `min_complexity` | integer | no | `0` | Cutoff score (out-degree based) |

## `calculate_cyclomatic_complexity` — parameter schema

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `symbol_id` | string (UUID) | yes | — | From `find_symbol` / `find_code` |

## Workflow

1. **Start with `get_repository_stats`** — know the scale before drilling in.
2. **Find dead code / complexity hotspots** separately with the two list tools.
3. **For individual symbols flagged as complex**, call `calculate_cyclomatic_complexity` to confirm with true McCabe score before recommending changes.

### Complexity thresholds (out-degree proxy)

| Score | Rating | Action |
|-------|--------|--------|
| < 5 | Low | No action |
| 5–10 | Medium | Monitor; check `get_evolution` for growth trend |
| 10–20 | High | Refactor; extract helpers |
| > 20 | Critical | Split immediately; single-responsibility violation |

## Common mistakes

| Mistake | Fix |
|---------|-----|
| Deleting everything `find_dead_code` returns | Reflection, dynamic dispatch, and external consumers aren't visible; confirm with grep + git blame before deletion |
| Passing `include_tests: "true"` as string | JSON boolean `true` |
| Fixing only the top-complexity function | Pair with `get_evolution` — medium-complexity that's growing is often more urgent |
| Calling `calculate_cyclomatic_complexity` on a non-function | Only meaningful for Function / Method symbols |
