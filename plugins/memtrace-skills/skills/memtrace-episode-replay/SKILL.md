---
name: memtrace-episode-replay
description: "Replay the graph diff of one episode — a single git commit or working-tree save — to inspect what it changed: added/modified/removed symbols and edges. Use when the user asks what one commit or save changed in the graph, why code looks this way, or wants to inspect implementation attempts, reversions, past reasoning, or abandoned approaches across commits and working-tree episodes. Do not use git log or Grep for graph-level episode diffs; Memtrace replays indexed episode records. Do NOT use for module-level summaries or date-range change history — use memtrace-evolution."
---

## Overview

Replay the graph diff for **one episode** (a single git commit or working-tree save). Shows which nodes/edges were added, modified, or removed in that episode — not a multi-episode time-range narrative.

Use `get_evolution(mode: "recent")` to **find** episode IDs and timestamps, then drill into a specific episode with this tool.

## Episode selector — pick one

| Approach | Parameters |
|---|---|
| Known episode UUID | `episode_id` |
| Newest episode in repo | `repo_id` + `episode_index: 0` |
| Nth newest | `repo_id` + `episode_index: N` |

**There is no `from` / `to` / `include_working_tree` on this tool.**

## Optional filters (large commits)

| Param | Purpose |
|---|---|
| `symbol` | Scope to one symbol name |
| `file_path` | Substring filter on record paths |
| `kind` | e.g. `"Function"`, `"CALLS"` |
| `mode` | `"graph_summary"` for digest on huge commits |
| `compress` | default `true` — collapse identical-hash modifications |
| `limit` / `cursor` | Pagination per bucket (default limit 200) |

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

## Steps

### 1. Find the episode to inspect

From `get_evolution`:

```json
{ "repo_id": "memdb", "from": "7d ago", "mode": "recent", "limit": 20 }
```

Each entry has `episode` metadata including `id` (use as `episode_id`) and `reference_time`.

Or use `episode_index: 0` for the newest episode without knowing the UUID.

### 2. Call `get_episode_replay`

By episode UUID:

```json
{
  "episode_id": "550e8400-e29b-41d4-a716-446655440000",
  "symbol": "execute",
  "file_path": "src/order/service.rs",
  "kind": "Function",
  "compress": true,
  "limit": 200
}
```

Newest episode, summary first on large commits:

```json
{
  "repo_id": "memdb",
  "episode_index": 0,
  "mode": "graph_summary"
}
```

### 3. Interpret the response

| Field | Meaning |
|---|---|
| `found` | `false` if episode missing — check `_note` |
| `totals` | Counts: `nodes_added/modified/removed`, `edges_*` |
| `nodes_added[]` etc. | Per-record diffs (paginated) |
| `page.next_cursor` | More records remain — pass as `cursor` |

For symbol history **across many episodes**, use `get_timeline` instead:

```json
{
  "repo_id": "memdb",
  "scope_path": "OrderService.execute",
  "file_path": "src/order/service.rs"
}
```

## Output

`get_episode_replay` returns the buckets interpreted in step 3:

```json
{
  "found": true,
  "totals": { "nodes_added": 3, "nodes_modified": 12, "nodes_removed": 1, "edges_added": 7, "edges_removed": 2 },
  "nodes_added": [ /* per-record diffs, paginated per bucket */ ],
  "nodes_modified": [ /* … */ ],
  "edges_removed": [ /* … */ ],
  "page": { "next_cursor": 200 }
}
```

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| Passing `from` / `to` time window | Not supported — one episode per call |
| Expecting `narrative_hint` / `attempted_and_reverted` | Not in API — inspect added/modified/removed buckets |
| Unfiltered replay on 10k-symbol commits | Use `kind`, `file_path`, `symbol`, or `mode: "graph_summary"` first |
| Using this for "what changed last week?" or module summaries | Use memtrace-evolution — `get_evolution(from=..., mode: recent)` — to list episodes |
