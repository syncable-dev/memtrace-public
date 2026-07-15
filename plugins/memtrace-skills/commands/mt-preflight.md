---
description: Pre-edit safety check on a symbol (blast radius, co-change, decisions, churn)
argument-hint: "<symbol> [repo_id]"
allowed-tools: ["ToolSearch", "mcp__memtrace__list_indexed_repositories", "mcp__memtrace__find_symbol", "mcp__memtrace__get_impact", "mcp__memtrace__get_cochange_context", "mcp__memtrace__recall_decision", "mcp__memtrace__why_is_this_here", "mcp__memtrace__get_timeline"]
---

# mt-preflight: pre-edit safety on a symbol

Before modifying an existing symbol, gather blast radius, hidden coupling, recorded
rationale, and stability in one pass. Do NOT start editing a non-trivial existing
function without this: the graph knows the dependency edges, the git-episode co-change
partners, and any recorded ban that a diff read alone would miss.

## Parameters

`$ARGUMENTS` is `<symbol> [repo_id]`. The first token is the target symbol name
(required); the optional second token is a `repo_id`.

## Behavior

0. Preload the deferred tool schemas:
   ```
   ToolSearch(query="select:mcp__memtrace__find_symbol,mcp__memtrace__get_impact,mcp__memtrace__get_cochange_context,mcp__memtrace__recall_decision,mcp__memtrace__why_is_this_here,mcp__memtrace__get_timeline,mcp__memtrace__list_indexed_repositories")
   ```
1. Parse inputs. If the first token is missing, ask for the symbol name and stop.
   Resolve `repo_id` from the second token if present, else via
   `list_indexed_repositories` matched against the current working directory. A
   `path: null` / `last_indexed_at: null` match is a stale stub: flag it, results may be
   incomplete.
2. Run, in order:
   - `find_symbol(name=<target>, repo_id=<repo_id>)` - confirm the exact symbol id. If
     several match, list them and ask which one.
   - `get_impact` - transitive blast radius (upstream callers plus downstream
     dependents). Highest-value probe; do not skip it.
   - `get_cochange_context` - files or symbols that historically move with this one (the
     hidden edits a diff would forget).
   - `recall_decision(query=<target> + intent)` then `why_is_this_here(symbol=<target>)`
     - any recorded decision, ban, or convention governing this code. A hit here can veto
     the planned edit.
   - `get_timeline` - recent modification history (recently churned code is less stable).
3. Emit a short risk verdict: LOW / MEDIUM / HIGH, with the caller count, the co-change
   partners to touch together, any governing decision or ban (quoted), and whether the
   symbol is stable or hot. If a ban or contract forbids the change, say so and stop
   before editing.

## Example

`/mt-preflight parseConfig` checks the symbol `parseConfig` in the cwd-resolved repo.
`/mt-preflight parseConfig backend-api` scopes the same check to `repo_id: backend-api`.

## Restrictions

Read-only pre-flight: performs no edits and never touches the live index.
