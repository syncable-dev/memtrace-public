---
name: memtrace-evolution
description: "Trace source-code change history from Memtrace's symbol-level temporal memory. Use when the user asks about change history, recent modifications, what changed since a date, symbol timeline, evolution, unexpected changes, or incident timelines. Do not use git log, git diff, Grep, or manual file search to reconstruct history. Do NOT use for current-state architecture overviews (use memtrace-codebase-exploration) or for replaying one commit/save's graph diff (use memtrace-episode-replay)."
---

## Overview

Temporal analysis for "what changed in this repo between two points in time?" Works on both git commits and uncommitted working-tree saves.

## Required parameters — read before calling

`get_evolution` **always** requires `repo_id` and `from`. There is no `days` parameter.

| Parameter | Required | Type | Example |
|---|---|---|---|
| `repo_id` | yes | string | `"memdb"` |
| `from` | yes | string | `"90d ago"`, `"2026-04-01T00:00:00Z"`, `"yesterday"` |
| `to` | no | string | defaults to now; set to incident time when investigating |
| `mode` | no | string | `"recent"` (default), `"compound"`, `"summary"`, `"overview"` |
| `target` | no | string | symbol name or file path substring to scope results |
| `branch` | no | string | branch filter |

> **Parameter types:** MCP parameters are strictly typed. Numbers (`limit`, `cursor`, etc.) must be JSON numbers — not strings. Use `limit: 100`, never `limit: "100"`.

### Time-window parameters — do not confuse these tools

| Tool | Time param | Example | Notes |
|---|---|---|---|
| `get_evolution` | **`from`** (required) | `"90d ago"` | optional `to`; **never** pass `days` |
| `get_changes_since` | **`since`** (required) | `"2026-04-13T10:43:00Z"` | optional `until` |
| `replay_history` | **`days`** (optional) | `90` | re-walks git history — different tool entirely |

Common failure: `MCP error -32602: missing field 'from'` — you omitted `from` or passed `days` instead.

```json
// CORRECT — 90-day overview
{ "repo_id": "memdb", "from": "90d ago", "mode": "overview" }

// WRONG — days is not a get_evolution parameter
{ "repo_id": "memdb", "days": 90, "mode": "overview" }
```

## Query modes — only these four exist

| Mode | What you get | Best for |
|---|---|---|
| `recent` | Per-episode list with nodes/edges added/removed; paginate with `limit` + `cursor` | Incident timelines, walking commit-by-commit |
| `compound` | Totals plus `top_changed_files` and `top_touched_symbols` | "What changed?" when you need hotspots |
| `summary` | Totals plus `first_episode` / `last_episode` metadata | Cheapest window check |
| `overview` | Alias for `summary` | Same as `summary` |

Modes **`impact`**, **`novel`**, and **`directional`** are **not implemented** — calling them returns `unknown mode`.

### `recent` mode filters (ignored by `compound` / `summary`)

| Param | Purpose |
|---|---|
| `file_path` | substring match against episode `touched_files` |
| `kind` | `"git_commit"` or `"working_tree"` |
| `limit` | page size (default 100 episodes) |
| `cursor` | pagination offset; follow `next_cursor` in response |

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

## Steps

### 1. Get `repo_id`

```
list_indexed_repositories()
```

### 2. Choose the time window

- `from` — start (required). Relative strings work: `"7d ago"`, `"24 hours ago"`.
- `to` — end (optional, defaults to now). Set to the incident timestamp when investigating regressions.

If resuming a session and you have a stored timestamp from last time, prefer `get_changes_since(since=...)` — see `memtrace-session-continuity`.

### 3. Choose the mode

```
User wants...
├── per-commit / per-save changelog     → recent
├── top changed files + symbols         → compound
├── quick totals only                   → summary or overview
└── one symbol's full history           → get_timeline (not get_evolution)
```

### 4. Execute

```json
// General "what changed lately?"
{ "repo_id": "memdb", "from": "30d ago", "mode": "compound" }

// Changes in one file last week
{ "repo_id": "memdb", "from": "7d ago", "mode": "recent", "file_path": "auth.ts", "limit": 50 }

// Incident: 24h before failure until failure time
{ "repo_id": "memdb", "from": "2026-04-16T13:00:00Z", "to": "2026-04-17T13:00:00Z", "mode": "recent" }
```

### 5. Interpret results

**`recent` mode** — each entry in `episodes[]`:
- `episode` — metadata (`source_type`, `reference_time`, `touched_files`, …)
- `nodes_added`, `nodes_removed`, `edges_added`, `edges_removed`

**`compound` mode:**
- `totals` — aggregate counts
- `top_changed_files` — files with most activity
- `top_touched_symbols` — symbols with most activity

**`summary` / `overview` mode:**
- `totals` — episode and node/edge counts
- `first_episode`, `last_episode` — window boundaries

### 6. Drill deeper

| Need | Tool |
|---|---|
| Full history of one symbol | `get_timeline(repo_id, scope_path, file_path)` |
| Blast radius of a suspect symbol | `get_impact(repo_id, target)` |
| What a diff would affect | `detect_changes(repo_id, diff=...)` |
| Behavioral coupling | `get_cochange_context(repo_id, target=<symbol>)` |
| Sub-commit tried-and-reverted history | `get_episode_replay` |

## Output

Sample `compound` payload (per-mode field lists: step 5 above):

```json
{
  "totals": { "episodes": 42, "nodes_added": 310, "nodes_removed": 95,
              "edges_added": 541, "edges_removed": 120 },
  "top_changed_files":   [ /* files ranked by activity */ ],
  "top_touched_symbols": [ /* symbols ranked by activity */ ]
}
```

`recent` returns `episodes[]` + `totals` + `page.next_cursor`; `summary`/`overview` return `totals` + `first_episode`/`last_episode`.

## Common mistakes

| Mistake | Reality |
|---|---|
| Passing `days: 90` | Use `from: "90d ago"` — `days` is only on `replay_history` |
| Omitting `from` | Required — call fails with `-32602` before any query runs |
| Using `mode: "novel"` / `"impact"` / `"directional"` | Not implemented — use `compound` + `get_impact` + `get_cochange_context` instead |
| Expecting `by_module[]` or significance scores | Not in current response shape — use `top_changed_files` from `compound` |
| Using `overview` when you need file/symbol detail | `overview`/`summary` are totals only — switch to `compound` or `recent` |
| Using `get_evolution` for one symbol's history | Use `get_timeline` |
| Guessing timestamps when resuming work | Use `get_changes_since(since=<stored time>)` — see `memtrace-session-continuity` |
| Applying `file_path` filter in `compound` mode | Filters are ignored in roll-up modes — use `recent` with `file_path` instead |
