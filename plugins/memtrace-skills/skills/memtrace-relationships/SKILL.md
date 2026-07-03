---
name: memtrace-relationships
description: "Map source-code relationships between symbols. Use when the user asks about callers, callees, references, imports, exports, type usages, class hierarchy, inheritance, implementations, overrides, or dependencies between symbols. Do not use Grep, Glob, rg, find, or manual text search for references; Memtrace traverses typed AST graph edges."
allowed-tools:
  - mcp__memtrace__analyze_relationships
  - mcp__memtrace__get_symbol_context
  - mcp__memtrace__find_symbol
  - mcp__memtrace__find_code
  - mcp__memtrace__get_impact
  - mcp__memtrace__get_timeline
  - mcp__memtrace__get_evolution
metadata:
  author: "Syncable <support@syncable.dev>"
  version: "1.0.0"
  category: development
---

## Overview

Traverse the code knowledge graph to map relationships between symbols — callers, callees, class hierarchies, imports, exports, and type usages. Essential for understanding a symbol's neighbourhood before modifying it.

## Quick Reference

| query_type | What it finds |
|------------|---------------|
| `find_callers` | What calls this function/method? |
| `find_callees` | What does this function call? |
| `class_hierarchy` | Parent classes, interfaces, mixins |
| `overrides` | Which child classes override this method? |
| `imports` | What modules does this file import? |
| `exporters` | Which files import this module? |
| `type_usages` | Where is this type/interface referenced? |

> **Parameter types:** Numbers (`depth`, etc.) must be JSON numbers — not strings.

## Graph tool parameters

### `get_symbol_context` (360° view — prefer this first)

Required: `repo_id`, `symbol`

```json
{ "repo_id": "memdb", "symbol": "validateToken", "file_path": "src/auth.ts" }
```

Returns: callers, callees, type references, community, processes, cross-repo API callers.

### `analyze_relationships` (targeted traversal)

Required: `repo_id`, `target`, `query_type`

```json
{
  "repo_id": "memdb",
  "target": "validateToken",
  "query_type": "find_callers",
  "depth": 3,
  "file_path": "src/auth.ts"
}
```

| Param | Default | Notes |
|---|---|---|
| `depth` | **3** (max 10) | Not 2 |
| `file_path` | — | Disambiguates overloaded names |

Full parameter spec for every Memtrace tool: [references/mcp-parameters.md](../../references/mcp-parameters.md).

## Steps

### 1. Get the symbol name

Use `find_symbol` or `find_code`. Save **`name`**, **`scope_path`**, and **`file_path`** from results — pass `symbol`/`target` plus optional `file_path` to graph tools.

### 2. Choose your approach

**Quick 360° view** → `get_symbol_context` (one call).

**Targeted traversal** → `analyze_relationships` when you need a specific `query_type` at custom `depth`.

### 3. Interpret results

Use `get_symbol_context` fields (callers, callees, community, processes) for blast-radius and architecture context. For degree-style metrics on a specific symbol, follow up with `get_impact`.

### 4. Follow up

- `get_impact(repo_id, target=...)` — quantify blast radius
- `get_timeline(repo_id, scope_path, file_path)` — full version history
- `get_evolution(repo_id, from=..., target=...)` — window-scoped activity

## Output

`get_symbol_context` returns the symbol's 360° neighbourhood:

```json
{
  "symbol": "validateToken",
  "callers": ["..."],
  "callees": ["..."],
  "type_references": ["..."],
  "community": "...",
  "processes": ["..."],
  "api_callers_cross_repo": ["..."]
}
```

`analyze_relationships` returns the symbols/edges matching the requested `query_type` (no degree metrics — see Common Mistakes).

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| Passing `symbol_id` | Use **`target`** (`analyze_relationships`) or **`symbol`** (`get_symbol_context`) |
| Assuming default `depth: 2` | Default is **3** |
| Expecting `in_degree` on `analyze_relationships` results | Use `get_symbol_context` or `get_impact` for centrality/risk context |
