---
name: memtrace-relationships
description: "Use when the user asks who calls a function, what a function calls, class hierarchy, inheritance, imports, exports, type usages, dependencies between symbols, or wants to understand how code connects before making changes"
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

## Steps

### 1. Get the symbol ID

If you don't have a symbol `id`, find it first:
- Use `find_symbol` for exact names
- Use `find_code` for natural-language queries

### 2. Choose your approach

**Quick 360° view** → Use `get_symbol_context`
Returns in one call: direct callers, callees, type references, community membership, process membership, and cross-repo API callers.

**ALWAYS prefer `get_symbol_context` first** — it answers "what does this touch and what touches it?" faster than multiple `analyze_relationships` calls.

**Targeted traversal** → Use `analyze_relationships`
When you need a specific relationship type at a specific depth:
- `symbol_id` — the symbol to start from (required)
- `query_type` — one of the types above (required)
- `depth` — traversal hops, default 2 (higher = slower but reveals indirect deps)

### 3. Interpret results

- **High in_degree** (many callers) → widely-used symbol; changes have large blast radius
- **High out_degree** (many callees) → complex function; candidate for refactoring
- **Deep class hierarchy** → check for Liskov violations or fragile base class issues
- **Cross-repo API callers** → changes require coordination with other teams/services

### 4. Follow up

After understanding relationships, consider:
- `get_impact` to quantify the blast radius of a change
- `get_evolution` to see how this symbol has changed over time
- `find_dead_code` if you found unreferenced symbols

## Decision Points

| Situation | Action |
|-----------|--------|
| Need broad context fast | Use `get_symbol_context` (one call, full picture) |
| Need specific relationship at depth >2 | Use `analyze_relationships` with custom depth |
| Symbol has many callers | Follow up with `get_impact` before modifying |
| Found cross-repo API callers | This is a service boundary — coordinate changes |
