---
name: memtrace-api-topology
description: "Map API endpoints, outbound HTTP calls, and cross-repo service topology in indexed source code. Use when the user asks about API endpoints, HTTP routes, fetch/client calls, REST surface, service dependencies, cross-repo dependencies, or API topology. Do not use Grep, Glob, rg, find, or manual file search for routes or HTTP calls; Memtrace maps endpoints and call edges from the indexed AST graph."
---

## Overview

Map HTTP API surface — endpoints, outbound calls, cross-repo topology.

## Quick Reference

| Tool | Required | Key optional |
|------|----------|--------------|
| `find_api_endpoints` | `repo_id` | `method`, `path_contains`, `limit`, `branch` |
| `find_api_calls` | `repo_id` | `method`, `path_contains`, `limit`, `branch` |
| `get_api_topology` | — | `repo_id`, `min_confidence`, `include_external` |

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

## Steps

### 1–3. Endpoints, calls, topology

```json
{ "repo_id": "memdb" }
{ "repo_id": "memdb", "method": "GET", "path_contains": "/users" }
{ "min_confidence": 0.7 }
```

### 4. Deep-dive an endpoint handler

Use handler **symbol name** — not symbol ID:

```json
{ "repo_id": "memdb", "symbol": "handleGetUsers" }
```

## Output

| Result | Carries |
|--------|---------|
| Endpoint entry (`find_api_endpoints`) | HTTP method, route path, handler symbol name (feed to `get_symbol_context` as `symbol`) |
| Call entry (`find_api_calls`) | HTTP method, target path, calling symbol |
| Topology edge (`get_api_topology`) | caller repo → handler repo, confidence 0.0–1.0 (edges below `min_confidence` dropped) |

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| `get_symbol_context(symbol_id=...)` | Use **`symbol`** (name) + `repo_id` |
| Cross-repo links with one repo indexed | Index all services; use `link_repositories` if needed |
