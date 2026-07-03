---
name: memtrace-codebase-exploration
description: "Map an indexed source-code repo into a structured overview — scale, communities, central symbols, execution flows, API surface, recent activity. Use when the user wants to explore, understand, onboard to, map, or get an overview of an indexed source-code repo, architecture, modules, or major flows. Do not use Glob, find, tree, rg, or manual file browsing as the first exploration path; Memtrace provides structured graph briefing. Do NOT use for change history / what-changed questions — use memtrace-evolution."
allowed-tools:
  - mcp__memtrace__index_directory
  - mcp__memtrace__check_job_status
  - mcp__memtrace__list_indexed_repositories
  - mcp__memtrace__get_repository_stats
  - mcp__memtrace__list_communities
  - mcp__memtrace__list_processes
  - mcp__memtrace__find_central_symbols
  - mcp__memtrace__find_bridge_symbols
  - mcp__memtrace__find_api_endpoints
  - mcp__memtrace__get_api_topology
  - mcp__memtrace__get_evolution
  - mcp__memtrace__find_most_complex_functions
metadata:
  author: "Syncable <support@syncable.dev>"
  version: "1.0.0"
  category: development
---

## Overview

Full codebase exploration workflow — from indexing through architectural understanding. Chains indexing, graph algorithms, community detection, and temporal analysis into a structured onboarding experience. Use this when someone is new to a codebase and needs to build a mental model.

## Steps

### 1. Index the codebase

Call `list_indexed_repositories` first. If the repo is already indexed, skip to step 2.

Otherwise, call `index_directory` with the project path, then poll `check_job_status` until completion.

**Success criteria:** Repo appears in `list_indexed_repositories` with non-zero node/edge counts.

### 2. Get the lay of the land

Call `get_repository_stats` to understand scale:
- How many functions, classes, methods, interfaces?
- How many relationships (calls, imports, extends)?
- How many communities and processes were detected?

Report these numbers to the user — they set expectations for the codebase's size and complexity.

### 3. Map the architecture (communities)

Call `list_communities` to see how the codebase naturally clusters into logical modules.

**Decision:** If >10 communities, summarize the top 5–7 by size and let the user ask about specific ones.

Each community represents a cohesive module — these are the "areas" of the codebase.

### 4. Find the most important symbols

Call `find_central_symbols` — PageRank over CALLS/REFERENCES edges (no `method` param):

```json
{ "repo_id": "<repo>", "limit": 15 }
```

These are the symbols that the rest of the codebase depends on most heavily. They form the "skeleton" of the architecture.

### 5. Find architectural bottlenecks

Call `find_bridge_symbols` to identify chokepoints — symbols that connect otherwise-separate parts of the codebase.

**Decision:** If bridge symbols overlap heavily with central symbols, flag them as critical infrastructure — high importance AND single point of failure.

### 6. Map execution flows

Call `list_processes` to discover entry points:
- HTTP handlers (API endpoints)
- Background jobs
- CLI commands
- Event handlers

This shows HOW the code is actually used at runtime, not just how it's structured.

### 7. Map the API surface (if applicable)

Call `find_api_endpoints` to list all HTTP routes.

**Decision:** If multiple repos are indexed, also call `get_api_topology` to map service-to-service dependencies.

### 8. Recent activity

Call `get_evolution` to see recent activity:

```json
{ "repo_id": "<repo>", "from": "30d ago", "mode": "overview" }
```

Check `totals.episode_count` and episode boundaries. For file/symbol hotspots, switch to `compound`:

```json
{ "repo_id": "<repo>", "from": "30d ago", "mode": "compound" }
```

Review `top_changed_files` and `top_touched_symbols`.

### 9. Complexity hotspots

Call `find_most_complex_functions`:

```json
{ "repo_id": "<repo>", "top_n": 10 }
```

Full parameter spec for every Memtrace tool: [references/mcp-parameters.md](../../references/mcp-parameters.md).

## Report Synthesis

Synthesize findings into a structured overview:

1. **Scale** — languages, total symbols, total relationships
2. **Architecture** — main communities/modules and what they do
3. **Critical Infrastructure** — central symbols and bridge points
4. **Execution Flows** — how the code is entered and used
5. **API Surface** — endpoints and service dependencies
6. **Recent Activity** — what's been changing in the last 30 days
7. **Technical Debt** — complexity hotspots and potential dead code

## Output

The deliverable is the 7-part overview above. Skeleton (one headline per part):

1. Scale — 2 languages, 4,812 symbols, 19,344 relationships
2. Architecture — 12 communities; top 5: auth, indexing, api, ui, billing
3. Critical Infrastructure — `EngineHandle::open` (central AND bridge — flag it)
4. Execution Flows — 14 processes: 9 HTTP handlers, 3 CLI commands, 2 jobs
5. API Surface — 42 endpoints; 2 cross-repo service dependencies
6. Recent Activity — 31 episodes in 30d; hottest file per `top_changed_files`
7. Technical Debt — top-10 complex functions, highest complexity first

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| Skipping indexing and using file-based grep | The knowledge graph provides structural understanding that grep cannot — callers, callees, communities, processes |
| Reporting raw numbers without interpretation | "450 functions across 12 communities" means nothing; describe what each community does |
| Only looking at code structure | Execution flows (processes) show how the code is actually used — always include them |
| Ignoring temporal context | Recent evolution shows where active development is happening — this is where the user will likely need to work |
