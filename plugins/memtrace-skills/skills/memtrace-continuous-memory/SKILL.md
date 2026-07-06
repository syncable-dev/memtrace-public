---
name: memtrace-continuous-memory
description: "Keep the Memtrace index fresh while editing by watching a repo for live, incremental re-indexing. Use when the user asks to keep Memtrace fresh while editing, watch a repo, enable live or incremental indexing, set up always-on memory (meaning Memtrace index watching, not generic agent memory), or make just-saved source code queryable immediately. Do not fall back to repeated Grep or manual rescans; configure Memtrace watching."
---

## Overview

Keep the knowledge graph live while editing. `watch_directory` triggers incremental re-indexing on file saves (~80 ms typical latency after debounce).

## Required parameters — `watch_directory`

| Param | Required | Notes |
|---|---|---|
| `path` | yes | Absolute directory path |
| `repo_id` | yes | Must already be indexed |
| `branch` | no | default `"main"` |

```json
{ "path": "/abs/path/to/repo", "repo_id": "memdb" }
```

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

## Steps

### 1. Confirm indexed

```
list_indexed_repositories()
```

Run `index_directory` first if missing (poll `check_job_status`; stop after ~5 minutes and report the job id — see memtrace-index).

### 2. Start watching

See JSON above. Returns immediately; watcher runs in background.

### 3. Confirm active watches

```
list_watched_paths()
```

Response shape: see [Output](#output).

### 4. Edit normally

Next `find_symbol` / `get_symbol_context` call sees saved changes after debounce (~500 ms) + incremental persist (~80 ms).

### 5. Stop watching (kill switch)

`unwatch_directory`:

```json
{ "path": "/abs/path/to/repo" }
```

Idempotent — unwatching an already-unwatched path is a no-op.

## Latency expectations

| Stage | Typical |
|---|---|
| Debounce | **500 ms** (MCP schema) |
| Incremental persist | ~80 ms |
| Save → queryable | ~80–150 ms after debounce |

## Output

`watch_directory` returns an immediate acknowledgment; the watcher runs in the background. `list_watched_paths` returns:

```json
{
  "watches": [
    { "path": "...", "repo_id": "...", "branch": "...", "started_at": "...", "origin": "..." }
  ],
  "count": 1
}
```

There is **no `persist_ms`** field in this response.

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| `watch_directory(path=...)` without `repo_id` | **Both required** |
| Watching unindexed repo | Index first |
| Expecting `persist_ms` from `list_watched_paths` | Field not in MCP response |
| Debounce "50 ms" | Schema documents **500 ms** |
