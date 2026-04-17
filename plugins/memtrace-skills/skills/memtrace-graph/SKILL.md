---
name: memtrace-graph
description: "Graph-wide architecture analysis of an indexed codebase — PageRank/degree centrality, Louvain communities, betweenness bridges, execution-flow processes, and custom read-only Cypher. USE for repo-level questions like 'what are the key modules', 'what are the bottlenecks', 'which symbols are load-bearing', 'how does a request flow'. DO NOT USE for a single symbol's neighbourhood (→ memtrace-relationships), for blast-radius scoring of one change (→ memtrace-impact), for temporal 'what changed' questions (→ memtrace-evolution), or for symbol discovery when you don't have an ID (→ memtrace-search)."
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

## CRITICAL: parameter types are strict

Full schema for every Memtrace tool: **`../../references/mcp-parameters.md`**. Quick-reference for graph tools:

| Shape | Correct | Wrong |
|---|---|---|
| Integer (`limit`, `min_size`, `depth`) | `limit: 20` | `limit: "20"` |
| String (`repo_id`, `branch`, `algorithm`) | `"my-repo"` | `my-repo` (unquoted) |
| Boolean | `true` / `false` | `"true"` / `"false"` |
| Enum (`algorithm`) | `"pagerank"` or `"degree"` | `"PageRank"` (case matters) |

`execute_cypher` rejects any query containing write keywords (CREATE, MERGE, DELETE, SET, DROP, REMOVE). Use `$repo_id` as a bind param — do not string-concat it into the query.

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
