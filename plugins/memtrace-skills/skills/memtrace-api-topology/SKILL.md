---
name: memtrace-api-topology
description: "Use when the user asks about API endpoints, HTTP routes, service-to-service calls, microservice dependencies, API topology, which services call which, cross-repo dependencies, or wants to understand the API surface of a codebase"
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

## Steps

### 1. Discover endpoints

Use `find_api_endpoints`:
- `repo_id` — required
- Returns: method, path, handler function, framework detected

### 2. Discover outbound calls

Use `find_api_calls`:
- `repo_id` — required
- Returns: target URL/path, HTTP method, calling function, library used (fetch, axios, reqwest, etc.)

### 3. Map service topology

Use `get_api_topology` to see the cross-repo HTTP call graph:
- Which services call which endpoints
- Confidence scores for each detected link
- Service-to-service dependency direction

**Prerequisite:** Multiple repos must be indexed. If cross-repo links aren't appearing, use `link_repositories` to explicitly connect them.

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
