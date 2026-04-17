---
name: memtrace-index
description: "Index a local codebase into Memtrace's knowledge graph — parses files, resolves edges, detects APIs, runs community / process detection, writes symbols + embeddings. USE when `list_indexed_repositories` shows the project isn't indexed yet, when the user says 'index this project' / 'set up memtrace here', or when `memtrace start` hasn't been run. DO NOT USE if the repo already appears in `list_indexed_repositories` (→ go straight to memtrace-search / memtrace-graph). DO NOT USE to 'reindex after edits' — the file watcher handles that automatically; call `watch_directory` instead."
---

## Overview

Index a local codebase into the persistent code knowledge graph. This is always the first step — it parses every source file, resolves cross-file relationships, detects API endpoints/calls, runs community detection and process tracing, and embeds all symbols for semantic search.

## CRITICAL: parameter types are strict

Full schema for every Memtrace tool: **`../../references/mcp-parameters.md`**. Pitfalls specific to this skill:

* `path` is an ABSOLUTE path. Relative paths like `"."` or `"./src"` fail or index the wrong root.
* `incremental`, `clear_existing`, `skip_embed` are JSON booleans (`true` / `false`), not strings.
* `job_id` from the response is a UUID string — pass it back to `check_job_status` exactly as received.

## `index_directory` — parameters

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `path` | string | yes | — | ABSOLUTE path to repo root |
| `repo_id` | string | no | directory name | Override the default auto-derived ID |
| `branch` | string | no | `"main"` | |
| `incremental` | boolean | no | `false` | Re-index only changed files |
| `clear_existing` | boolean | no | `false` | Wipe graph before indexing |
| `skip_embed` | boolean | no | `false` | Skip embedding stage (faster; loses semantic search) |

## `check_job_status` — parameters

| Field | Type | Required | Default |
|---|---|---|---|
| `job_id` | string (UUID) | yes | — |

## `list_indexed_repositories` — parameters

No parameters.

## Workflow

1. **Is it already indexed?** — call `list_indexed_repositories`. If the target repo is in the list with a recent `last_indexed`, skip to step 4.
2. **Start indexing** — call `index_directory` with an absolute `path`. Response returns immediately with a `job_id`.
3. **Poll** — call `check_job_status` every 2–3 seconds with `job_id`. Stages (in order): **scan → parse → resolve → communities → processes → persist → embeddings → api_detect → done**. Stop on `status: "completed"` or `"failed"`.

### 4. Report to user

After indexing completes, call `list_indexed_repositories` to confirm the repo appears with correct node/edge counts. Report: repo_id, languages detected, total symbols, total relationships.

**Save the `repo_id`** — most other memtrace tools require it.

## Error Handling

| Error | Action |
|-------|--------|
| Path does not exist | Ask user to verify the absolute path |
| Job status "failed" | Report the error message; suggest `clear_existing: true` for a fresh rebuild |
| Timeout (job running > 5 min) | Large repos are normal; keep polling. For monorepos, index subdirectories separately |
| Already indexed | Use `incremental: true` to update, or skip indexing entirely |
