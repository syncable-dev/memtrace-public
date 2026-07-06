---
name: memtrace-decision-recall
description: "Recall ranked decisions, bans, and conventions from Cortex decision memory by free-text query. Use when the user asks what was decided, what was chosen or rejected, whether there's a convention/ban/policy on something, or before re-picking a library, pattern, or approach that may already be settled. Do not reconstruct past decisions from git log or guesswork. To verify whether a known decision held, use memtrace-intent-verification; for symbol-scoped decision lineage/contracts, use memtrace-provenance."
---

## Overview

`recall_decision` is the **free-text entry point** to decision memory. Given a query,
it returns the statistically-ranked set of decisions/conversations that bear on it —
including **bans** ("never use X", "don't do Y"), which are recorded as decisions.
Use it before re-litigating a settled choice or contradicting a convention.

This is the one decision-memory tool that takes plain text. The ranked decisions it
returns carry the `decision_id`s the other tools (`verify_intent`, `get_arc`) need.

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

## Quick Reference

| Tool | Purpose |
|------|---------|
| `recall_decision` | Ranked decisions/bans for a free-text query (the entry point) |
| `verify_intent` | Given a returned `decision_id` — did it still hold? (see `memtrace-intent-verification`) |
| `get_arc` | Given a returned `decision_id` — what implemented it? (see `memtrace-intent-verification`) |

> **Honesty contract:** an empty or unknown query returns an explicit **CannotProve**,
> never a fabricated decision. CannotProve means "no recorded decision on this" — it is
> *unknown*, not *approved*. Don't treat it as a green light.

## Steps

### 1. Query in the user's own terms

`recall_decision(query)` — `query` is free text. Use the noun phrase of the thing in
question: a library (`"redis vs in-memory cache"`), a pattern (`"error handling
strategy"`), a subsystem (`"auth tokens"`), or the exact thing you're about to do.

### 2. Read the ranked result

Results come back ranked (lexical + semantic lanes, RRF-fused) with an `id`, a
`kind`, and a score per hit. **`decision`-kind hits are ranked first**; lower-ranked
`conversation` hits are supporting context (the verbatim turns around the decision).
For anything you mean to chain (step 4), use a hit whose `kind` is `decision` — its
`id` is the `decision_id` the other tools require.

### 3. Act on what's there

| Result | Action |
|---|---|
| A matching decision | Honor it. Quote it back to the user before doing anything that contradicts it. |
| A **ban** ("never/don't …") | Do **not** reintroduce the banned approach without explicit user sign-off. |
| Several competing/old hits | Run `verify_intent(decision_id)` on the top one to see if it still holds before relying on it. |
| **CannotProve** | No recorded decision. Don't invent one — fall back to `memtrace-first` and/or ask the user. |

### 4. Chain into the id-based tools when you need more

The `id` of a `kind: "decision"` hit feeds `verify_intent` (did it hold?) and
`get_arc` (what implemented it?). Don't pass a `conversation` hit's id — those tools
require a Decision node and will honestly return CannotProve. See
`memtrace-intent-verification`.

## Decision Points

| Situation | Action |
|-----------|--------|
| About to choose a library/pattern/approach | `recall_decision` FIRST — you may be undoing a deliberate choice or ban |
| User asks "did we decide X?" / "what's our convention on Y?" | `recall_decision("X" / "Y")` |
| You suspect a "don't do this" rule exists | `recall_decision` — bans are decisions and will surface |
| Recall returns a decision you're about to contradict | Surface it to the user verbatim; don't silently override |
| Recall returns CannotProve | Treat as unknown, not approval; do not fabricate a rationale |

## Output

`recall_decision` returns ranked hits — `kind: "decision"` first, `conversation` hits below as supporting context — or an explicit **CannotProve**:

```json
[
  { "id": "…", "kind": "decision", "score": 0.91 },
  { "id": "…", "kind": "conversation", "score": 0.44 }
]
```

Only a `decision`-kind hit's `id` is a `decision_id` usable with `verify_intent` / `get_arc`. An empty or unknown query returns CannotProve, never fabricated hits.
