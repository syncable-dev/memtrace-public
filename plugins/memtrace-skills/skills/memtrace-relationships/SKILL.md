---
name: memtrace-relationships
description: "Map graph relationships for a specific symbol in an indexed codebase — who calls it, what it calls, class hierarchy, overrides, imports, exports, type usages. USE when you need the neighbourhood of ONE symbol before modifying it. DO NOT USE for computing blast-radius risk ratings (→ memtrace-impact), for diff-based change scoping (→ memtrace-impact with detect_changes), for graph-wide centrality / bridges / communities (→ memtrace-graph), or for natural-language symbol discovery before you have an ID (→ memtrace-search)."
---

## What this gives you

One symbol's immediate graph neighbourhood. `get_symbol_context` is the preferred one-shot call — it returns direct callers, callees, type references, community, process membership, and cross-repo API callers in a single request. `analyze_relationships` is the targeted drill-down when you need one specific edge type at custom depth.

## Tools

| Tool | Use when |
|------|----------|
| `get_symbol_context` | ALMOST ALWAYS. 360° view in one call. |
| `analyze_relationships` | You need one specific `query_type` at depth > 2, or want a filtered result |

## CRITICAL: parameter types are strict

Full schema for every Memtrace tool: **`../../references/mcp-parameters.md`**.

MCP validation is structural. Pass numbers as JSON numbers, not strings. Example failure: `depth: "3"` → `MCP error -32602: invalid type: string, expected usize`.

## `get_symbol_context` — full parameter schema

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `symbol_id` | string (UUID) | yes | — | From `find_symbol` / `find_code` |

Returns: `{ symbol, callers, callees, type_references, community, processes, api_callers_cross_repo }`.

## `analyze_relationships` — full parameter schema

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `symbol_id` | string (UUID) | yes | — | From `find_symbol` / `find_code` |
| `query_type` | string enum | yes | — | See query-type table below |
| `depth` | integer | no | `2` | 1–5 reasonable; higher = slower + wider |
| `limit` | integer | no | `50` | Cap per-level results |

### `query_type` values

| Value | Finds |
|-------|-------|
| `"find_callers"` | Functions/methods that call this one |
| `"find_callees"` | Functions/methods this one calls |
| `"class_hierarchy"` | Parent classes, interfaces, mixins |
| `"overrides"` | Child classes that override this method |
| `"imports"` | Modules this file imports |
| `"exporters"` | Files that import this module |
| `"type_usages"` | Where this type / interface is referenced |

## Workflow

1. **No symbol ID yet?** Call `find_symbol` (exact name) or `find_code` (behaviour description) and copy the `id` from the first match.
2. **Broad context needed:** Call `get_symbol_context` first. Done in one request.
3. **Need depth or a specific edge type:** Call `analyze_relationships`. Keep `depth ≤ 3` unless there's a reason.
4. **Act on what you see:**

| Signal | Meaning | Next step |
|--------|---------|-----------|
| High in_degree (many callers) | Widely-used; changes ripple | Run `get_impact` before modifying |
| High out_degree (many callees) | Complex; refactoring candidate | See `memtrace-refactoring-guide` |
| Deep class hierarchy | Fragile-base-class risk | Check `overrides` before changing the base |
| Cross-repo API callers | Service boundary | Coordinate; treat as public API |
| Zero callers on public symbol | Possibly dead | Confirm with `find_dead_code` |

## Common mistakes

| Mistake | Fix |
|---------|-----|
| Making 4 `analyze_relationships` calls for callers+callees+hierarchy+types | Use one `get_symbol_context` call instead |
| Passing `depth: "2"` as string | JSON number `depth: 2` |
| `query_type: "callers"` (wrong enum) | Use `"find_callers"` — the `find_` prefix is required |
| Jumping to relationships before search | Get the `id` from `find_symbol`/`find_code` first |
