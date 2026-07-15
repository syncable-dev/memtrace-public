---
name: memtrace-decision-memory
description: "Use Cortex decision memory through the normal Memtrace MCP tools. Trigger for free-text questions about what was decided, chosen, rejected, banned, or established as a convention; for why a symbol exists or which contracts constrain it; for whether a known decision held, drifted, or was violated; and for the implementation arc behind a decision. Use before non-trivial edits, refactors, deletions, or re-picking a library, pattern, architecture, or subsystem behavior. Routes internally across recall_decision, why_is_this_here, governing_contracts, verify_intent, and get_arc. Do not guess rationale from a diff or git log."
---

# Decision Memory First

## The Iron Law

```
BEFORE you assume why code exists, contradict a convention, re-pick a settled
choice, or delete code that "looks unused/weird" → CHECK DECISION MEMORY.

recall_decision(free-text)  →  what did we decide / ban about X?
why_is_this_here(symbol_id) →  what decision put this here?
verify_intent(decision_id)  →  did that decision still hold, or was it violated?
get_arc(decision_id)        →  what episodes implemented it?
governing_contracts(sym_id) →  what constraints bind this symbol?
```

This is the **rationale layer** of the codebase. `memtrace-first` answers *what the
code is and how it's wired* (symbols, calls, git, blast radius). Decision memory
answers *why it is the way it is, what we already decided, and whether that decision
still holds* — extracted from real coding conversations and decisions, not the AST.

The graph can tell you a function exists and who calls it. Only decision memory can
tell you that three weeks ago you **banned** the approach you're about to reintroduce.

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

## The honesty contract — read this first

These five tools are **deterministic, zero-LLM**. Every call returns a labeled
**Verdict / Evidence / CannotProve** answer carrying its FactStatus and proof path.
**No tool ever fabricates an answer.**

| Answer | What it means | What you must NOT do |
|---|---|---|
| **Verdict + Evidence** (Observed / DeterministicallyDerived / StatisticallyRanked) | A recorded decision/provenance backs this | — |
| **CannotProve** | No recorded decision governs this | Do **not** read it as "safe / approved / unconstrained." It means *unknown*, not *permitted*. Fall back to `memtrace-first` + asking the user. |

`CannotProve` is a real, trustworthy answer ("memory has nothing on this"), not a
failure and not a green light. Never invent a rationale to fill the gap.

## Tool availability (once per session)

These tools are exposed on the normal **`memtrace` MCP server**:
`recall_decision`, `why_is_this_here`, `verify_intent`, `get_arc`, and
`governing_contracts`. Hosts do not need a second MemCortex MCP connection. If a
tool call returns CannotProve because Cortex is unavailable (for example native
Windows without WSL2), say decision memory was unavailable/unknown and continue
with `memtrace-first`; do not fabricate decisions.

## The decision rule

| What you're about to do / be asked | Right tool | Procedure |
|---|---|---|
| "Did we already decide/choose/reject X?" "What's our convention on Y?" | `recall_decision("X")` | Free-text recall |
| "Is there a ban / a 'don't do this' on Z?" | `recall_decision("Z")` — bans surface as decisions | Free-text recall |
| About to edit behavior, re-pick a library/pattern/architecture, or change a subsystem policy | `recall_decision` FIRST — don't re-litigate a settled call | Free-text recall |
| "Why is this code here?" "Why is it done this odd way?" | `why_is_this_here(symbol_id)` | Symbol provenance |
| About to delete/refactor/clean up existing code, especially odd or "dead" code | `why_is_this_here` + `governing_contracts` before touching it | Symbol provenance |
| "What rules/contracts constrain this symbol?" | `governing_contracts(symbol_id)` | Symbol contracts |
| "Did decision D actually hold, or did we drift?" | `verify_intent(decision_id)` | Intent verification |
| "What commits/episodes implemented decision D?" | `get_arc(decision_id)` | Implementation arc |

## How the tools chain (ids come from recall, not from names)

Only `recall_decision` takes free text. The other four take **numeric node ids**
(`decision_id` / `symbol_id`, uint64). The normal flow is:

```
recall_decision("auth strategy")
        │  returns ranked hits, decisions first: [{ id, kind: "decision", ... }]
        │  (pick a kind:"decision" hit; conversation hits are context, not chainable)
        ├─► verify_intent(decision_id)   did it hold?
        └─► get_arc(decision_id)         what implemented it?

why_is_this_here(symbol_id)
        │  returns the governing decision lineage for a symbol
        └─► verify_intent(that decision_id)   is that rationale still valid?
```

`symbol_id` comes from a prior recall/arc result or the Cortex view — **if you only
have a name or a free-text question, start with `recall_decision`.** Do not invent ids.

## Standard workflows

### "Why does this code exist / can I delete it?"
1. If you only have a name/free-text target, `recall_decision("<symbol/subsystem>")` first.
2. If you have a `symbol_id`, `why_is_this_here(symbol_id)` → the governing decision, if any.
3. `governing_contracts(symbol_id)` → constraints that must survive a rewrite.
4. If a decision governs it → `verify_intent(decision_id)` to see if it still holds.
5. **CannotProve on all checks ≠ safe to delete** — confirm with `memtrace-impact` (blast radius) and the user.

### "Should I do X?" (about to make a choice)
1. `recall_decision("X")` → did we already decide or ban this?
2. If a prior decision exists → `verify_intent(decision_id)` → is it still in force?
3. Honor a held decision; only revisit a `ViolatedAt`/superseded one — and say so explicitly

### "Did we follow through on decision D?"
1. `verify_intent(decision_id)` → Held | ViolatedAt | CannotProve
2. `get_arc(decision_id)` → the episodes that implemented (or should have) it

## Red flags — STOP, check decision memory

| Thought | Reality |
|---|---|
| "This code looks unused, I'll delete it" | `why_is_this_here` first — a decision may govern it; deletion may reopen a closed issue |
| "I'll just edit/refactor this existing behavior" | `recall_decision("<behavior/subsystem>")` first — the change may violate a recorded decision or ban |
| "I'll just use library/pattern X" | `recall_decision("X")` — you may be undoing a deliberate ban |
| "The diff/git log will tell me why" | Git shows *what changed*, not *what was decided or rejected*. Decision memory has the rationale and the bans. |
| "CannotProve, so it's fine/approved" | CannotProve = unknown, not approved. Don't treat absence of a record as permission. |
| "I'll guess the rationale from the code" | Don't fabricate a why. Return what the tool proves, or say it's unknown. |

## Skill priority

This is a **process skill** — it runs alongside `memtrace-first` before
implementation. Use `memtrace-first` for *what/where/impact* (code graph); use
decision memory for *why/whether-it-held* (rationale). When both apply, decision
memory gates the intent, the code graph gates the mechanics.

## Output

The routing outcome: which sibling skill/tool to invoke, and the evidence to quote.

```
Ask:    "Should I switch to library X?"
Route:  recall_decision("library X")   →  free-text recall
Hit:    { id: 4217, kind: "decision", ... }   (a ban exists)
Next:   verify_intent(4217)            →  intent verification
Quote:  Verdict + Evidence (FactStatus, proof path) — or CannotProve = unknown, not permission
```
