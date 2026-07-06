---
name: memtrace-graph
description: "Map source-code architecture with graph algorithms — PageRank centrality, bridge symbols, Louvain communities, dependency paths, and execution flows over the AST graph. Use when the user asks about source-code architecture, important symbols, centrality, PageRank, bridge functions, communities, logical modules, chokepoints, service boundaries, or dependency path questions. Do not use Glob, find, tree, or directory browsing to infer architecture; Memtrace runs graph algorithms over the AST graph."
---

## Overview

Graph algorithms over the knowledge graph — communities (Louvain), PageRank centrality, bridge symbols (articulation points), shortest paths, and execution flows.

## Quick Reference

| Tool | Required params | Key optional |
|------|-----------------|--------------|
| `find_central_symbols` | `repo_id` | `limit` (def 25), `kinds[]` |
| `find_bridge_symbols` | `repo_id` | `limit` |
| `find_dependency_path` | `repo_id`, `source`, `target` | `max_depth` (def 20), `edge_type` |
| `list_communities` | `repo_id` | `min_size`, `limit` |
| `list_processes` | `repo_id` | `limit` |
| `get_process_flow` | `repo_id`, `process` | `branch` |

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

## Steps

### 1. Understand the architecture

`list_communities` — natural module partitions.

### 2. Find critical infrastructure

`find_central_symbols` runs **PageRank** internally. There is **no `method` param** (no degree-centrality switch).

```json
{ "repo_id": "memdb", "limit": 25, "kinds": ["Function", "Method"] }
```

### 3. Find chokepoints

```json
{ "repo_id": "memdb", "limit": 15 }
```

### 4. Path between two symbols

```json
{
  "repo_id": "memdb",
  "source": "handleLogin",
  "target": "DatabaseClient.query",
  "max_depth": 20
}
```

Params are **`source`** and **`target`** (symbol names) — not `from`/`to`.

### 5. Execution flows

```json
{ "repo_id": "memdb" }
{ "repo_id": "memdb", "process": "POST /login" }
```

## Output

`find_central_symbols` — each result carries the PageRank `score` plus degree counts:

```json
[
  { "name": "DatabaseClient.query", "score": 0.0142, "in_degree": 38, "out_degree": 5 },
  { "name": "handleLogin", "score": 0.0097, "in_degree": 21, "out_degree": 9 }
]
```

`find_dependency_path` returns the ordered symbol chain from `source` to `target`; `list_communities` returns module partitions with their member symbols.

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| `find_central_symbols(method: "degree")` | **No `method` param** — always PageRank |
| `find_dependency_path(from=..., to=...)` | Use **`source`** and **`target`** |
