---
name: memtrace-intent-verification
description: "Verify whether a past decision actually held or was violated, and surface the arc of episodes that implemented that specific decision. Returns a Held | ViolatedAt | CannotProve verdict and the implementing arc from Cortex decision memory. Use when the user asks whether a decision held, was followed, or drifted, or before relying on a decision or reporting drift. Requires a decision_id (from memtrace-decision-recall); do NOT use for free-text decision lookup — use memtrace-decision-recall first. Do not assume a decision was followed; verify it."
allowed-tools:
  - mcp__memtrace__verify_intent
  - mcp__memtrace__get_arc
  - mcp__memtrace__recall_decision
  - mcp__memtrace__why_is_this_here
metadata:
  author: "Syncable <support@syncable.dev>"
  version: "1.0.0"
  category: development
---

## Overview

A decision being *recorded* doesn't mean it was *followed*. `verify_intent` checks
whether a decision held across its arc; `get_arc` returns the episodes that implemented
it. Use these to confirm a rationale is still in force before relying on it, and to
detect drift where the code quietly diverged from what was decided.

Full parameter spec for every Memtrace tool: [references/mcp-parameters.md](../../references/mcp-parameters.md).

## Quick Reference

| Tool | Purpose | Returns / FactStatus |
|------|---------|----------------------|
| `verify_intent` | Did the decision hold across its arc? | `Held` \| `ViolatedAt` \| `CannotProve` |
| `get_arc` | Arc of episodes implementing the decision | DeterministicallyDerived \| CannotProve |

> **Both take a `decision_id` (uint64), not free text.** Get it from `recall_decision`
> (see `memtrace-decision-recall`) or from `why_is_this_here` (see `memtrace-provenance`).
> Do not invent ids.

> **Honesty contract:** verdicts are deterministically defended. `CannotProve` means the
> decision is invisible or has no implementing episode — *unknown*, not "fine."

## Steps

### 1. Get the decision_id

Run `recall_decision(...)` and take a `decision_id` from a hit, or get one from
`why_is_this_here(symbol_id)`. These tools do not accept names.

### 2. Verify it held

`verify_intent(decision_id)`:

| Verdict | Meaning | Action |
|---|---|---|
| **Held** | The decision held across its arc | Safe to rely on; honor it |
| **ViolatedAt** | The code drifted from the decision at a point | Surface the drift — it's either a bug or an unrecorded reversal; ask which |
| **CannotProve** | Decision invisible or no implementing episode | Treat as unknown; don't claim it held |

### 3. Inspect what implemented it

`get_arc(decision_id)` → the episodes reachable by Produced/DerivedFrom edges. This is
the implementation trail: where the decision actually landed (or where it should have).

### 4. Cross to the code graph for the "what"

The arc names episodes/symbols; to see the current code or its blast radius, hand those
over to `memtrace-first` / `memtrace-impact`. Decision memory proves *whether it held*;
the code graph shows *the current state*.

## Decision Points

| Situation | Action |
|-----------|--------|
| Relying on a recalled decision for a choice | `verify_intent` first — only trust a `Held` verdict |
| Auditing whether the codebase follows its own decisions | `verify_intent` per decision; report every `ViolatedAt` |
| "Where/when was decision D implemented?" | `get_arc(decision_id)` |
| `verify_intent` returns `ViolatedAt` | Don't silently 'fix' it — it may be a deliberate (unrecorded) reversal; confirm with the user |
| `verify_intent` returns `CannotProve` | Treat as unknown; fall back to `recall_decision` + the code graph |

## Output

| Call | Returns |
|------|---------|
| `verify_intent(decision_id)` | Verdict: `Held` \| `ViolatedAt` (the point where code drifted from the decision) \| `CannotProve` (decision invisible or no implementing episode — unknown, not "fine") |
| `get_arc(decision_id)` | FactStatus `DeterministicallyDerived` \| `CannotProve`, plus the implementing arc: the episodes reachable by Produced/DerivedFrom edges |
