---
name: memtrace-evolution
description: "Use when the user asks what changed in the codebase, how code evolved over time, what was recently modified, what's the diff between versions, what changed since a date, incident investigation timeline, unexpected changes, change history, or temporal analysis of any kind"
allowed-tools:
  - mcp__memtrace__get_evolution
  - mcp__memtrace__get_timeline
  - mcp__memtrace__detect_changes
  - mcp__memtrace__list_indexed_repositories
  - mcp__memtrace__get_changes_since
user-invocable: true
---

## Overview

Multi-mode temporal analysis engine that answers "what changed and why should I care?" across arbitrary time windows. Uses Structural Significance Budgeting (SSB) to surface the most important changes without overwhelming you with noise.

This is memtrace's most powerful analytical tool. It implements six distinct scoring algorithms — choose the right one based on what the user needs.

## Query Modes — Choose the Right Algorithm

| Mode | Algorithm | Best For |
|------|-----------|----------|
| `compound` | Rank-fusion: 0.50×impact + 0.35×novel + 0.15×recent | **Default.** General-purpose "what changed?" — use when unsure |
| `impact` | Structural Significance: `sig(n) = in_degree^0.7 × (1 + out_degree)^0.3` | "What broke?" — finds changes with the largest blast radius |
| `novel` | Change Surprise Index: `surprise(n) = (1 + in_degree) / (1 + change_freq_90d)` | "What's unexpected?" — anomaly detection for rarely-changing code |
| `recent` | Temporal Proximity: `impact × exp(−0.5 × Δhours)` | "What changed near the incident?" — time-weighted for root cause |
| `directional` | Asymmetric scoring (added→out_degree, removed→in_degree, modified→impact) | "What was added vs removed?" — structural change direction |
| `overview` | Fast module-level rollup only | Quick summary — no per-symbol scoring, just module counts |

## Steps

### 1. Determine the time window

Ask the user or infer:
- `from` — ISO-8601 start timestamp (required)
- `to` — ISO-8601 end timestamp (defaults to now)
- `repo_id` — scope to a repo (call `list_indexed_repositories` if unknown)

### 2. Choose the mode

**Decision tree:**

```
User wants to know...
├── "what changed?"           → compound (default)
├── "what could have broken?" → impact
├── "anything unexpected?"    → novel
├── "what changed near X?"    → recent (set to to incident time)
├── "what was added/removed?" → directional
└── "quick summary?"          → overview
```

### 3. Execute the query

Use the `get_evolution` MCP tool with:
- `repo_id` — required
- `from` / `to` — the time window
- `mode` — one of: compound, impact, novel, recent, directional, overview

### 4. Interpret results

The response contains:

- **`added[]`** — new symbols that appeared in the time window
- **`removed[]`** — symbols that were deleted
- **`modified[]`** — symbols that changed
- **`by_module[]`** — module-level rollup (NEVER truncated — always shows all modules)
- **`significance_coverage`** — fraction of total significance captured (target: ≥0.80)
- **`budget_exhausted`** — if true, there were more significant changes than the budget allowed

Each symbol includes: `name`, `kind`, `file_path`, `scope_path`, `in_degree`, `out_degree`, and all four scores (`impact`, `novel`, `recent`, `compound`).

### 5. Drill deeper

- **For a single symbol's full history:** Use `get_timeline` with the symbol name
- **For diff-based change scope:** Use `detect_changes` when you have a specific diff/patch
- **For blast radius of a specific change:** Use `get_impact` on high-scoring symbols

## Scoring Algorithms — Detailed Reference

### Impact Score (Structural Significance Budgeting)
```
sig(n) = in_degree^0.7 × (1 + out_degree)^0.3
```
- Heavily weights callers (in_degree) — symbols called by many others have high blast radius
- Mild boost for outbound complexity (out_degree) — complex functions that changed are notable
- SSB selects the minimum set covering ≥80% of total significance mass

### Novelty Score (Change Surprise Index)
```
surprise(n) = (1 + in_degree) / (1 + change_freq_90d)
```
- High in_degree + low change frequency = **maximum surprise**
- A core utility that hasn't changed in 90 days suddenly changing → likely worth investigating
- Low in_degree + high frequency = routine churn, deprioritized

### Recent Score (Temporal Proximity Weighting)
```
recent(n) = impact(n) × exp(−0.5 × |Δhours to reference|)
```
- Exponential decay from the reference timestamp (the `to` parameter)
- Changes close to an incident get amplified; older changes fade
- Best for incident timelines: set `to` to the incident timestamp

### Compound Score (Rank Fusion)
```
compound = 0.50×rank(impact) + 0.35×rank(novel) + 0.15×rank(recent)
```
- Rank-based fusion avoids scale sensitivity between different score types
- Impact-dominant but boosted by novelty and recency
- Best default when you don't have a specific hypothesis

## Auto-overview Safety

If a time window produces more than 500 candidates and mode is not `overview`, the query **automatically downgrades to overview mode** and returns `auto_overview: true`. This prevents timeouts on wide windows. When you see `auto_overview: true`:
- Narrow the window, OR
- Switch to `get_changes_since` (which handles this automatically), OR
- Use the `by_module` rollup to identify the specific area and query a tighter window

## Session-Aware Alternative

If you're resuming work after a break and don't know the right `from` timestamp, use `get_changes_since` instead — it accepts a `last_episode_id` anchor and never requires timestamp guessing.

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| Using `overview` when user needs details | Overview only gives module-level counts — use `compound` for symbol-level |
| Ignoring `budget_exhausted` flag | If true, there are more significant changes beyond what was returned — narrow the time window or use module rollup |
| Not checking `by_module` first | Module rollup is never truncated — scan it to identify which areas changed before diving into symbol-level |
| Using `recent` without setting `to` | The `to` timestamp is the reference point for proximity weighting — set it to the incident/event time |
| Guessing timestamps when resuming work | Use `get_changes_since` with a stored `session_anchor` instead — exact episode boundary, no guessing |
