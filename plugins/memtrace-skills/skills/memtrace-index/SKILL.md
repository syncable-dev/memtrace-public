---
name: memtrace-index
description: "Use when the user asks to index a project, set up code intelligence, parse a codebase, build a knowledge graph, prepare a repo for analysis, or as the very first step before any code exploration, search, or relationship analysis"
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


## Steps

### 1. Check if already indexed

Use the `list_indexed_repositories` MCP tool first. If the repo is already indexed and recent, skip to step 4.

**Success criteria:** You have a list of repo_ids and their last-indexed timestamps.

### 2. Index the directory

Use the `index_directory` MCP tool:

- Set `path` to the project root (absolute path)
- Set `incremental: true` if re-indexing after changes
- Set `clear_existing: true` only if a full rebuild is needed

**Success criteria:** You receive a `job_id` immediately.

### 3. Poll for completion

Use `check_job_status` with the `job_id` every 2–3 seconds.

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
| Timeout (job running > 5 min) | Large repos are normal; keep polling. For monorepos, index subdirectories separately |
| Already indexed | Use `incremental: true` to update, or skip indexing entirely |
