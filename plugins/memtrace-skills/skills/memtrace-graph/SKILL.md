---
name: memtrace-graph
description: "Use when the user asks about architectural bottlenecks, important symbols, PageRank, centrality, bridge functions, code communities, logical modules, service boundaries, chokepoints, or wants to understand the high-level architecture of a codebase"
---

## Overview

Graph algorithms that reveal the structural architecture of a codebase — community detection (Louvain), centrality ranking (PageRank/degree), bridge symbol identification (betweenness), and execution flow tracing.

## Quick Reference

| Tool | Purpose |
|------|---------|
| `find_bridge_symbols` | Architectural chokepoints — symbols that connect otherwise-separate modules |
| `find_central_symbols` | Most important symbols by PageRank or degree centrality |
| `list_communities` | Louvain-detected logical modules/services |
| `list_processes` | Execution flows: HTTP handlers, background jobs, CLI commands, event handlers |
| `get_process_flow` | Trace a single process step-by-step |
| `execute_cypher` | Direct read-only Cypher queries for custom analysis |

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
- `limit` — **integer**, optional. How many to return. Default `20`, capped at `100`.
- `algorithm` — string, optional. `"pagerank"` (default, via MAGE — falls back to degree if unavailable) or `"degree"` (simple in-degree count, no MAGE required).

### 3. Find architectural chokepoints

Use `find_bridge_symbols` to find symbols that, if removed, would disconnect parts of the graph. These are:
- **Single points of failure** — if they break, cascading failures occur
- **Integration points** — good places for interfaces/contracts
- **Refactoring targets** — often too much responsibility concentrated in one place

**`find_bridge_symbols` parameters:**
- `repo_id` — string, required.
- `branch` — string, optional. Defaults to `"main"`.
- `limit` — **integer**, optional. Default `15`, capped at `50`.

### 4. Trace execution flows

Use `list_processes` to see all entry points (HTTP handlers, background jobs, CLI commands, event handlers).

**`list_processes` parameters:**
- `repo_id` — string, required.
- `branch` — string, optional. Defaults to `"main"`.
- `limit` — **integer**, optional. Default `50`.

Use `get_process_flow` with a process name to trace a specific flow step-by-step — shows the full call chain from entry point through business logic to data access.

**`get_process_flow` parameters:**
- `process` — string, required. Process name or entry-point symbol name (from `list_processes`).
- `repo_id` — string, required.
- `branch` — string, optional. Defaults to `"main"`.

### 5. Custom queries

Use `execute_cypher` for advanced graph queries not covered by built-in tools. This is read-only and runs directly against the knowledge graph.

**`execute_cypher` parameters:**
- `query` — string, required. A read-only Cypher query. Write keywords (CREATE, MERGE, DELETE, SET, etc.) are forbidden. Use `$repo_id` to scope to a repository.
- `params` — object, optional. JSON object of parameter bindings.
- `repo_id` — string, optional. If provided, injected as `$repo_id` into `params`.

## Decision Points

| Question | Tool |
|----------|------|
| "What are the main modules?" | `list_communities` |
| "What are the most important functions?" | `find_central_symbols` with method=pagerank |
| "Where are the bottlenecks?" | `find_bridge_symbols` |
| "How does a request flow through the system?" | `list_processes` → `get_process_flow` |
| "What's the entry point for feature X?" | `list_processes`, then filter by name |
