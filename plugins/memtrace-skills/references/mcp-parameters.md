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
| `symbol_id` | string (UUID) | Node UUID from `find_symbol` / `find_code` results |
| `from` / `to` / `incident_time` | string (ISO-8601 with timezone) | e.g. `"2026-04-17T13:00:00Z"` — NOT a date like `"2026-04-17"` |

## Search & discovery

### `find_code`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `query` | string | yes | — | Natural-language text |
| `repo_id` | string | no | all repos | Scope to one repo |
| `kind` | string | no | — | One of `Function`, `Class`, `Method`, `Interface`, `APIEndpoint`, `APICall` |
| `file_path` | string | no | — | Path substring filter |
| `limit` | integer | no | `20` | Capped at `100` |
| `as_of` | string (ISO-8601) | no | now | Time-travel search |

### `find_symbol`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `name` | string | yes | — | Exact or partial identifier |
| `fuzzy` | boolean | no | `false` | Levenshtein tolerance |
| `edit_distance` | integer | no | `2` | Max 2. Only used when `fuzzy: true` |
| `repo_id` | string | no | all repos | |
| `kind` | string | no | — | Same enum as `find_code` |
| `file_path` | string | no | — | |
| `limit` | integer | no | `10` | Capped at `50` |

### `list_indexed_repositories`
No parameters. Call once at session start to get `repo_id` values.

## Relationships

### `get_symbol_context`
| Field | Type | Required | Default |
|---|---|---|---|
| `symbol_id` | string (UUID) | yes | — |

Returns: symbol, callers, callees, type_references, community, processes, api_callers_cross_repo.

### `analyze_relationships`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `symbol_id` | string (UUID) | yes | — | |
| `query_type` | string enum | yes | — | `find_callers` \| `find_callees` \| `class_hierarchy` \| `overrides` \| `imports` \| `exporters` \| `type_usages` |
| `depth` | integer | no | `2` | |
| `limit` | integer | no | `50` | |

## Impact

### `get_impact`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `symbol_id` | string (UUID) | yes | — | |
| `direction` | string enum | no | `"both"` | `"upstream"` \| `"downstream"` \| `"both"` |
| `depth` | integer | no | `3` | |
| `limit` | integer | no | `100` | |

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
| `from` | string (ISO-8601) | yes | — | Start of window |
| `to` | string (ISO-8601) | yes | — | End of window |
| `mode` | string enum | no | `"compound"` | `"compound"` \| `"impact"` \| `"novel"` \| `"recent"` \| `"directional"` \| `"overview"` |
| `incident_time` | string (ISO-8601) | no | — | Reference for `recent` mode |
| `branch` | string | no | `"main"` | |
| `max_symbols` | integer | no | `50` | Per category |
| `scope` | string | no | — | File / module prefix |

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
| `repo_id` | string | yes | — | |
| `symbol` | string | yes | — | Symbol name, e.g. `"validateToken"` |
| `from` | string (ISO-8601) | yes | — | Window start |
| `to` | string (ISO-8601) | yes | — | Window end |
| `branch` | string | no | `"main"` | |
| `include_working_tree` | boolean | no | `true` | Include uncommitted file-save episodes |

### `get_changes_since`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `last_episode_id` | string | no † | — | Preferred anchor from previous response |
| `last_reference_time` | string (ISO-8601) | no † | — | Fallback when no episode ID |
| `branch` | string | no | `"main"` | |

† Pass exactly one.

### `get_cochange_context`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `symbol` | string | yes | — | Symbol name (not ID) |
| `branch` | string | no | `"main"` | |
| `limit` | integer | no | `20` | |

## Graph algorithms

### `find_central_symbols`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `branch` | string | no | `"main"` | |
| `algorithm` | string enum | no | `"pagerank"` | `"pagerank"` \| `"degree"` — falls back to degree if MAGE unavailable |
| `limit` | integer | no | `20` | Max 100 |

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

### `execute_cypher`
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `query` | string | yes | — | Read-only Cypher. Write keywords (CREATE/MERGE/DELETE/SET) are rejected |
| `params` | object | no | `{}` | JSON map of parameter bindings |
| `repo_id` | string | no | — | Injected into `params` as `$repo_id` |

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
| `limit` | integer | no | `10` |
| `min_complexity` | integer | no | `0` |

### `calculate_cyclomatic_complexity`
| Field | Type | Required | Default |
|---|---|---|---|
| `symbol_id` | string (UUID) | yes | — |

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

### `watch_directory` / `unwatch_directory` / `list_watched_paths`
File watcher control. See the tool schema via `list_tools` for current fields.

## When this file is wrong

It's drift-prone. Regenerate by grepping `crates/memtrace-mcp/src/tools/*.rs`
for `^pub struct.*Params` — the Rust declarations are the source of truth.
If a live tool call rejects with `-32602`, trust the Rust struct over this
doc and file a fix PR.
