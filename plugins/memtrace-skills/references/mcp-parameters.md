# Memtrace MCP tool parameters — authoritative reference

This file is the single source of truth for the name, type, required-ness,
default, and constraint of every argument accepted by every Memtrace MCP
tool. It's generated from the `#[derive(Deserialize, JsonSchema)] struct
*Params` declarations in `crates/memtrace-mcp/src/tools/*.rs`. If you're
wondering what to pass to a tool, check this table; if the live tool
rejects a call, the validator error message here explains why.

## Zeroth rule — JSON types are strict

The MCP validator does not coerce. `limit: "10"` is a type error even
though "10" parses as a number. Rejection surfaces as:

```
MCP error -32602: invalid type: string "10", expected usize
```

Pass JSON numbers as numbers, booleans as booleans, strings as strings,
arrays as arrays. No quoting numbers, no `"true"` for booleans.

```json
// CORRECT
{ "repo_id": "memtrace", "depth": 3, "fuzzy": true, "include_tests": false }

// WRONG — every one of these fails validation
{ "repo_id": memtrace, "depth": "3", "fuzzy": "true", "include_tests": 0 }
```

## Conventions that appear across tools

| Param | Type | Meaning |
|---|---|---|
| `repo_id` | string | Repository identifier from `list_indexed_repositories` (usually the repo folder name, e.g. `"memtrace"`) |
| `branch` *or* `branch_name` | string | Git branch. Default `"main"` across every tool. Both spellings occur historically — use whichever the specific tool's schema says |
| `limit` | integer | Cap on returned results. Always a JSON number, never a string |
| `depth` | integer | Graph traversal hops. 1–5 is reasonable; >8 explodes on wide graphs |
| `target` / `symbol` | string | Symbol **name** for graph tools — `get_impact`/`analyze_relationships` use `target`; `get_symbol_context` uses `symbol` |
| `from` / `to` / `since` / `until` | string | e.g. `"90d ago"`, `"2026-04-17T13:00:00Z"`, `"yesterday"` — relative strings work for temporal tools. **Never `days` on `get_evolution`.** |

## Search & discovery

### `find_code`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `query` | string | yes | — | Natural-language text |
| `repo_id` | string | no | all repos | Scope to one repo |
| `file_path` | string | no | — | Path substring filter |
| `limit` | integer | no | `20` | Max 100 |
| `as_of` | string | no | now | Time-travel search |
| `include_diagnostics` | boolean | no | `false` | Include `id`, `score` in results |

No `kind` param — use `find_symbol(kind=...)` instead.

### `find_symbol`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `name` | string | yes | — | Exact or partial identifier |
| `fuzzy` | boolean | no | `false` | Exact-match today; field kept for API parity |
| `edit_distance` | integer | no | `2` | Max 2. Only used when `fuzzy: true` |
| `repo_id` | string | no | all repos | |
| `kind` | string | no | — | Same enum as `find_code` |
| `file_path` | string | no | — | |
| `limit` | integer | no | `10` | Capped at `50` |

### `get_source_window`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `file_path` | string | yes | — | Use the path returned by Memtrace search/context tools |
| `repo_id` | string | no | — | Helps resolve repo-relative and repo-prefixed paths |
| `start_line` | integer | no | `1` | 1-based line number from a Memtrace result |
| `end_line` | integer | no | `start_line` | 1-based line number from a Memtrace result |
| `before_lines` | integer | no | `8` | Context before the span |
| `after_lines` | integer | no | `24` | Context after the span |
| `max_lines` | integer | no | `120` | Hard-capped at `400` |

### `list_indexed_repositories`
No parameters. Call once at session start to get `repo_id` values.

## Relationships

### `get_symbol_context`
| Field | Type | Required | Default |
|---|---|---|---|
| `repo_id` | string | yes | — |
| `symbol` | string | yes | — |
| `file_path` | string | no | — |
| `branch` | string | no | `"main"` |
| `as_of` | string | no | now |
| `view` | string | no | `"live"` |

Returns: symbol, callers, callees, type_references, community, processes, api_callers_cross_repo.

### `analyze_relationships`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `target` | string | yes | — | Symbol name |
| `query_type` | string enum | yes | — | `find_callers` \| `find_callees` \| `class_hierarchy` \| `overrides` \| `imports` \| `exporters` \| `type_usages` |
| `depth` | integer | no | `3` | Max 10 |
| `file_path` | string | no | — | Disambiguation hint |

## Impact

### `get_impact`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `target` | string | yes | — | Symbol name |
| `direction` | string enum | no | `"both"` | `"upstream"` \| `"downstream"` \| `"both"` |
| `depth` | integer | no | `5` | Max 15 |
| `as_of` | string | no | now | |

### `detect_changes`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `diff` | string | no ‡ | — | Unified git diff text |
| `changed_files` | array of string | no ‡ | — | Alternative to `diff` |
| `branch` | string | no | `"main"` | |

‡ Exactly one of `diff` or `changed_files` must be provided.

## Temporal

### `get_evolution`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `from` | string | yes | — | Start of window. RFC3339, ISO date, epoch, or relative (`"7d ago"`, `"yesterday"`). **Never pass `days`.** |
| `to` | string | no | now | End of window |
| `mode` | string enum | no | `"recent"` | `"recent"` \| `"compound"` \| `"summary"` \| `"overview"` (alias for `summary`) |
| `target` | string | no | — | Symbol name or file path substring scope |
| `branch` | string | no | any branch | |
| `file_path` | string | no | — | **`recent` mode only** — substring match on `touched_files` |
| `kind` | string | no | — | **`recent` mode only** — `"git_commit"` or `"working_tree"` |
| `limit` | integer | no | `100` | **`recent` mode only** — page size; `0` = server cap |
| `cursor` | integer | no | `0` | **`recent` mode only** — pagination offset |

**Response by mode:**
- `recent` → `episodes[]`, `totals`, `page` (with `next_cursor`)
- `compound` → `totals`, `top_changed_files`, `top_touched_symbols`
- `summary` / `overview` → `totals`, `first_episode`, `last_episode`

Modes **`impact`**, **`novel`**, **`directional`** are **not implemented**.

### `get_timeline`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `scope_path` | string | yes | — | e.g. `"AuthService::validateToken"` |
| `file_path` | string | yes | — | Containing file |
| `branch` | string | no | `"main"` | |

### `get_episode_replay`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `episode_id` | string | † | — | Episode UUID — preferred when known |
| `repo_id` | string | † | — | Required with `episode_index` when `episode_id` omitted |
| `episode_index` | integer | † | — | 0 = newest episode |
| `branch` | string | no | any branch | Used with `episode_index` |
| `symbol` | string | no | — | Optional filter — scope to one symbol |
| `kind` | string | no | — | e.g. `Function`, `CALLS` — narrow large commits |
| `file_path` | string | no | — | Substring filter on record paths |
| `limit` | integer | no | `200` | Page size per bucket; `0` = no pagination |
| `cursor` | integer | no | `0` | Pagination offset |
| `mode` | string | no | default | `"graph_summary"` for digest on large commits |
| `compress` | boolean | no | `true` | Collapse identical-hash modifications |

† Supply `episode_id`, OR `repo_id` + `episode_index`.

### `get_changes_since`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `since` | string | yes | — | Cutoff timestamp. Same formats as `get_evolution.from`. **Not** `last_episode_id`. |
| `until` | string | no | now | Optional upper bound |
| `branch` | string | no | any branch | |

Returns `episodes[]` with per-episode change counts and `totals`. Store `until` (or latest episode `reference_time`) as the next session's `since` value.

### `get_cochange_context`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `target` | string | yes | — | Symbol name or file path |
| `window_days` | integer | no | `30` | Lookback from `as_of` |
| `as_of` | string | no | now | Window anchor |
| `branch` | string | no | any branch | |
| `limit` | integer | no | `10` | Max cochanged files returned |

Returns `cochanged_files[]` with `file_path`, `cochange_count`, `last_cochanged_at`.

## Graph algorithms

### `find_central_symbols`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `branch` | string | no | any branch | |
| `kinds` | array of string | no | Function/Method/Class/… | |
| `limit` | integer | no | `25` | Max 100. Always PageRank — no `method` param. |

### `find_bridge_symbols`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `branch` | string | no | `"main"` | |
| `limit` | integer | no | `15` | Max 50 |

### `list_communities`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `branch` | string | no | `"main"` | |
| `min_size` | integer | no | `3` | |
| `limit` | integer | no | `50` | Max 200 |

### `list_processes`
| Field | Type | Required | Default |
|---|---|---|---|
| `repo_id` | string | yes | — |
| `branch` | string | no | `"main"` |
| `limit` | integer | no | `50` |

### `get_process_flow`
| Field | Type | Required | Default |
|---|---|---|---|
| `repo_id` | string | yes | — |
| `process` | string | yes | — |
| `branch` | string | no | `"main"` |

### `find_dependency_path`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `source` | string | yes | — | Start symbol name |
| `target` | string | yes | — | End symbol name |
| `max_depth` | integer | no | `20` | Max 20 |
| `edge_type` | string | no | calls+refs+… | Comma-separated or alias |

## Quality

### `get_repository_stats`
| Field | Type | Required | Default |
|---|---|---|---|
| `repo_id` | string | yes | — |
| `branch` | string | no | `"main"` |

### `find_dead_code`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `branch` | string | no | `"main"` | |
| `include_tests` | boolean | no | `false` | |
| `limit` | integer | no | `50` | |
| `kinds` | array of string | no | all | Filter, e.g. `["Function","Method"]` |

### `find_most_complex_functions`
| Field | Type | Required | Default |
|---|---|---|---|
| `repo_id` | string | yes | — |
| `branch` | string | no | `"main"` |
| `top_n` | integer | no | `20` |
| `kinds` | array of string | no | Function+Method |

### `calculate_cyclomatic_complexity`
| Field | Type | Required | Default |
|---|---|---|---|
| `repo_id` | string | yes | — |
| `target` | string | yes | — |

## API topology

### `find_api_endpoints`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | The service that handles these endpoints |
| `method` | string | no | — | HTTP method filter, e.g. `"GET"` |
| `path_contains` | string | no | — | Substring filter, e.g. `"/users"` |
| `branch` | string | no | `"main"` | |
| `limit` | integer | no | `50` | |

### `find_api_calls`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | The service making the calls |
| `method` | string | no | — | |
| `path_contains` | string | no | — | |
| `branch` | string | no | `"main"` | |
| `limit` | integer | no | `50` | |

### `get_api_topology`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `min_confidence` | number (float) | no | `0.7` | 0.0–1.0 — threshold for cross-repo HTTP edges |
| `include_external` | boolean | no | `false` | Include calls to services outside indexed repos |
| `repo_id` | string | no | — | Omit for full cross-service topology |

### `link_repositories`
Connects already-indexed repos so `get_api_topology` can stitch their HTTP graphs. Parameters vary — see `link_repositories` tool schema via the MCP `list_tools` call.

## Indexing

### `index_directory`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `path` | string | yes | — | Absolute path to the repo root |
| `repo_id` | string | no | directory name | |
| `branch` | string | no | `"main"` | |
| `incremental` | boolean | no | `false` | Re-index only changed files |
| `clear_existing` | boolean | no | `false` | Wipe before indexing |
| `skip_embed` | boolean | no | `false` | Skip the embedding stage |

### `check_job_status`
| Field | Type | Required | Default |
|---|---|---|---|
| `job_id` | string (UUID) | yes | — |

### `delete_repository`
| Field | Type | Required | Default |
|---|---|---|---|
| `repo_id` | string | yes | — |

### `watch_directory`
| Field | Type | Required | Default |
|---|---|---|---|
| `path` | string | yes | — |
| `repo_id` | string | yes | — |
| `branch` | string | no | `"main"` |

### `unwatch_directory`
| Field | Type | Required |
|---|---|---|
| `path` | string | yes |

### `list_watched_paths`
No parameters. Returns `{ watches: [{ path, repo_id, branch, started_at, origin }], count }`.

### `replay_history`
| Field | Type | Required | Default |
|---|---|---|---|
| `repo_id` | string | yes | — |
| `days` | integer | no | all history |

## Session bookends & pre-edit checks

### `get_daily_briefing`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `window_hours` | integer | no | `24` | Clamped to 1–336 (14 days) |

### `review_agent_sessions`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `window_hours` | integer | no | `24` | Clamped to 1–336. Saves >30 min apart split into separate sessions |

### `find_hotspots`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `window_days` | integer | no | `14` | Kept inside the engine's 15-day hot-history horizon; deeper windows trigger a full-store scan |
| `top_n` | integer | no | `20` | Max rows |

### `preflight_check`
| Field | Type | Required | Default |
|---|---|---|---|
| `repo_id` | string | yes | — |
| `symbol` | string | yes | — |

## Cortex decision memory

Exposed on the normal `memtrace` MCP server and proxied to the local MemCortex
sidecar when Cortex is enabled. Ids are **JSON numbers** (Cortex node ids), not
strings — `decision_id: 1`, never `"1"`.
An unknown or empty query/id returns an explicit **CannotProve**, never a
fabricated result.

### `recall_decision`
| Field | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | Free text; drives the lexical lane, decisions ranked first |
| `top_k` | integer | no | Optional result cap; must be > 0 |
| `min_score` | number | no | Optional finite score floor |

(A tier-based decision cap is applied server-side; it is not a caller parameter.)

### `get_arc` / `verify_intent`
| Field | Type | Required | Notes |
|---|---|---|---|
| `decision_id` | integer | yes | The `id` of a `kind: "decision"` hit from `recall_decision` |

### `why_is_this_here` / `governing_contracts`
| Field | Type | Required | Notes |
|---|---|---|---|
| `symbol_id` | integer | yes | Cortex symbol node id |

## When this file is wrong

It's drift-prone. Regenerate by grepping `crates/memtrace-mcp/src/tools/*.rs`
and `crates/memtrace-mcp/src/cortex_client.rs` for `Params` declarations — the
Rust declarations are the source of truth.
If a live tool call rejects with `-32602`, trust the Rust struct over this
doc and file a fix PR.
