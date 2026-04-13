---
name: memtrace-cochange
description: "Use when the user asks what tends to change together with a symbol, what other code moves when this moves, historical coupling, blast awareness before modifying a symbol, or wants to find hidden dependencies not visible in the call graph"
allowed-tools:
  - mcp__memtrace__get_cochange_context
  - mcp__memtrace__find_symbol
  - mcp__memtrace__get_impact
user-invocable: true
---

## Overview

Find symbols that historically co-change with a target symbol — ranked by co-occurrence frequency across all episodes. This surfaces **behavioral coupling** that the static call graph cannot see.

`get_impact` answers "who calls this?" (structural).
`get_cochange_context` answers "what always moves when this moves?" (historical).

They are complementary. A symbol with no direct callers can still have strong cochange partners if it's always modified alongside another in every commit.

## Steps

### 1. Identify the target symbol

Use `find_symbol` if you need the exact name. The tool matches by `name` field.

### 2. Call `get_cochange_context`

```
get_cochange_context(
  repo_id: "...",
  symbol: "execute",    // exact symbol name
  limit: 20             // default 20, increase for broader view
)
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
