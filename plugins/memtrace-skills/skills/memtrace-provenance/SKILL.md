---
name: memtrace-provenance
description: "Retrieve the governing decision lineage (why is this here) and the contracts that bind a symbol, from Cortex decision memory. Use before deleting, rewriting, or 'cleaning up' code that looks unused, odd, or redundant, and when the user asks why a symbol exists or what rules constrain it. Symbol-scoped; for free-text decision search use memtrace-decision-recall. Do not infer intent from the diff or assume unfamiliar code is safe to remove."
allowed-tools:
  - mcp__memtrace__why_is_this_here
  - mcp__memtrace__governing_contracts
  - mcp__memtrace__verify_intent
  - mcp__memtrace__recall_decision
metadata:
  author: "Syncable <support@syncable.dev>"
  version: "1.0.0"
  category: development
---

## Overview

Provenance answers **"why is this code here, and what binds it?"** before you change
it. `why_is_this_here` returns the decision/conversation lineage that governs a symbol
(its Governs/Produced provenance); `governing_contracts` returns the contract nodes
that constrain it. Together they stop you from deleting code that closes a past issue
or violating an invariant nothing in the AST advertises.

Full parameter spec for every Memtrace tool: [references/mcp-parameters.md](../../references/mcp-parameters.md).

## Quick Reference

| Tool | Purpose | Returns / FactStatus |
|------|---------|----------------------|
| `why_is_this_here` | The governing decision/conversation lineage of a symbol | DeterministicallyDerived \| CannotProve |
| `governing_contracts` | The contract nodes that constrain a symbol | Contract nodes \| CannotProve |
| `verify_intent` | Given a returned governing `decision_id` — is that rationale still valid? | Held \| ViolatedAt \| CannotProve |

> **`why_is_this_here` and `governing_contracts` each take a `symbol_id` (uint64 node
> id), not a name. `verify_intent` takes a `decision_id` (uint64), not a `symbol_id`.**
> If you only have a name or a question, start with `recall_decision` (free text) — see
> `memtrace-decision-recall`. ids come from a prior recall/arc result or the Cortex
> view. Do not invent ids.

> **Honesty contract:** for a symbol no decision produced, `why_is_this_here` returns an
> honest **CannotProve**; `governing_contracts` returns CannotProve rather than a false
> "no contracts" verdict. **CannotProve ≠ safe to delete / unconstrained** — it means
> *unknown*, so fall back to blast-radius and the user.

## Steps

### 1. Get the symbol_id

If you have a name or a "why" question, run `recall_decision(...)` first and take the
`symbol_id` from the result (or the Cortex view). The id-based tools need a numeric id.

### 2. Ask why it's here

`why_is_this_here(symbol_id)` → the governing decision lineage. This is the *rationale*
the git blame won't give you: the decision that produced this code and, often, the
problem it was put there to solve.

### 3. Ask what constrains it

`governing_contracts(symbol_id)` → contracts that must survive any rewrite (invariants,
interface promises, "must always …" rules). A refactor that breaks one of these is a
regression even if every test passes.

### 4. Check the rationale still holds

If `why_is_this_here` returns a governing `decision_id`, run `verify_intent(decision_id)`
(see `memtrace-intent-verification`) — a `ViolatedAt` verdict (or a decision you can see
was superseded via `recall_decision`) changes whether the code should still look this way.

## Decision Points

| Situation | Action |
|-----------|--------|
| Code looks unused/dead and you want to delete it | `why_is_this_here` + `governing_contracts` BEFORE deleting; CannotProve is not a green light |
| Code is written in a strange/non-obvious way | `why_is_this_here` — there's likely a decision explaining the oddity |
| About to refactor a symbol's internals | `governing_contracts` — preserve every constraint, not just the test surface |
| User asks "why is this here?" | `why_is_this_here(symbol_id)`; if you only have a name, `recall_decision` first |
| All provenance returns CannotProve | Treat as unknown; combine with `memtrace-impact` (blast radius) and ask the user before removing |

## Output

```text
why_is_this_here(symbol_id: 4821)
→ DeterministicallyDerived — governing lineage:
   decision_id 913 (Governs): "Keep retry shim until legacy clients migrate"
governing_contracts(symbol_id: 4821)
→ contract node: "writes must always be debounced" (must survive any rewrite)
— no decision produced the symbol → CannotProve (unknown, NOT unconstrained)
```

`verify_intent(decision_id)` on a returned lineage id yields Held | ViolatedAt | CannotProve.
