---
name: memtrace-graph
description: "Use when the user asks about architectural bottlenecks, important symbols, PageRank, centrality, bridge functions, code communities, logical modules, service boundaries, chokepoints, dependency paths between symbols, or wants to understand the high-level architecture of a codebase"
---

## Overview

Graph algorithms that reveal the structural architecture of a codebase — community detection (Louvain), centrality ranking (PageRank), bridge symbol identification (Tarjan articulation points), shortest-path discovery, and execution flow tracing.

All four algorithm tools (`find_central_symbols`, `find_bridge_symbols`, `find_dependency_path`, `list_communities`) run natively against the MemDB-backed knowledge graph.

## Quick Reference

| Tool | Purpose |
|------|---------|
| `find_bridge_symbols` | Architectural chokepoints — symbols whose removal disconnects the graph (Tarjan articulation points) |
| `find_central_symbols` | Most important symbols by **PageRank** (default) or degree centrality |
| `find_dependency_path` | Shortest call/import path between two symbols (BFS over typed edges) |
| `list_communities` | Louvain-detected logical modules/services |
| `list_processes` | Execution flows: HTTP handlers, background jobs, CLI commands, event handlers |
| `get_process_flow` | Trace a single process step-by-step (ordered by indexed `step` property) |

## Parameter Types — Read This First

All memtrace MCP tools are **strictly typed**. Numbers must be JSON numbers, not strings.

| Parameter shape | Correct | Wrong (will fail deserialization) |
|-----------------|---------|-----------------------------------|
| Integer/count (`limit`, `min_size`, `depth`) | `limit: 20` | `limit: "20"` |
| String identifier (`repo_id`, `branch`, `name`) | `repo_id: "my-repo"` | `repo_id: my-repo` |
| Boolean (`fuzzy`, `include_tests`) | `fuzzy: true` | `fuzzy: "true"` |

If you see `MCP error -32602: invalid type: string "N", expected usize`, you passed a string where a number was required. Remove the quotes.

## Steps

### 1. Understand the architecture

Start with `list_communities` to see how the codebase is naturally partitioned into logical modules. Each community has a name, member count, and representative symbols.

**`list_communities` parameters:**
- `repo_id` — string, required. Repository ID (from `list_indexed_repositories`).
- `branch` — string, optional. Defaults to `"main"`.
- `min_size` — **integer**, optional. Minimum community size to include. Default `3`.
- `limit` — **integer**, optional. Max communities to return. Default `50`, capped at `200`.

Example (correct):
```json
{ "repo_id": "Memtrace", "limit": 20 }
```
Example (WRONG — will fail):
```json
{ "repo_id": "Memtrace", "limit": "20" }
```

### 2. Find critical infrastructure

Use `find_central_symbols` to identify the most important symbols:

**`find_central_symbols` parameters:**
- `repo_id` — string, required.
- `branch` — string, optional. Defaults to `"main"`.
- `method` — string, optional. `"pagerank"` (default) or `"degree"`.
- `limit` — **integer**, optional. How many to return. Default `20`, capped at `100`.

Returns the top-N symbols ranked by **PageRank** with the standard 0.85 damping factor over the repo's CALLS / REFERENCES edges. The output is sorted by score descending; each entry carries `name`, `kind`, `file_path`, `score`, `in_degree`, and `out_degree`. Filtered to Function / Method / Class / Interface / Struct in the requested repo + branch.

### 3. Find architectural chokepoints

Use `find_bridge_symbols` to find symbols that, if removed, would disconnect parts of the graph (Tarjan articulation points). These are:
- **Single points of failure** — if they break, cascading failures occur
- **Integration points** — good places for interfaces/contracts
- **Refactoring targets** — often too much responsibility concentrated in one place

**`find_bridge_symbols` parameters:**
- `repo_id` — string, required.
- `branch` — string, optional. Defaults to `"main"`.
- `limit` — **integer**, optional. Default `15`, capped at `50`.

Implementation: Tarjan articulation-point pass over the projected directed graph, sorted by the number of disconnected components each cut would produce.

### 4. Discover paths between symbols

Use `find_dependency_path` to answer "how does symbol A reach symbol B?" — returns the shortest call/import chain via BFS over typed edges.

**`find_dependency_path` parameters:**
- `repo_id` — string, required.
- `from` — string, required. Source symbol name.
- `to` — string, required. Destination symbol name.
- `max_depth` — **integer**, optional. Default `8`.

### 5. Trace execution flows

Use `list_processes` to see all entry points (HTTP handlers, background jobs, CLI commands, event handlers).

**`list_processes` parameters:**
- `repo_id` — string, required.
- `branch` — string, optional. Defaults to `"main"`.
- `limit` — **integer**, optional. Default `50`.

Use `get_process_flow` with a process name to trace a specific flow step-by-step — shows the full call chain from entry point through business logic to data access, ordered by the indexed `step` property on each STEP_IN_PROCESS edge.

**`get_process_flow` parameters:**
- `process` — string, required. Process name or entry-point symbol name (from `list_processes`).
- `repo_id` — string, required.
- `branch` — string, optional. Defaults to `"main"`.

## Decision Points

| Question | Tool |
|----------|------|
| "What are the main modules?" | `list_communities` |
| "What are the most important functions?" | `find_central_symbols` (PageRank, native) |
| "Where are the bottlenecks?" | `find_bridge_symbols` |
| "How does symbol A reach symbol B?" | `find_dependency_path` |
| "How does a request flow through the system?" | `list_processes` → `get_process_flow` |
| "What's the entry point for feature X?" | `list_processes`, then filter by name |
