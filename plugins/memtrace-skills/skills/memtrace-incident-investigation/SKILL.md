---
name: memtrace-incident-investigation
description: "Investigate source-code bugs, incidents, regressions, production issues, and failures to root cause with Memtrace symbol search, impact, call graph, and temporal history. Use when the user asks about root cause analysis, what broke, or what changed when debugging a failure. Do not start with Grep, Glob, rg, find, or manual file search for code causes. For plain what-changed questions without a failure, use memtrace-evolution."
---

## Overview

Root cause investigation workflow for incidents, regressions, and production issues. Uses `get_evolution` to list changes near the incident time, then traces blast radius and execution flows to identify the likely cause.

## Steps

### 1. Establish the timeline

Determine:
- **Incident time** — when did the problem start? (This becomes the `to` parameter)
- **Lookback window** — how far back to search? Start with 24 hours, expand if needed.
- **Repo(s)** — which services are affected? Call `list_indexed_repositories` to get repo_ids.

### 2. List changes near the incident

Call `get_evolution`:

```json
{
  "repo_id": "<affected-repo>",
  "from": "<incident_time minus lookback, e.g. 24h before>",
  "to": "<incident_time ISO-8601>",
  "mode": "recent",
  "limit": 100
}
```

**Why `recent` mode?** Returns a chronological per-episode changelog. Focus on episodes whose `reference_time` is closest to the incident — especially those with high `nodes_added` + `nodes_removed` or that touch files in the failure area.

Paginate with `cursor` if `next_cursor` is present in the response.

**Success criteria:** A list of episodes in the window, with touched files and change counts for each.

### 3. Identify hotspot files and symbols

Call `get_evolution` again on the same window with `mode: "compound"`:

```json
{
  "repo_id": "<affected-repo>",
  "from": "<same as step 2>",
  "to": "<same as step 2>",
  "mode": "compound"
}
```

Review `top_changed_files` and `top_touched_symbols`. Cross-reference with the failure area (endpoint, module, error stack).

**Decision:** Prioritize symbols/files that appear in both the recent episode list (step 2) and the compound hotspots (step 3).

### 4. Check for unexpected changes to stable code

For hotspot symbols from step 3, resolve each with `find_symbol`:

```json
{ "repo_id": "<affected-repo>", "name": "<symbol>", "limit": 10 }
```

Then call `get_timeline` — stable code that suddenly changed is suspect:

```json
{ "repo_id": "<affected-repo>", "scope_path": "<from find_symbol>", "file_path": "<from find_symbol>" }
```

### 5. Assess the blast radius

For the top 3–5 symbols, call `get_impact`:

```json
{ "repo_id": "<affected-repo>", "target": "<symbol>", "direction": "upstream" }
```
- How many downstream consumers were affected?
- What execution flows pass through this symbol?

**Decision:** Prioritize symbols where the blast radius overlaps with the reported failure area.

### 6. Trace execution flows

Use `get_symbol_context` on the top suspects to see which processes (HTTP handlers, background jobs, etc.) they participate in.

**Decision:** If the incident is in a specific endpoint/flow, focus on suspects that are members of that process.

### 7. Build the full timeline for the suspect

Once you have a primary suspect, call `get_timeline` with `repo_id`, `scope_path`, and `file_path`:
- What changed in each episode?
- When was the last "stable" version?
- Was the change a modification, or was it newly added?

### 8. Correlate adds vs removes per episode

From the step 2 `recent` response, inspect each episode's `nodes_added` and `nodes_removed`:
- High `nodes_added` — new code introduced (potential new bugs)
- High `nodes_removed` — deleted code (potential missing functionality)
- Both moderate — changed behaviour (potential regressions)

### 9. Check historical coupling (cochange)

For the primary suspect, call `get_cochange_context`:

```json
{ "repo_id": "<affected-repo>", "target": "<symbol>", "limit": 10 }
```
- Which symbols historically co-change with this one?
- If the blast radius from `get_impact` doesn't explain the failure area, check cochange partners — the coupling may be behavioral, not structural.

**Decision:** If a cochange partner is in the failure area but has no direct call relationship to the suspect, it's a hidden dependency — investigate both.

### 10. Replay the sub-commit implementation history (if needed)

If the suspect's episode isn't clear, call `get_episode_replay`:

```json
{
  "repo_id": "<affected-repo>",
  "episode_index": 0,
  "symbol": "<suspect>",
  "mode": "graph_summary"
}
```

- Look for `attempted_and_reverted` hints — approaches tried and rolled back within the episode often explain why the committed state looks the way it does.

## Report: Root Cause Analysis

1. **Incident Timeline** — when it started, what was observed
2. **Most Likely Cause** — episodes and symbols closest to the incident with blast radius confirmation
3. **Supporting Evidence** — timeline sparsity (stable code suddenly changed?), blast radius overlap, process membership overlap
4. **Change History** — full timeline of the suspect symbol
5. **Affected Scope** — all processes and downstream consumers impacted
6. **Remediation** — revert the change, fix forward, or mitigate

## Tool selection guide for incidents

| Phase | Tool / mode | Why |
|---|---|---|
| Initial triage | `get_evolution` `recent` | Per-episode changelog near the incident |
| Hotspot identification | `get_evolution` `compound` | Top changed files and symbols in the window |
| Scope assessment | `get_impact` | Blast radius of suspect symbols |
| Hidden coupling | `get_cochange_context` | Behavioral coupling not in the call graph |
| Symbol history | `get_timeline` | Full version history of a suspect |
| Sub-commit intent | `get_episode_replay` | What was tried before the committed state |
| Quick window check | `get_evolution` `overview` | Totals only — use before narrowing the window |

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

## Output

The deliverable is the RCA report above. Abridged example (3 of 6 sections filled):

- **Most Likely Cause** — episode `2026-07-01T14:22Z` modified `AuthService::validateToken`; upstream blast radius reaches the failing `/api/login` process
- **Supporting Evidence** — symbol was stable for 90 days before this episode; cochange partner `token_cache.rs` changed in the same window despite no call edge
- **Remediation** — revert the episode, or fix forward with an expiry guard

## Common mistakes

| Mistake | Reality |
|---|---|
| Omitting `from` or passing `days` | Always pass `from` (e.g. `"24 hours ago"`) — never `days` |
| Using `mode: "novel"` or `"directional"` | Not implemented — use `compound` + `get_timeline` + `get_cochange_context` |
| Only looking at the most recent commit | The root cause may be from an earlier episode whose effects were delayed |
| Not checking blast radius overlap | A change is only a suspect if its blast radius reaches the failure area |
| Stopping at call graph analysis | `get_cochange_context` finds hidden coupling — symbols that move together without calling each other |
| Reading only committed code | `get_episode_replay` reveals tried-and-reverted approaches that explain the current implementation |
