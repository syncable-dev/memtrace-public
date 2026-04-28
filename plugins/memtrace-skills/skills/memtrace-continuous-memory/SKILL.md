---
name: memtrace-continuous-memory
description: "Use when the user asks to keep memtrace fresh while editing, watch a directory for live updates, enable incremental indexing, set up always-on memory, or wants their just-saved code to be queryable immediately"
---

## Overview

Memtrace keeps the knowledge graph live as you edit. Once you call `watch_directory`, every save runs through the **incremental indexing fast-path** — a notify-based file watcher debounces saves, the indexer re-parses only the touched files, and the engine commits the delta in a single WAL transaction. Steady-state latency is **~80 ms from save to queryable** on a typical project.

This is what makes "session continuity" actually work: by the time you ask `find_symbol` after a save, the new symbol is already in the graph.

## Steps

### 1. Confirm the repo is indexed

```
mcp__memtrace__list_indexed_repositories
```

If the repo isn't there, run `index_directory` first. The watcher requires an existing repo_id — it never bootstraps from scratch.

### 2. Start watching

```
mcp__memtrace__watch_directory(
  path: "/abs/path/to/repo"
)
```

The tool registers a `notify` watcher on the directory tree, debounces save bursts (so a `:wq` that touches a swap file doesn't trigger twice), and routes deltas through the indexer's incremental fast-path. Returns immediately — watching runs in the background.

### 3. Confirm it's live

```
mcp__memtrace__list_watched_paths
```

Each entry shows the watched root, the bound repo_id, and the last delta's `persist_ms`.

### 4. Edit normally — Memtrace catches up

After every save the watcher emits a `labels_updated` WebSocket event:

```json
{
  "event":           "labels_updated",
  "repo_id":         "demo",
  "nodes_changed":   12,
  "persist_ms":      78,
  "timestamp":       "2026-04-27T10:42:13Z"
}
```

Dashboards and IDE plugins subscribe to this on `/ws` and refresh themselves. As an agent you don't have to listen — your next `find_symbol` / `find_code` / `get_symbol_context` call will see the new state automatically.

### 5. Stop watching

```
mcp__memtrace__unwatch_directory(path: "/abs/path/to/repo")
```

Idempotent — unwatching an already-unwatched path is a no-op.

## When to Use

- **Long sessions on the same repo** — keeps `get_symbol_context` accurate without rerunning `index_directory`
- **Pair programming with an IDE plugin** — the dashboard's WebSocket subscription auto-refreshes panels
- **Demo / live coding** — every save reflects in the graph within 80–150 ms
- **Long-running agents** — instead of polling `index_directory`, the watcher pushes deltas

## When NOT to Use

- **One-shot batch edits** — running `index_directory --incremental` at the end is cheaper than spinning up a watcher
- **Generated / build output trees** — exclude paths under `target/`, `dist/`, `node_modules/` (the watcher honours common ignore patterns but a noisy build can still saturate the debounce queue)
- **CI / containerised runs** — file events are unreliable across container boundaries; index explicitly instead

## Latency Expectations

| Operation | Typical wall time |
|---|---|
| File save → watcher fires | < 5 ms |
| Debounce window | 50 ms |
| Incremental parse + delta persist | ~80 ms |
| `labels_updated` broadcast | < 1 ms after persist |
| Total: save → queryable | ~80–150 ms |

If you see `persist_ms` consistently above 500 ms, the saved files are larger than expected (e.g., generated bundles) — narrow the watch root or add ignore patterns.

## Common Mistakes

| Mistake | Reality |
|---|---|
| Calling `watch_directory` on an unindexed repo | Returns an error — run `index_directory` first |
| Watching `node_modules/` or `target/` | Saturates the watcher with build noise — point at the source root only |
| Polling `find_symbol` every second to "wait" for indexing | Subscribe to the `labels_updated` WS event, or just call once after the save — the delta is already there |
| Forgetting to `unwatch_directory` between sessions | Watchers are per-process; restarting `memtrace start` wipes them, but for hosted instances unwatching cleanly avoids leaks |
