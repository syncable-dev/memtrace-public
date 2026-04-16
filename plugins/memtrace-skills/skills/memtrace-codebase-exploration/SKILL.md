---
name: memtrace-codebase-exploration
description: "Use when the user asks to explore a codebase, understand a project, onboard to a new repo, get an overview of how code is structured, map the architecture, or wants a comprehensive understanding of a codebase they're new to"
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

Call `find_central_symbols` with `method: "pagerank"` and `limit: 15`.

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

Call `get_evolution` with mode `overview` and a 30-day window to see which modules have been most active recently.

**Decision:** If the user asks about specific recent changes, switch to mode `compound` for symbol-level detail.

### 9. Complexity hotspots

Call `find_most_complex_functions` with `limit: 10` to identify potential technical debt.

## Report Synthesis

Synthesize findings into a structured overview:

1. **Scale** — languages, total symbols, total relationships
2. **Architecture** — main communities/modules and what they do
3. **Critical Infrastructure** — central symbols and bridge points
4. **Execution Flows** — how the code is entered and used
5. **API Surface** — endpoints and service dependencies
6. **Recent Activity** — what's been changing in the last 30 days
7. **Technical Debt** — complexity hotspots and potential dead code

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| Skipping indexing and using file-based grep | The knowledge graph provides structural understanding that grep cannot — callers, callees, communities, processes |
| Reporting raw numbers without interpretation | "450 functions across 12 communities" means nothing; describe what each community does |
| Only looking at code structure | Execution flows (processes) show how the code is actually used — always include them |
| Ignoring temporal context | Recent evolution shows where active development is happening — this is where the user will likely need to work |
