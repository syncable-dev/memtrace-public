---
name: memtrace-preflight
description: "Run a one-call pre-flight check on a single existing symbol before editing it: blast radius, co-change partners, complexity, 30-day churn, and verification checklist, then check Cortex decision memory for rationale/bans when intent may matter. Use before modifying any existing function or symbol you did not just write. Do not start editing a non-trivial existing function without pre-flight plus decision-memory recall/provenance; Memtrace knows the dependency graph, change history, and recorded decisions."
---

## Overview

Pre-flight check: the two minutes before an edit that prevent the bug. One
`preflight_check` call composes the dependency graph, the bi-temporal change
history, and the complexity metrics into an actionable radar for a single
symbol.

## Quick Reference

| Tool | Purpose |
|------|---------|
| `preflight_check` | Full radar for one symbol: blast radius + co-change + churn + checklist |
| `get_impact` | Deeper blast-radius walk when the radar flags HIGH/CRITICAL |
| `get_cochange_context` | Wider co-change window when partners look surprising |
| `recall_decision` | Recorded choices, bans, and conventions for the symbol/subsystem/behavior |
| `why_is_this_here` / `governing_contracts` | Symbol-scoped rationale and constraints when a symbol id is available |

> **Parameter types:** MCP parameters are strictly typed. Numbers must be

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).
> JSON numbers — not strings.

## Steps

### 1. Run the pre-flight check before touching the symbol

`preflight_check` with:
- `repo_id` — required
- `symbol` — the function/method name you are about to modify

### 2. Read the checklist — it is generated from the graph, not boilerplate

Each line names something concrete to verify:
- *"N symbols depend on this"* → run `get_impact` again after your edit and
  compare `total_affected`; new dependents mean your change leaked wider
  than intended.
- *"hotspot: N changes in 30 days"* → keep the edit small and reviewable;
  hot code punishes large diffs.
- *"sits in N process flow(s)"* → check the process steps still order
  correctly after the change.
- *"last touched by agent session …"* → read the latest episode before
  assuming the current shape is intentional.

### 3. Treat co-change partners as part of the change

Files that historically changed together with the target usually need
updating in the same change. If you finish the edit without touching a
frequent partner, say so explicitly and why.

### 4. After the edit, close the loop

- Re-run `preflight_check` (or `get_impact`) on the symbol.
- The complexity number should not have grown without a stated reason;
  if it did, simplify before declaring the work done.

### Cortex gate before editing

Before changing behavior, deleting code, or refactoring odd/legacy code:

1. Run `recall_decision("<symbol/subsystem/behavior>")`.
2. If the preflight/graph result gives you a numeric `symbol_id`, run
   `why_is_this_here(symbol_id)` and `governing_contracts(symbol_id)`.
3. Honor matching held decisions/bans. Treat CannotProve as unknown, not approval.

## Verdict guidance

- `risk: CRITICAL/HIGH` — propose the change plan before editing; consider
  a feature flag or staged rollout.
- `dependents: 0` — edit freely; note the symbol may be newly added or an
  entry point.
- A matching Cortex decision/ban — surface it before editing and do not
  contradict it without explicit user sign-off.

## Output

`preflight_check` returns one radar for the symbol:

| Field | Meaning |
|-------|---------|
| `risk` | verdict, e.g. `HIGH` / `CRITICAL` (see Verdict guidance) |
| `dependents` | blast radius — count of symbols that depend on the target |
| co-change partners | files that historically change with it (step 3) |
| complexity + 30-day churn | current complexity score and recent change count |
| checklist | generated verification lines, e.g. `"hotspot: 6 changes in 30 days"`, `"sits in 2 process flow(s)"` |
