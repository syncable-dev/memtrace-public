---
description: Map an indexed repo via the Memtrace graph (communities, symbols, processes)
argument-hint: "[repo_id]"
allowed-tools: ["ToolSearch", "mcp__memtrace__list_indexed_repositories", "mcp__memtrace__list_communities", "mcp__memtrace__find_central_symbols", "mcp__memtrace__list_processes", "mcp__memtrace__get_service_diagram"]
---

# mt-onboard: structured repo overview from the graph

Build a structured architecture overview of an indexed repo from the Memtrace knowledge
graph. Do NOT reach for `Glob` / `find` / `tree` / `rg` to infer architecture: the graph
already carries communities, centrality, and execution flows in one pass.

## Parameters

`$ARGUMENTS` is an optional `repo_id`. When omitted, the command resolves the repo whose
indexed path matches the current working directory.

## Behavior

0. Preload the deferred tool schemas (the `mcp__memtrace__*` tools fail with
   `InputValidationError` until their schema loads):
   ```
   ToolSearch(query="select:mcp__memtrace__list_indexed_repositories,mcp__memtrace__list_communities,mcp__memtrace__find_central_symbols,mcp__memtrace__list_processes,mcp__memtrace__get_service_diagram")
   ```
1. Call `list_indexed_repositories`.
   - If `$ARGUMENTS` is non-empty, use it verbatim as `repo_id`.
   - Otherwise pick the entry whose `path` matches the current working directory. If the
     match has `path: null` or `last_indexed_at: null`, the index is a stale stub: report
     that and stop, since every downstream answer would come from stale data.
   - If nothing matches the cwd and `$ARGUMENTS` is empty, list the candidate `repo_id`
     values and ask which one, then stop.
2. Run, in order, each scoped to the resolved `repo_id`:
   - `list_communities` - the main modules or subsystems.
   - `find_central_symbols` - the highest-centrality symbols (read these first).
   - `list_processes` - the main execution flows.
   - `get_service_diagram` - how services or repos connect (cross-repo HTTP edges).
3. Synthesize a compact overview: scale (node/edge count from step 1's repo entry), the
   top 3 to 5 communities, the 5 most central symbols, the main processes, and any
   cross-service edges. Name the entry points a new contributor should read first. Do not
   dump raw tool JSON.

## Example

`/mt-onboard` run inside an indexed repo's working directory returns a one-page
architecture map with no further input. `/mt-onboard backend-api` targets a specific
indexed `repo_id` instead of resolving from cwd.

## Restrictions

Read-only discovery command: issues no writes and never resets the index.
