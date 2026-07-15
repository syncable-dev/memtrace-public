---
description: Compute the blast radius of a planned change to a symbol via the graph
argument-hint: "<symbol> [repo_id]"
allowed-tools: ["ToolSearch", "mcp__memtrace__list_indexed_repositories", "mcp__memtrace__find_symbol", "mcp__memtrace__get_impact", "mcp__memtrace__get_symbol_context"]
---

# mt-impact: blast radius for a planned change

Compute what a change to one symbol will touch, from the AST graph, in three calls. Do
NOT hand-grep references: a raw search returns line hits with no caller graph or
cross-repo edge, so it undercounts the true blast radius.

## Parameters

`$ARGUMENTS` is `<symbol> [repo_id]`. The first token is the target symbol name
(required); the optional second token is a `repo_id`.

## Behavior

0. Preload the deferred tool schemas:
   ```
   ToolSearch(query="select:mcp__memtrace__find_symbol,mcp__memtrace__get_impact,mcp__memtrace__get_symbol_context,mcp__memtrace__list_indexed_repositories")
   ```
1. Parse inputs. If the first token is missing, ask for the symbol and stop. Resolve
   `repo_id` from the second token if present, else via `list_indexed_repositories`
   matched against the current working directory. Flag a `path: null` stub: impact from
   a stale index undercounts.
2. Run, in order:
   - `find_symbol(name=<target>, repo_id=<repo_id>)` - pin the exact symbol id. If
     ambiguous, list matches and ask which one; do not guess.
   - `get_impact` - the transitive impact set: direct plus indirect callers (who breaks
     if the contract changes) and downstream dependents. Note cross-repo HTTP edges if
     present.
   - `get_symbol_context` - the symbol's role: its community, the processes it
     participates in, and its immediate neighbors. This turns a raw count into a risk
     story.
3. Report: the direct caller count, the total transitive impact size, the highest-risk
   callers by name, any cross-repo edge, and the community or processes the symbol sits
   in. Close with a one-line risk read (LOW / MEDIUM / HIGH) and, if HIGH, the callers to
   update in the same change.

## Example

`/mt-impact parseConfig` computes blast radius for `parseConfig` in the cwd-resolved
repo. For a recorded-rationale check on top of the raw radius, chain into
`/mt-preflight` or `/mt-recall`.

## Restrictions

Read-only impact query: performs no edits and never touches the live index.
