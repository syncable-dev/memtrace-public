---
name: memtrace-incident-investigation
description: "Root-cause workflow for incidents / regressions / production bugs on an indexed codebase — chains get_evolution(recent) → find_bridge_symbols → get_process_flow → get_cochange_context. USE when 'X broke at Y time, find why' — you have an incident timestamp and need to narrow down candidate changes. DO NOT USE for pre-change risk assessment of a planned refactor (→ memtrace-change-impact-analysis), for symbol blast-radius without a failure context (→ memtrace-impact), for bug investigation unrelated to recent changes (→ memtrace-search + memtrace-relationships), or when the user hasn't specified an incident window (ask for one first)."
---

> **Tool argument schemas:** this skill calls several MCP tools. For the authoritative name / type / required / default / constraint of every argument, see `references/mcp-parameters.md`. Pass JSON numbers as numbers, booleans as booleans — the MCP validator does not coerce.

## Overview

Root cause investigation workflow for incidents, regressions, and production issues. Uses temporal analysis with the `recent` scoring mode to surface changes closest to the incident time, then traces blast radius and execution flows to identify the likely cause.

## Steps

### 1. Establish the timeline

Determine:
- **Incident time** — when did the problem start? (This becomes the `to` parameter)
- **Lookback window** — how far back to search? Start with 24 hours, expand if needed.
- **Repo(s)** — which services are affected? Call `list_indexed_repositories` to get repo_ids.

### 2. Surface recent changes near the incident

Call `get_evolution` with:
- `mode: "recent"` — temporal proximity weighting: `impact × exp(−0.5 × Δhours)`
- `from` — lookback start (e.g., 24 hours before incident)
- `to` — the incident timestamp
- `repo_id` — the affected repo

**Why `recent` mode?** It exponentially amplifies changes close to the incident time while still weighting by structural impact. A high-impact change made 1 hour before the incident scores much higher than the same change made 20 hours before.

**Success criteria:** A ranked list of changes, with the most likely culprits at the top.

### 3. Check for unexpected changes

Call `get_evolution` again with `mode: "novel"` on the same time window:
- Flags changes to rarely-modified code (high in_degree, low change frequency)
- A core utility that hasn't changed in 90 days suddenly changing near an incident is a strong signal

**Decision:** If `novel` mode surfaces different symbols than `recent` mode, investigate both — the root cause may be an unexpected change to stable infrastructure.

### 4. Trace the blast radius of top suspects

For the top 3–5 symbols from steps 2–3, call `get_impact` with `direction: upstream`:
- How many downstream consumers were affected?
- What execution flows pass through this symbol?

**Decision:** Prioritize symbols where the blast radius overlaps with the reported failure area.

### 5. Trace execution flows

Use `get_symbol_context` on the top suspects to see which processes (HTTP handlers, background jobs, etc.) they participate in.

**Decision:** If the incident is in a specific endpoint/flow, focus on suspects that are members of that process.

### 6. Build the full timeline for the suspect

Once you have a primary suspect, call `get_timeline` with the symbol name to see its full version history:
- What changed in each commit?
- When was the last "stable" version?
- Was the change a modification, or was it newly added?

### 7. Correlate with surrounding changes

Call `get_evolution` with mode `directional` to separate:
- **Added symbols** — new code introduced (potential new bugs)
- **Removed symbols** — deleted code (potential missing functionality)
- **Modified symbols** — changed behaviour (potential regressions)

### 8. Check historical coupling (cochange)

For the primary suspect, call `get_cochange_context`:
- Which symbols historically co-change with this one?
- If the blast radius from `get_impact` doesn't explain the failure area, check cochange partners — the coupling may be behavioral, not structural.

**Decision:** If a cochange partner is in the failure area but has no direct call relationship to the suspect, it's a hidden dependency — investigate both.

### 9. Replay the sub-commit implementation history (if needed)

If the suspect's commit history doesn't explain the intent, call `get_episode_replay`:
- What was tried before the final committed state?
- Was any approach attempted and reverted in the same session?
- The `attempted_and_reverted` hint often explains why seemingly-correct code was changed to something subtler.

## Report: Root Cause Analysis

1. **Incident Timeline** — when it started, what was observed
2. **Most Likely Cause** — the top-ranked change(s) by `recent` mode with blast radius confirmation
3. **Supporting Evidence** — novelty signal (was this an unexpected change?), blast radius overlap with failure area, process membership overlap
4. **Change History** — full timeline of the suspect symbol
5. **Affected Scope** — all processes and downstream consumers impacted
6. **Remediation** — revert the change, fix forward, or mitigate

## Algorithm Selection Guide for Incidents

| Phase | Tool / Mode | Why |
|-------|-------------|-----|
| Initial triage | `get_evolution` `recent` | Time-weighted ranking surfaces changes near the incident |
| Anomaly detection | `get_evolution` `novel` | Catches unexpected changes to stable code |
| Scope assessment | `get_impact` | Ranks by structural significance (blast radius) |
| Hidden coupling | `get_cochange_context` | Surfaces behavioral coupling not in the call graph |
| Direction analysis | `get_evolution` `directional` | Separates added/removed/modified |
| Sub-commit intent | `get_episode_replay` | Reveals what was tried before the committed state |
| Quick summary | `get_evolution` `overview` | Fast module-level scan before deep-diving |

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| Starting with `impact` mode | Use `recent` first — time proximity is the strongest signal for incidents |
| Only looking at the most recent commit | The root cause may be from an earlier change whose effects were delayed |
| Ignoring `novel` mode | Unexpected changes to stable code are often the root cause |
| Not checking blast radius overlap | A change is only a suspect if its blast radius reaches the failure area |
| Stopping at call graph analysis | `get_cochange_context` finds hidden coupling — symbols that move together without calling each other |
| Reading only committed code | `get_episode_replay` reveals tried-and-reverted approaches that explain the current implementation |
