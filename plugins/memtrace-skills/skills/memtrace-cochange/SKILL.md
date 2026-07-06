---
name: memtrace-cochange
description: "Find files that historically co-change with a target symbol or file, ranked by co-occurrence across git episodes. Use when the user asks about historical coupling, co-change, what changes with this, hidden dependencies, or what else needs to move for source code. Do not use git log, git diff, Grep, or manual file search to correlate changes; Memtrace queries co-change and temporal graph data directly."
---

## Overview

Find **files** that historically co-change with a target symbol or file path — ranked by co-occurrence frequency across git episodes. Surfaces **behavioral coupling** the static call graph cannot see.

`get_impact` answers "who calls this?" (structural).
`get_cochange_context` answers "what files always move when this moves?" (historical, file-level).

They are complementary. A file with no call-graph edges to the target can still be a strong cochange partner if it's always modified alongside it in every commit.

> **Parameter types:** Numbers (`limit`, `window_days`, etc.) must be JSON numbers — not strings.

## Required parameters

| Parameter | Required | Default | Notes |
|---|---|---|---|
| `repo_id` | yes | — | |
| `target` | yes | — | Symbol name **or** file path substring — **not** `symbol` |
| `limit` | no | **10** | Max cochanged files returned |
| `window_days` | no | **30** | Lookback from `as_of` |
| `as_of` | no | now | Window anchor |
| `branch` | no | any branch | |

```json
{
  "repo_id": "memdb",
  "target": "execute",
  "limit": 10,
  "window_days": 30
}
```

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

## Output

```json
{
  "cochanged_files": [
    {
      "file_path": "src/order/types.rs",
      "cochange_count": 8,
      "last_cochanged_at": "2026-04-13T10:43:00Z"
    }
  ],
  "target_files": ["src/order/service.rs"]
}
```

There is **no** `cochanges[]` with symbol names — results are **file-level**.

## Steps

### 1. Identify the target

Use `find_symbol` if needed. Pass the symbol **`name`** or a **`file_path`** as `target`.

### 2. Call `get_cochange_context`

See required parameters above.

### 3. Interpret results

High `cochange_count` on a file → strong historical coupling. When you modify the target, review those files too — even without direct call-graph edges.

### 4. Cross-reference with call graph

For symbols in cochanged files, optionally run `get_impact(target=...)`:

| Structural coupling | Historical coupling | Interpretation |
|---|---|---|
| Yes | Yes | Core dependency — highest risk |
| No | Yes | Hidden coupling — history-only |
| Yes | No | Called often but changed independently |

## Use Cases

- **Before modifying a symbol** — get blast awareness beyond what `get_impact` shows
- **Incident investigation** — when `get_impact` doesn't explain the blast radius, check cochange history
- **Code review** — verify that a PR touched all historically-coupled partners
- **Refactoring** — discover implicit coupling before extracting a module

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| Only using `get_impact` for blast radius | Structural coupling misses behavioral coupling — always pair with cochange |
| Ignoring cochanged files with no call-graph edges | A rarely-called file with high `cochange_count` is a strong coupling signal |
| Using cochange as a dependency map | It's a change correlation, not a dependency graph — files can cochange without any direct relationship |
| Passing `symbol:` | Required param is **`target`** |
| Expecting `cochanges[]` with symbol names | Response is **`cochanged_files[]`** (file paths) |
| Using `limit: 20` as default | API default is **10** |
| Empty results with 0 git episodes | Run `replay_history` during indexing to populate co-change data |
