---
name: memtrace-cochange
description: "Find symbols that historically change together with a target symbol in an indexed codebase — ranked by co-occurrence across git episodes. USE when you want blast-awareness BEYOND the static call graph (hidden coupling: sibling files that always get edited together, config↔code pairs, test↔impl drift). DO NOT USE for structural 'who calls this' questions (→ memtrace-impact / memtrace-relationships — that's the call graph), for general what-changed-when questions (→ memtrace-evolution), or on a freshly indexed repo with no git history replayed yet (call `replay_history` first)."
---

## Overview

Find symbols that historically co-change with a target symbol — ranked by co-occurrence frequency across all episodes. This surfaces **behavioral coupling** that the static call graph cannot see.

`get_impact` answers "who calls this?" (structural).
`get_cochange_context` answers "what always moves when this moves?" (historical).

They are complementary. A symbol with no direct callers can still have strong cochange partners if it's always modified alongside another in every commit.

## CRITICAL: parameter types are strict

Full schema for every Memtrace tool: **`../../references/mcp-parameters.md`**.

Cochange takes a **symbol NAME (string), not an ID**. This is the one Memtrace tool where you feed in a name rather than a UUID.

## `get_cochange_context` — parameters

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | From `list_indexed_repositories` |
| `symbol` | string | yes | — | Exact symbol NAME (not UUID), e.g. `"validateToken"` |
| `branch` | string | no | `"main"` | |
| `limit` | integer | no | `20` | JSON number, NOT `"20"` |

## Workflow

1. **Confirm the name exists** — run `find_symbol` with the name to verify spelling and see all matches. Pick the right one.
2. **Call `get_cochange_context`** with `repo_id` and the exact `symbol` string.

```json
{ "repo_id": "memtrace", "symbol": "validateToken", "limit": 20 }
```

### 3. Interpret results

The response contains `cochanges[]`, each with:
- `name` — symbol name
- `kind` — Function / Method / Class / Struct
- `file_path` — where it lives
- `cochange_count` — how many episodes it shared with the target

```
High cochange_count = strong historical coupling
→ If you modify the target, you will likely need to touch this too
→ Or it may be the real root cause you should investigate first
```

### 4. Cross-reference with call graph

For the top cochange partners, optionally run `get_impact` to see if the coupling is also structural:

| Structural coupling | Historical coupling | Interpretation |
|---|---|---|
| Yes | Yes | Core architectural dependency — highest risk |
| No | Yes | Hidden coupling — only visible through history |
| Yes | No | Called frequently but changed independently — lower risk |

## When to Use

- **Before modifying a symbol** — get blast awareness beyond what `get_impact` shows
- **Incident investigation** — when `get_impact` doesn't explain the blast radius, check cochange history
- **Code review** — verify that a PR touched all historically-coupled partners
- **Refactoring** — discover implicit coupling before extracting a module

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| Only using `get_impact` for blast radius | Structural coupling misses behavioral coupling — always pair with cochange |
| Ignoring low-`in_degree` cochange partners | A rarely-called utility with high cochange_count is a strong coupling signal |
| Using cochange as a dependency map | It's not a dependency graph — it's a change correlation. Two symbols can cochange without any direct relationship. |
