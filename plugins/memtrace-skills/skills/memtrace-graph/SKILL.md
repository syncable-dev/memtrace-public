---
name: memtrace-graph
description: "Use when the user asks about architectural bottlenecks, important symbols, PageRank, centrality, bridge functions, code communities, logical modules, service boundaries, chokepoints, or wants to understand the high-level architecture of a codebase"
allowed-tools:
  - mcp__memtrace__find_bridge_symbols
  - mcp__memtrace__find_central_symbols
  - mcp__memtrace__list_communities
  - mcp__memtrace__list_processes
  - mcp__memtrace__get_process_flow
  - mcp__memtrace__execute_cypher
user-invocable: true
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

## Steps

### 1. Understand the architecture

Start with `list_communities` to see how the codebase is naturally partitioned into logical modules. Each community has a name, member count, and representative symbols.

### 2. Find critical infrastructure

Use `find_central_symbols` to identify the most important symbols:
- `method: "pagerank"` — importance by link structure (like Google's PageRank)
- `method: "degree"` — importance by direct connection count
- `limit` — how many to return

### 3. Find architectural chokepoints

Use `find_bridge_symbols` to find symbols that, if removed, would disconnect parts of the graph. These are:
- **Single points of failure** — if they break, cascading failures occur
- **Integration points** — good places for interfaces/contracts
- **Refactoring targets** — often too much responsibility concentrated in one place

### 4. Trace execution flows

Use `list_processes` to see all entry points (HTTP handlers, background jobs, CLI commands, event handlers).

Use `get_process_flow` with a process ID to trace a specific flow step-by-step — shows the full call chain from entry point through business logic to data access.

### 5. Custom queries

Use `execute_cypher` for advanced graph queries not covered by built-in tools. This is read-only and runs directly against the knowledge graph.

## Decision Points

| Question | Tool |
|----------|------|
| "What are the main modules?" | `list_communities` |
| "What are the most important functions?" | `find_central_symbols` with method=pagerank |
| "Where are the bottlenecks?" | `find_bridge_symbols` |
| "How does a request flow through the system?" | `list_processes` → `get_process_flow` |
| "What's the entry point for feature X?" | `list_processes`, then filter by name |
