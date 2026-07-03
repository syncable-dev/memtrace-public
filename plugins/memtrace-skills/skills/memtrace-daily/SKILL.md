---
name: memtrace-daily
description: "Orient at the start of a coding session, review what recently changed in a repository, and self-audit after completing work. Use when the user wants the daily briefing (what changed in the last 24h with complexity deltas), hotspots (complexity × churn refactor priorities), or a session review (clean/review/risky verdicts per editing session). For catching up after time away or resuming a prior session, use memtrace-session-continuity; daily is the last-24h briefing + hotspots + self-audit. Do not reconstruct recent activity from git log; Memtrace diffs the graph at save granularity."
allowed-tools:
  - mcp__memtrace__get_daily_briefing
  - mcp__memtrace__find_hotspots
  - mcp__memtrace__review_agent_sessions
  - mcp__memtrace__preflight_check
metadata:
  author: "Syncable <support@syncable.dev>"
  version: "1.0.0"
  category: development
---

## Overview

Session bookends: orient before you start, audit before you finish. All
three tools read the bi-temporal version history — every save is a change
event with complexity deltas, so "what happened" includes "did it make the
code better or worse".

## Quick Reference

| Tool | Purpose |
|------|---------|
| `get_daily_briefing` | Last 24h diffed at graph level: changed functions with complexity deltas, new functions, new endpoints, per-module distribution |
| `find_hotspots` | Functions ranked by complexity × recent changes — the ordered refactor priority list |
| `review_agent_sessions` | Editing sessions clustered by actor, judged clean / review / risky |

> **Parameter types:** MCP parameters are strictly typed. Numbers

Full parameter spec for every Memtrace tool: [references/mcp-parameters.md](../../references/mcp-parameters.md).
> (`window_hours`, `window_days`, `top_n`) must be JSON numbers.

## Steps

### 1. Orient at session start

`get_daily_briefing` with `repo_id`. Read the summary first:
- `net_cyclomatic_delta` positive → recent work added complexity; check
  whether your task touches the same area before piling on.
- `changed` rows with large positive deltas → recently-destabilised code;
  prefer `preflight_check` before editing anything in that list.

### 2. When the task is refactoring or cleanup

`find_hotspots` — work the list top-down. A hotspot you simplify pays off
on every future change; a complex-but-untouched function can wait.

### 3. Self-audit before declaring work done

`review_agent_sessions` and find YOUR session (newest, your agent id):
- `clean` — done; state the verdict in your summary.
- `review` — re-read your top_changes rows; justify each complexity
  increase or simplify it.
- `risky` — do not hand off yet. You added >30 net cyclomatic or touched
  a >50-complexity function; extract helpers / flatten nesting first,
  then re-run the review.

## The standard a session should meet

Net complexity delta ≤ 0 unless the task genuinely required new branching
(new feature with new cases). "I left the code simpler than I found it"
is verifiable here — verify it.

## Output

| Tool | Returns |
|------|---------|
| `get_daily_briefing` | summary with `net_cyclomatic_delta`; `changed` rows (function + complexity delta); new functions, new endpoints, per-module distribution |
| `find_hotspots` | ranked list of functions scored by complexity × recent changes |
| `review_agent_sessions` | per-session verdict (`clean` / `review` / `risky`) with `top_changes` rows and net cyclomatic delta |
