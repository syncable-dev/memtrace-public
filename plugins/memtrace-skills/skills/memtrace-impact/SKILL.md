---
name: memtrace-impact
description: "Quantify the blast radius of modifying a single symbol in an indexed codebase. USE when planning or reviewing a change to a specific function / method / class / type and you need to know who breaks, or when reviewing a PR to scope affected symbols from a diff. DO NOT USE for repo-wide overviews (→ memtrace-graph), for what-changed-over-time questions (→ memtrace-evolution), for finding code you don't have a symbol ID for yet (→ memtrace-search first), or for general 'explain this codebase' (→ memtrace-codebase-exploration)."
---

## What this gives you

Traverses the code knowledge graph from one symbol to report: upstream dependents (what breaks if this changes), downstream dependencies (what this relies on), transitive reach with decay, and a Low/Medium/High/Critical risk rating. Diff-mode additionally maps a raw patch to the set of affected symbols + execution flows.

## Tools

| Tool | Use when |
|------|----------|
| `get_impact` | You have a single symbol ID and want its blast radius |
| `detect_changes` | You have a git diff / patch text and need the symbols it touches |

## CRITICAL: parameter types are strict

Full schema for every Memtrace tool: **`../../references/mcp-parameters.md`**.

MCP validation is structural. A string where a number is expected fails with `MCP error -32602: invalid type: string, expected usize` and wastes a turn. Pass numeric fields as JSON numbers, booleans as booleans.

```json
// CORRECT
{ "symbol_id": "abc-123", "direction": "both", "depth": 3 }

// WRONG — fails validation
{ "symbol_id": "abc-123", "direction": "both", "depth": "3" }
```

## `get_impact` — full parameter schema

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `symbol_id` | string (UUID) | yes | — | Get from `find_symbol` / `find_code` results |
| `direction` | string enum | no | `"both"` | One of `"upstream"`, `"downstream"`, `"both"` |
| `depth` | integer | no | `3` | Traversal hops; 1–8 reasonable, >8 explodes |
| `include_types` | array of string | no | all edge types | E.g. `["CALLS","IMPORTS","IMPLEMENTS"]` |
| `limit` | integer | no | `100` | Max symbols returned in each direction |

## `detect_changes` — full parameter schema

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `diff` | string | yes | — | Unified git diff text; pipe `git diff` output directly |
| `repo_id` | string | no | auto-detect from diff headers | Pass to disambiguate in multi-repo setups |
| `branch_name` | string | no | `"main"` | Match the branch the diff applies to |

## Workflow

1. **No symbol ID yet?** Call `find_symbol` (exact name) or `find_code` (natural language) first — both return `id` values you feed to `get_impact`.
2. **Single symbol change:** `get_impact` with `direction: "both"` gives full picture. Drop to `"upstream"` for "who breaks" or `"downstream"` for "what do I rely on".
3. **PR review / multi-symbol change:** `detect_changes` with the diff text. Returns affected symbols and the execution flows (processes) that cross them.
4. **Act on the risk label:**

| Risk | Meaning | Next action |
|------|---------|-------------|
| Low | Leaf node, few dependents | Change freely; unit test is enough |
| Medium | Bounded dependents | Test direct callers; review interface contracts |
| High | Many dependents across modules | Full test suite; coordinate with module owners |
| Critical | Core infrastructure, wide transitive reach | Plan migration; prefer backward-compatible change |

## Common mistakes

| Mistake | Fix |
|---------|-----|
| Passing `depth: "3"` as string | Use JSON number `depth: 3` |
| Calling `get_impact` before you have an ID | Call `find_symbol`/`find_code` first, copy the `id` |
| Using `depth: 10+` on wide graphs | Stay at 3–5; you get thousands of symbols at higher depths |
| Using `get_impact` for a diff with multiple symbols | Use `detect_changes` instead — takes the diff once |
