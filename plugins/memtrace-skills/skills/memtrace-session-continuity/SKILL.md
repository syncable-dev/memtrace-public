---
name: memtrace-session-continuity
description: "Use at the start of any session to check what changed since last time, when resuming work after a break, when an agent needs to orient itself without guessing timestamps, or when asked 'what changed while I was away'"
allowed-tools:
  - mcp__memtrace__get_changes_since
  - mcp__memtrace__list_indexed_repositories
  - mcp__memtrace__get_evolution
user-invocable: true
---

## Overview

Session continuity for agents. Instead of guessing a time window and blindly running `get_evolution`, pass a `session_anchor` from your last session and get back exactly what changed — nothing more. The response returns a new anchor to persist for next time.

**Core principle:** Agents track a cursor, not a clock. Never guess timestamps.

## Steps

### 1. Find or bootstrap the session anchor

Look for a stored `session_anchor` from your last session:

```json
{
  "last_episode_id": "ep_abc123",
  "last_reference_time": "2026-04-13T10:43:00Z"
}
```

If you have no anchor yet (first run), call `list_indexed_repositories`. Each repo now includes `last_episode_id`, `last_episode_time`, and `last_episode_type` — use `last_episode_id` as your bootstrap anchor.

### 2. Call `get_changes_since`

```
get_changes_since(
  repo_id: "...",
  last_episode_id: "ep_abc123"      // preferred — exact episode boundary
  // OR
  last_reference_time: "2026-04-13T10:43:00Z"   // fallback
)
```

### 3. Interpret the response

| `status` | Meaning | Action |
|---|---|---|
| `no_changes` | Nothing changed since your anchor | Safe to proceed; store new anchor |
| `changes_detected` | Full symbol-level delta returned | Review `modified[]`, `added[]`, `removed[]` |
| `changes_detected_overview` | >500 candidates — module rollup only | Check `by_module` for affected areas |
| `error` | Bad anchor or unknown episode | Fall back to `last_reference_time` or re-index |

### 4. Decide whether changes are relevant

```
changes_detected
├── Check modified[]/added[]/removed[] — do any overlap with your current task?
│   ├── YES → understand what changed before proceeding
│   └── NO  → safe to continue, update anchor

changes_detected_overview (large window)
├── Check by_module — does any changed module overlap with your task area?
│   ├── YES → get_evolution(mode: compound) scoped to that window for detail
│   └── NO  → ignore, update anchor
```

### 5. Always persist the returned anchor

Every response includes a new `session_anchor`. Store it for next session:

```json
{
  "session_anchor": {
    "last_episode_id": "ep_xyz789",
    "last_reference_time": "2026-04-13T14:22:00Z"
  }
}
```

## Auto-mode Selection

`get_changes_since` automatically picks the right mode so it never crashes:

| Candidate count | Mode selected | What you get |
|---|---|---|
| 0 | — | `no_changes` immediately |
| 1–499 | `compound` | Full symbol scoring |
| 500+ | `overview` | Module rollup only |

`candidate_count` in the response tells you what was found before selection.

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| Using `get_evolution` with a guessed timestamp | `get_changes_since` uses an exact episode boundary — no guessing, no over-fetching |
| Discarding the returned `session_anchor` | Without it, next session reverts to timestamp guessing |
| Treating `changes_detected_overview` as too large to act on | `by_module` is complete — it tells you exactly which areas changed even in large windows |
| Calling this tool repeatedly within one session | Call once at session start; use the returned evolution result for the rest of the session |
