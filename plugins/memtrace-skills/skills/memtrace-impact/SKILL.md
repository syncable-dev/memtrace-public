---
name: memtrace-impact
description: "Compute the blast radius of modifying a symbol through transitive graph impact. Use when the user asks about blast radius, impact, what breaks, risk, upstream callers, downstream dependencies, or consequences of modifying a symbol, before or during source-code changes. Do not use Grep or manual reference search; Memtrace computes transitive graph impact. For a full risk-rated plan covering a multi-part change, use memtrace-change-impact-analysis."
---

## Overview

Compute the blast radius of changing a specific symbol. Traces upstream (what depends on this) and downstream (what this depends on) through the knowledge graph to quantify risk before making modifications.

## Quick Reference

| Tool | Purpose |
|------|---------|
| `get_impact` | Blast radius from a symbol **name** (`target`) |
| `detect_changes` | Scope symbols affected by a diff/patch |

> **Parameter types:** MCP parameters are strictly typed. Numbers (`depth`, `limit`, etc.) must be JSON numbers — not strings.

## Required parameters — `get_impact`

| Parameter | Required | Default | Notes |
|---|---|---|---|
| `repo_id` | yes | — | |
| `target` | yes | — | Symbol name (e.g. `"validateToken"`) — **not** `symbol_id` |
| `direction` | no | `"both"` | `"upstream"` \| `"downstream"` \| `"both"` |
| `depth` | no | `5` | BFS depth (max 15) |
| `as_of` | no | now | ISO-8601 for time-travel |

```json
{ "repo_id": "memdb", "target": "validateToken", "direction": "both", "depth": 5 }
```

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

## Steps

### 1. Identify the symbol name

If you don't know the exact name:
- `find_symbol(name="...")` for exact identifiers
- `find_code(query="...")` for natural-language queries

Use the returned **`name`** (and optional `file_path` hint) — graph tools resolve by name, not internal IDs.

### 2. Run impact analysis

```json
{
  "repo_id": "memdb",
  "target": "validateToken",
  "direction": "upstream",
  "depth": 5
}
```

### 3. Interpret the risk rating

| Risk | Meaning | Action |
|------|---------|--------|
| **Low** | Few dependents, leaf node | Safe to modify; minimal testing needed |
| **Medium** | Moderate dependents | Test direct callers; review interface contracts |
| **High** | Many dependents across modules | Coordinate changes; comprehensive test coverage |
| **Critical** | Core infrastructure, many transitive dependents | Plan migration strategy; backward-compatible changes |

### 4. For diff-based analysis

When you have an actual code diff, use `detect_changes`:

```json
{ "repo_id": "memdb", "diff": "<unified git diff text>" }
```

Or pass `changed_files: ["path/a.rs", "path/b.ts"]` when no diff text is available.

## Decision Points

| Situation | Action |
|-----------|--------|
| Changing a single function | `get_impact` with `direction: "both"` |
| Reviewing a PR or diff | `detect_changes` with the diff content |
| Renaming/removing a public API | `get_impact` with `direction: "upstream"`, higher `depth` |
| Refactoring internals | `get_impact` with `direction: "downstream"` |

## Output

`get_impact` returns the blast radius for the target symbol:

| Field | Example / meaning |
|---|---|
| Risk rating | `High` — one of `Low` / `Medium` / `High` / `Critical` (interpret via step 3) |
| Upstream | Transitive dependents (callers/importers) within `depth` hops — what breaks if the target changes |
| Downstream | Transitive dependencies — what the target relies on |

`detect_changes` returns the set of symbols affected by the supplied `diff` / `changed_files`, plus the affected processes (execution flows).

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| Passing `symbol_id` or `name` | Required param is **`target`** |
| Omitting `repo_id` | Both `repo_id` and `target` are required |
| Defaulting `depth` to 3 | API default is **5** |
