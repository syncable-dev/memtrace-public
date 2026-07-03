---
name: memtrace-quality
description: "Find dead code, complexity hotspots, and refactoring candidates in indexed source code. Use when the user asks about source-code quality, dead code, unused functions, zero callers, complexity, cyclomatic complexity, hotspots, refactoring candidates, or code smell questions. Do not use Grep, Glob, rg, or manual reference search for unused code; Memtrace uses graph reachability and complexity metrics."
allowed-tools:
  - mcp__memtrace__find_dead_code
  - mcp__memtrace__calculate_cyclomatic_complexity
  - mcp__memtrace__find_most_complex_functions
  - mcp__memtrace__get_repository_stats
  - mcp__memtrace__get_evolution
metadata:
  author: "Syncable <support@syncable.dev>"
  version: "1.0.0"
  category: development
---

## Overview

Code quality via graph analysis — dead code, complexity hotspots, repository stats.

## Quick Reference

| Tool | Purpose | Required | Key optional |
|------|---------|----------|--------------|
| `find_dead_code` | Zero-caller symbols | `repo_id` | `include_tests`, `limit`, `kinds[]` |
| `find_most_complex_functions` | Ranked complexity hotspots | `repo_id` | **`top_n`** (def 20) — not `limit` |
| `calculate_cyclomatic_complexity` | Score one symbol | `repo_id`, **`target`** | — |
| `get_repository_stats` | Repository overview | `repo_id` | `branch` |

> **Parameter types:** numbers must be JSON numbers — `limit: 20`, never `"20"` (MCP error -32602).

Full parameter spec for every Memtrace tool: [references/mcp-parameters.md](../../references/mcp-parameters.md).

## Steps

### 1. Repository overview

```json
{ "repo_id": "memdb" }
```

### 2. Dead code

```json
{ "repo_id": "memdb", "include_tests": false, "limit": 50 }
```

Exported symbols and entry points excluded by default. Results are candidates, not proof — callers via dynamic dispatch or reflection are invisible to the graph; verify before deleting.

### 3. Complexity hotspots

```json
{ "repo_id": "memdb", "top_n": 10 }
```

Ordered by call-graph out-degree.

| Score | Rating | Action |
|-------|--------|--------|
| <5 | Low | Fine |
| 5-10 | Medium | Consider simplifying |
| 10-20 | High | Refactor candidate |
| >20 | Critical | Refactor priority |

### 4. Single symbol complexity

```json
{ "repo_id": "memdb", "target": "processOrder" }
```

## Output

| Result | Carries |
|--------|---------|
| Dead-code entry (`find_dead_code`) | Symbol name + kind with zero graph callers — e.g. `format_legacy` (Function) |
| Complexity row (`find_most_complex_functions`) | Symbol, complexity score, risk level — e.g. `processOrder` — 27, Critical |
| Single score (`calculate_cyclomatic_complexity`) | Cyclomatic complexity for `target` |
| Stats (`get_repository_stats`) | Node counts by kind, edge counts, community and process counts for `repo_id`/`branch` |

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| `find_most_complex_functions(limit: 10)` | Param is **`top_n`** |
| `calculate_cyclomatic_complexity(symbol_id=...)` | Required param is **`target`** |
| Only looking at the highest complexity | Medium-complexity functions that are growing (check `get_evolution`) are often more urgent |
