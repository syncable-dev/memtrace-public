---
name: memtrace-index
description: "Index a source-code repo into the Memtrace knowledge graph and poll the job to completion. Use when the user asks to index, parse, ingest, reindex, watch, or prepare a source-code repo for Memtrace analysis, when code exploration needs an index, or when searches return 0/partial results for source paths under an indexed root. Use this before Grep, Glob, rg, find, or manual code search whenever the repo can be indexed. For ongoing watch mode / live re-indexing after the initial index, use memtrace-continuous-memory."
---

## Overview

Index a local codebase into the persistent code knowledge graph. This is always the first step — it parses every source file, resolves cross-file relationships, detects API endpoints/calls, runs community detection and process tracing, and embeds all symbols for semantic search.

## Quick Reference

| Parameter | Purpose |
|-----------|---------|
| `path` | Absolute path to the directory to index |
| `incremental` | Only re-parse changed files (use for subsequent runs) |
| `clear_existing` | Wipe and rebuild from scratch |

> **Parameter types:** MCP parameters are strictly typed. Numbers (`limit`, `depth`, `min_size`, `last_n`, etc.) must be JSON numbers — not strings. Use `limit: 20`, never `limit: "20"`. Passing a string yields `MCP error -32602: invalid type: string, expected usize`.

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).


## Steps

### 1. Check if already indexed

Use the `list_indexed_repositories` MCP tool first. If the repo is already indexed and recent, skip to step 4.

**Success criteria:** You have a list of repo_ids and their last-indexed timestamps.

If a repo is present but searches miss a source subdirectory under that repo
root (for example `ui/`, `memtrace-ui/`, `web/`, `frontend/`, or `src/`), treat
that as a stale/partial index. Do not use grep as a workaround. Run incremental
indexing on the repo root, then retry the Memtrace query.

### 2. Index the directory

Use the `index_directory` MCP tool:

- Set `path` to the project root (absolute path)
- Set `incremental: true` if re-indexing after changes
- Set `clear_existing: true` only if a full rebuild is needed

If the selected path is just a folder containing multiple independent git repos,
do not index that parent folder unless the user explicitly wants a shared
workspace. For separate repos, index each repo root separately. For intentional
sharing, ask the user to bless the parent explicitly with
`memtrace start --bless-workspace` first, then verify the boundary with
`memtrace workspace status <path>`.

**Success criteria:** You receive a `job_id` immediately.

### 3. Poll for completion

Use `check_job_status` with the `job_id` every 2–3 seconds. **The loop must never run unbounded** — hard cap at ~100 polls (~5 minutes). On hitting the cap: stop polling, report the `job_id` and last observed status to the user, and tell them the job keeps running server-side — they (or you, later) can resume with `check_job_status` on that `job_id` or find it via `list_jobs`.

Pipeline stages in order: **scan → parse → resolve → communities → processes → persist → embeddings → api_detect → done**

Wait until `status = "completed"`. If `status = "failed"`, report the error message to the user.

### 4. Report to user

After indexing completes, call `list_indexed_repositories` to confirm the repo appears with correct node/edge counts. Report: repo_id, languages detected, total symbols, total relationships.

**Save the `repo_id`** — most other memtrace tools require it.

## Error Handling

| Error | Action |
|-------|--------|
| Path does not exist | Ask user to verify the absolute path |
| Job status "failed" | Report the error message; suggest `clear_existing: true` for a fresh rebuild |
| Timeout (job running > 5 min) | Large repos are normal, but respect the poll cap: stop, report `job_id` + last status, resume later via `check_job_status` / `list_jobs`. For monorepos, index subdirectories separately |
| Already indexed | Use `incremental: true` to update, or skip indexing entirely |

## Output

`index_directory` returns a job handle immediately; `check_job_status` tracks it to a terminal state:

```json
{ "job_id": "6f9a2c1e-...-uuid" }
{ "status": "completed" }
```

While running, the job advances through the pipeline stages listed in step 3; `status = "failed"` carries an error message. The final user-facing report (confirmed via `list_indexed_repositories`) covers: `repo_id`, languages detected, total symbols, total relationships (node/edge counts).
