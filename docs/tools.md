# Tool catalogue

> **You don't call these tools.** Your agent does, automatically, when
> you ask it questions in plain English. This page is a reference —
> for understanding what Memtrace has under the hood, debugging when
> something looks off, or for people building integrations on top
> (orchestrators, custom MCP proxies, alternative agent skills).
>
> If you're a regular user wondering "how do I use Memtrace",
> [`workflows.md`](workflows.md) is the page you want — it's framed
> around the kinds of questions you'd actually type, not tool names.

The complete list of MCP tools an agent gains when Memtrace is wired
in. Grouped by what you'd reach for them for.

All tool names are the literal strings your agent sees — for example
`mcp__memtrace__find_symbol`. Argument types are JSON; integer args
must be JSON numbers (not quoted strings), and required args are
called out per tool.

If your agent isn't using these automatically, check
[`troubleshooting.md`](troubleshooting.md#agent-isnt-using-the-mcp).

## Index management

### `list_indexed_repositories`

No arguments. Returns every repo currently in the graph along with
basic stats. Always call this first to discover valid `repo_id`
values.

### `index_directory`

Index a new repository or re-index an existing one.

| Arg | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | Absolute path to the repo root. |
| `incremental` | boolean | no (default true) | False = re-parse everything. True = only changed files. |
| `clear_existing` | boolean | no (default false) | True = drop existing graph for this repo before indexing. |

Returns a `job_id`. Indexing runs asynchronously — poll
`check_job_status(job_id)` for progress.

### `delete_repository`

Permanently remove a repo's graph data.

| Arg | Type | Required |
|---|---|---|
| `repo_id` | string | yes |

Wipes nodes, edges, episodes, and embeddings. The on-disk
`.memdb/` for that repo shrinks immediately. Source files are not
touched.

### `cleanup_stale_records`

Targeted scrub for orphan records — useful when files were deleted
while the daemon was off, when an agent worktree was removed, or
when a branch checkout left old paths resolvable. Two scopes:

| Arg | Type | Required | Notes |
|---|---|---|---|
| `repo_id` | string | yes | Required to scope the cleanup. |
| `file_path_pattern` | string | no | Substring filter — delete records whose `file_path` contains this. |
| `check_missing` | boolean | no (default false) | If true, also delete records whose `file_path` doesn't exist on disk. |
| `dry_run` | boolean | no (**default true**) | Counts what would be deleted; pass `false` to actually mutate. |

Returns scanned record count, matched paths, would-delete counts,
actually-deleted counts (zero in dry-run), and a sample of paths.

### `check_job_status`

| Arg | Type | Required |
|---|---|---|
| `job_id` | string | yes |

Returns progress + state for an async job (indexing, replay).

### `list_jobs`

No arguments. Returns recent and active jobs.

## Discovery

### `find_symbol`

Exact / fuzzy lookup by symbol name. Sub-millisecond.

| Arg | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | The symbol name. |
| `repo_id` | string | no | Restrict to one repo. |
| `fuzzy` | boolean | no (default false) | Levenshtein-tolerant. |
| `limit` | integer | no (default 10) | |

Returns `{file_path, start_line, end_line, kind, name, scope_path}`
per match.

### `find_code`

Hybrid retrieval (BM25 + vector + graph + cross-encoder rerank). Best
for natural-language queries and concept search.

| Arg | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | Free-form description, identifier, or string fragment. |
| `repo_id` | string | no | Restrict to one repo. |
| `limit` | integer | no (default 10) | |
| `file_path_filter` | string | no | Restrict to files whose path contains this substring. |

Strong on identifier-like queries and on string literals / error
messages baked into function bodies. The semantic side embeds the
first ~1500 chars of every Function / Method / Class body, so things
like `find_code("STRIPE_KEY_FOO_BAR")` find the function that uses
the constant — no `grep` needed.

### `get_symbol_context`

Full picture of a symbol — callers, callees, community, processes,
recent changes. The agent's go-to "tell me about X" tool.

| Arg | Type | Required |
|---|---|---|
| `name` or `id` | string | one of these |
| `repo_id` | string | no |

Returns the symbol's metadata, its callers and callees with
file:line, the community / module it belongs to, the processes it
participates in, and a short evolution summary.

### `get_source_window`

Read a specific span of a file with surrounding context. Cheaper
than reading the whole file when you only need a few lines.

| Arg | Type | Required |
|---|---|---|
| `file_path` | string | yes |
| `start_line` | integer | yes |
| `end_line` | integer | yes |
| `context_before` | integer | no (default 3) |
| `context_after` | integer | no (default 5) |
| `repo_id` | string | no |

Returns the requested window plus N lines of context on each side.

## Impact + dependencies

### `get_impact`

Blast radius of changing a symbol. Returns transitive callers + the
symbols that would be affected.

| Arg | Type | Required |
|---|---|---|
| `name` or `id` | string | one of these |
| `depth` | integer | no (default 2) |
| `repo_id` | string | no |

Critical before any non-trivial refactor.

### `find_dependency_path`

Show how two symbols relate in the call graph.

| Arg | Type | Required |
|---|---|---|
| `from` | string (symbol id or name) | yes |
| `to` | string (symbol id or name) | yes |
| `max_depth` | integer | no (default 5) |
| `repo_id` | string | no |

### `analyze_relationships`

Bulk relationship analysis — for each input symbol, returns its
direct callers, callees, and structural neighbours.

## Architecture overview

### `list_communities`

Louvain-detected modules — clusters of tightly-coupled symbols.
Useful as a starting point when onboarding to a new codebase.

### `find_central_symbols`

PageRank-ranked "important" symbols. The functions / classes most
other code depends on.

| Arg | Type | Required |
|---|---|---|
| `repo_id` | string | no |
| `limit` | integer | no (default 20) |

### `find_bridge_symbols`

Symbols with high betweenness centrality — the chokepoints. Touching
these has outsized blast radius.

### `get_codebase_briefing`

A short prose summary of the indexed repo: what it does, key
modules, important symbols. Good "first-look" tool.

### `get_repository_stats`

Headline numbers — file count, symbol count, edge count, language
distribution.

## Time travel

### `get_evolution`

How a symbol or file evolved over time. Six modes:

| Arg | Type | Required |
|---|---|---|
| `symbol` or `file_path` | string | one of these |
| `from` | string (relative date or ISO) | no |
| `to` | string | no |
| `mode` | string | no — one of `recent` (default), `impact`, `novelty`, `directional`, `compound`, `overview` |

### `get_timeline`

Bi-temporal version history of a symbol. Returns every version with
`valid_from` / `valid_to` timestamps tied to commits or working-tree
saves.

### `get_changes_since`

What changed in the indexed repo between `from` and `to` dates.

### `detect_changes`

Given a git diff, classify which symbols are affected and how.

### `get_cochange_context`

Files / symbols that historically change together. Surfaces hidden
coupling.

### `get_episode_replay`

Replay a specific commit or working-tree episode — see exactly what
the graph looked like at that moment.

### `replay_history`

Re-run history replay on an already-indexed repo. Useful after a
bad replay or to apply a different time window.

| Arg | Type | Required |
|---|---|---|
| `repo_id` | string | yes |
| `days` | integer | no |
| `clear_existing` | boolean | no |

### `cleanup_episodes`

Delete episodes + their historical snapshots. Run before
`replay_history` if you want to redo replay from scratch.

## Cortex decision memory

These tools are published by the normal `memtrace mcp` server. Never
configure an MCP client to launch `memcortex-mcp` directly. Start the
workspace with `memtrace start` so the local Cortex endpoint is available.

### `recall_decision`

Free-text recall over decisions, bans, and conventions. Returns ranked
evidence or an honest `CannotProve` result.

| Arg | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | Must not be empty. Use a phrase that describes the decision rather than a symbol id. |
| `top_k` | positive integer | no | Result cap. The server applies a small bounded default when omitted. Zero is invalid. |
| `min_score` | finite number | no | Exclude results below this score. |

Example:

```json
{
  "query": "why do we avoid raw SQL in request handlers?",
  "top_k": 5,
  "min_score": 0.05
}
```

### `verify_intent`

Check whether a recalled decision held across its implementation arc.
Pass the positive `decision_id` returned by `recall_decision`, not a
symbol name.

### `get_arc`

Return the episodes connected to a recalled decision. Takes the positive
`decision_id` returned by `recall_decision`.

### `why_is_this_here`

Return decision/conversation provenance governing a symbol. Takes a
positive Cortex `symbol_id`.

### `governing_contracts`

Return contracts that constrain a symbol, or `CannotProve` when Cortex
has no such evidence. Takes a positive Cortex `symbol_id`.

`index_directory`, `clear_existing`, and the source-code reset commands
do not rebuild these tools' decision store. See [`cortex.md`](cortex.md)
for the governance and recovery flow.

## Processes + flows

### `list_processes`

Named execution pipelines through the call graph (e.g. "user signup
flow", "payment processing"). Auto-detected from entry points.

### `get_process_flow`

Step-by-step execution path for a named process. Critical when
tracing a request through a service.

| Arg | Type | Required |
|---|---|---|
| `process_name` | string | yes |
| `repo_id` | string | no |

## API topology

### `find_api_endpoints`

HTTP endpoints exposed by the indexed services — Express, Encore,
NestJS, Axum, FastAPI, Flask, Gin, Spring Boot, and friends. Returns
method + path + handler + request shape.

| Arg | Type | Required |
|---|---|---|
| `repo_id` | string | no |
| `path_filter` | string | no — substring filter |

### `find_api_calls`

Where a given endpoint is called from — including across-repo HTTP
calls if you've indexed both ends.

### `get_api_topology`

Cross-service call graph. If you've indexed multiple repos, returns
the full directed graph of which services call which endpoints on
which others.

### `get_service_diagram`

Mermaid-flavoured diagram of the cross-service topology. Paste into
your docs.

## Quality

### `find_dead_code`

Symbols with zero callers. Refactoring candidates.

| Arg | Type | Required |
|---|---|---|
| `repo_id` | string | no |
| `min_size` | integer | no (default 10 lines) |

### `calculate_cyclomatic_complexity`

Cyclomatic complexity per symbol.

### `find_most_complex_functions`

Top-N functions by complexity score.

| Arg | Type | Required |
|---|---|---|
| `repo_id` | string | no |
| `limit` | integer | no (default 20) |

## Watching + uncommitted changes

### `watch_directory`

Tell the daemon to watch a path for changes. Edits create
`working_tree` episodes — your in-progress code becomes queryable
within ~1 second of saving.

### `unwatch_directory`

Reverse of the above.

### `list_watched_paths`

What's currently being watched.

### `record_external_episode`

For non-file-watcher integrations — record a manually-supplied
episode (e.g. a CI run, a deploy event) into the bi-temporal
history.

## Common tool-call sequences (agent-level, FYI)

These are the typical sequences a well-configured agent runs to
answer a given user question. Useful if you're building an
integration / custom skill / orchestrator and want to understand
the agent's expected behaviour. Regular users never see these.

When the user asks **"Tell me about `X`"** the agent typically runs:
1. `find_symbol(name="X")` or `find_code(query="X")` to locate it.
2. `get_symbol_context(id=<id from step 1>)` for callers, callees,
   community.
3. `get_source_window(file_path, start_line, end_line)` only if it
   needs to quote the body.

When the user asks **"What breaks if I change `X`?"**:
1. `find_symbol(name="X")` to locate.
2. `get_impact(id=<id>, depth=3)` for blast radius.
3. `get_evolution(name="X", mode="recent")` to check if X is
   volatile — recent churn often predicts more callers landing
   soon.

When the user asks **"Why does `X` look this way?"**:
1. `find_symbol(name="X")`.
2. `get_evolution(name="X", from=<long ago>, mode="compound")` for
   the evolution history.
3. `get_episode_replay(episode_id=<one from above>)` to see the
   exact graph state at a suspicious revision.

When the user asks **"Where's the handler for `/users/:id`?"**:
1. `find_api_endpoints(path_filter="/users")`.
2. `find_api_calls(endpoint=<found>)` for call sites — including
   cross-repo if multiple services are indexed.

When the user asks **"What's the architecture of this codebase?"**:
1. `list_indexed_repositories()`.
2. `list_communities(repo_id=<R>)` for the major modules.
3. `find_central_symbols(repo_id=<R>, limit=20)` for the
   most-depended-on symbols.
4. `list_processes(repo_id=<R>)` for named flows.
5. `get_service_diagram()` for cross-repo systems.

## Parameter type rules

The MCP server is strictly typed. Pass JSON numbers for integer
args (not quoted strings), JSON booleans for booleans, and JSON
strings for strings.

| Right | Wrong |
|---|---|
| `limit: 20` | `limit: "20"` — fails with `invalid type: string "20", expected usize` |
| `fuzzy: true` | `fuzzy: "true"` |
| `repo_id: "my-repo"` | `repo_id: my-repo` (unquoted) |

## What this catalogue isn't

This document is the **agent-facing** API. There are also a handful
of CLI subcommands (`memtrace status`, `memtrace reset`, etc.) that
humans run from the terminal — those are listed in
[`getting-started.md`](getting-started.md).
