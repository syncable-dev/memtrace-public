---
name: memtrace-api-topology
description: "Map HTTP API topology across indexed repositories — endpoints each service exposes, outbound HTTP calls each service makes, and cross-service edges. USE for questions about microservice dependencies, service-to-service HTTP calls, 'which service calls which', orphaned endpoints, or the API surface of a repo. DO NOT USE for in-process function call graphs (→ memtrace-relationships), for database or message-queue dependencies (not tracked), for single-symbol blast-radius (→ memtrace-impact), or when no repos are linked yet (call `link_repositories` first)."
---

## Overview

Map the HTTP API surface of a codebase — exposed endpoints, outbound HTTP calls, and cross-repo service-to-service dependency graphs. Supports auto-detection for Express, Encore, NestJS, Axum, FastAPI, Flask, Gin, Spring Boot, and more.

## Quick Reference

| Tool | Purpose |
|------|---------|
| `find_api_endpoints` | All exposed HTTP endpoints (GET /users, POST /orders, etc.) |
| `find_api_calls` | All outbound HTTP calls (fetch, axios, reqwest, etc.) |
| `get_api_topology` | Cross-repo call graph: which service calls which endpoint |
| `link_repositories` | Manually link repos for cross-repo edge detection |

## CRITICAL: parameter types are strict

Full schema for every Memtrace tool: **`../../references/mcp-parameters.md`**. Quick pitfalls specific to this skill:

* `limit`, `min_confidence` are JSON numbers. `min_confidence` is a float 0.0–1.0 — not a string, not a percentage.
* `include_external` is a JSON boolean. `"true"` / `"false"` strings fail.
* `method` is an uppercase HTTP verb as a plain string — `"GET"`, not `GET` unquoted.

## `find_api_endpoints` — parameters

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | The service that handles these endpoints |
| `method` | string | no | — | HTTP verb filter, e.g. `"POST"` |
| `path_contains` | string | no | — | Path substring, e.g. `"/users"` |
| `branch` | string | no | `"main"` | |
| `limit` | integer | no | `50` | |

## `find_api_calls` — parameters

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | The service making the calls |
| `method` | string | no | — | |
| `path_contains` | string | no | — | |
| `branch` | string | no | `"main"` | |
| `limit` | integer | no | `50` | |

## `get_api_topology` — parameters

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `min_confidence` | number (float) | no | `0.7` | 0.0–1.0 threshold for cross-repo HTTP edges |
| `include_external` | boolean | no | `false` | Include calls to unindexed external services |
| `repo_id` | string | no | — | Omit for the full cross-service graph |

## Workflow

1. **Endpoints** — `find_api_endpoints` with `repo_id` to list exposed routes. Add `method` / `path_contains` to narrow.
2. **Outbound calls** — `find_api_calls` with `repo_id` to list every HTTP client call the service makes.
3. **Topology** — `get_api_topology` (usually without `repo_id` so you get the full graph). Expect multiple repos to be indexed; cross-repo edges appear automatically when base URLs match.
4. **Deep-dive** — grab the endpoint's `symbol_id` from step 1 results and call `get_symbol_context` to see handler + processes + cross-repo callers in one shot.

### 4. Deep-dive into an endpoint

For any specific endpoint, use `get_symbol_context` with the endpoint's symbol ID to see:
- Which internal functions handle the request
- Which processes (execution flows) include this endpoint
- Which external services call this endpoint

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| Expecting cross-repo links with only one repo indexed | Index ALL related services first; cross-repo HTTP edges are linked automatically after indexing |
| Missing endpoints from custom frameworks | Memtrace auto-detects major frameworks; for custom routers, the endpoints may appear as regular functions |
| Not using `link_repositories` | If auto-linking missed a connection, use this to manually establish cross-repo edges |
