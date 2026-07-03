---
name: memtrace-session-continuity
description: "Catch up on everything that changed in an indexed source-code repo since the last session, using stored session anchors and Memtrace change memory. Use when the user asks to continue, catch up, resume, see what changed while away, recover prior context, or orient at session start without guessing timestamps. Do not use git log, Grep, or manual file search for catch-up; Memtrace provides session anchors and change memory."
allowed-tools:
  - mcp__memtrace__get_changes_since
  - mcp__memtrace__list_indexed_repositories
  - mcp__memtrace__get_evolution
metadata:
  author: "Syncable <support@syncable.dev>"
  version: "1.0.0"
  category: development
---

## Overview

Session continuity for agents. Store a timestamp from your last session and call `get_changes_since` to see exactly what happened since then — then drill into details with `get_evolution` if needed.

**Core principle:** Persist a `since` timestamp (or the `until` from your last catch-up call). Never invent a `days` parameter.

## Steps

### 1. Find or bootstrap the session anchor

Look for a stored anchor from your last session:

```json
{
  "repo_id": "memdb",
  "since": "2026-04-13T10:43:00Z"
}
```

If you have no anchor yet (first run), call `list_indexed_repositories`. Use `last_episode_time` from the repo entry as a bootstrap `since` value, or ask the user how far back to look.

### 2. Call `get_changes_since`

```json
{
  "repo_id": "memdb",
  "since": "2026-04-13T10:43:00Z"
}
```

Optional: pass `until` to cap the upper bound (defaults to now).

**Required param is `since`** — not `last_episode_id`, not `days`. Same time formats as `get_evolution.from` (`"7d ago"`, ISO-8601, etc.).

Full parameter spec for every Memtrace tool: [references/mcp-parameters.md](../../references/mcp-parameters.md).

### 3. Interpret the response

Response shape: see [Output](#output) below. Act on `totals.episode_count`:

| `totals.episode_count` | Action |
|---|---|
| `0` | Nothing changed — safe to proceed |
| `1–20` | Review each episode's `touched_files` and change counts |
| Many episodes | Scan `touched_files` for overlap with your task; drill down with `get_evolution` |

### 4. Decide whether changes are relevant

```
episodes returned
├── Any touched_files overlap your current task area?
│   ├── YES → call get_evolution(from: since, mode: compound) for hotspots
│   └── NO  → safe to continue
└── Need per-commit detail?
    └── get_evolution(from: since, mode: recent, limit: 100)
```

### 5. Always persist the new anchor

Store the `until` value from the response (or the latest episode's `reference_time`) for next session:

```json
{
  "repo_id": "memdb",
  "since": "2026-04-13T14:22:00Z"
}
```

## Output

`get_changes_since` returns:

| Field | Meaning |
|---|---|
| `episodes[]` | Each episode since `since`, with `touched_files`, `nodes_added/removed`, `reference_time` |
| `totals.episode_count` | How many episodes in the window — `0` means nothing changed |
| `since` / `until` | The resolved window boundaries |

```json
{
  "since": "2026-04-13T10:43:00Z",
  "until": "2026-04-13T14:22:00Z",
  "episodes": [
    { "reference_time": "2026-04-13T12:05:00Z", "touched_files": ["src/engine/page_cache.rs"], "nodes_added": 4, "nodes_removed": 1 }
  ],
  "totals": { "episode_count": 1 }
}
```

## Common mistakes

| Mistake | Reality |
|---|---|
| Passing `last_episode_id` or `days` | `get_changes_since` requires `since` — a timestamp string |
| Using `get_evolution` without `from` | Always pass `from` (e.g. `"7d ago"`) — never omit it or use `days` |
| Expecting `status`, `by_module`, or `session_anchor` in the response | Not in the current shape — use `episodes[]` and `totals` |
| Discarding the stored `since` timestamp | Without it, next session reverts to guessing |
| Calling this repeatedly within one session | Call once at session start; reuse the result for the rest of the session |
