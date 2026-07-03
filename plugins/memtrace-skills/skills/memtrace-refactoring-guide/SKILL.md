---
name: memtrace-refactoring-guide
description: "Build a phased, risk-scored refactoring plan from Memtrace complexity, dead-code, bridge, and impact analysis. Use when the user wants to refactor source code, reduce complexity, clean technical debt, split large functions, extract modules, reorganize code, or choose refactoring priorities. Do not use Grep or manual reference search to plan refactors; Memtrace provides complexity, dead-code, relationships, and impact context."
allowed-tools:
  - mcp__memtrace__find_most_complex_functions
  - mcp__memtrace__find_dead_code
  - mcp__memtrace__find_bridge_symbols
  - mcp__memtrace__get_evolution
  - mcp__memtrace__get_impact
  - mcp__memtrace__get_symbol_context
  - mcp__memtrace__analyze_relationships
  - mcp__memtrace__list_communities
metadata:
  author: "Syncable <support@syncable.dev>"
  version: "1.0.0"
  category: development
---

## Overview

Guided refactoring workflow — identifies refactoring candidates using structural analysis, scores them by risk and priority, and produces a phased refactoring plan. Combines complexity metrics, dead code detection, bridge analysis, and temporal evolution to prioritize what to refactor first and how to do it safely.

## Steps

### 1. Identify refactoring candidates

Run these three tools in parallel to build a candidate list:

**a) Complexity hotspots:**
Call `find_most_complex_functions` with `top_n: 20`

**b) Dead code:**
Call `find_dead_code` to find unused symbols

**c) Architectural bottlenecks:**
Call `find_bridge_symbols` to find chokepoints with too much responsibility

### 2. Score candidates by volatility

Call `get_evolution` with `from: "90d ago"` and `mode: "compound"`.

Review `top_touched_symbols` and `top_changed_files`:
- Symbols that are BOTH complex AND frequently changing are the highest priority
- Complex but stable code can wait — it's not causing active pain
- Volatile but simple code may be fine — frequent changes to simple code is normal

**Priority matrix:**

| | Low Complexity | High Complexity |
|---|---|---|
| **Stable (low change freq)** | Leave alone | Monitor; refactor if touched |
| **Volatile (high change freq)** | Normal; leave alone | **TOP PRIORITY** — refactor first |

### 3. Assess risk for top candidates

For each top-priority candidate, call `get_impact` with `direction: both`:
- **Low risk** → refactor directly
- **Medium risk** → refactor with comprehensive tests
- **High/Critical risk** → plan incremental migration with backward compatibility

Also call `get_symbol_context` to check:
- How many processes does this symbol participate in? (More = more testing needed)
- Is it part of a cross-repo API? (If yes, coordinate with consumers)

### 4. Understand the neighbourhood

For each refactoring target, call `analyze_relationships`:
- `find_callees` — what does it depend on? These become candidates for extraction
- `find_callers` — what depends on it? These need updating after refactoring
- `class_hierarchy` — is it part of an inheritance chain? Liskov concerns

Full parameter spec for every Memtrace tool: [references/mcp-parameters.md](../../references/mcp-parameters.md).

### 5. Check community boundaries

Call `list_communities` and check: does the refactoring target sit at a community boundary?
- If yes, the refactoring may involve splitting responsibilities across modules
- If it belongs clearly to one community, the refactoring is more contained

### 6. Produce the refactoring plan

Synthesize into a phased plan:

**Phase 1 — Quick Wins:**
- Dead code removal (zero-risk deletions)
- Simple functions with high churn (reduce volatility)

**Phase 2 — High-Impact Refactors:**
- Complex + volatile functions (highest priority by the matrix)
- Bridge symbols with too many responsibilities (extract interfaces)

**Phase 3 — Structural Improvements:**
- Splitting oversized communities into smaller, focused modules
- Extracting shared logic from bridge symbols into dedicated services

For each item, include:
1. **Target** — function/class name, file, current complexity score
2. **Why** — complexity + volatility + blast radius rationale
3. **How** — specific refactoring approach (extract method, split class, introduce interface)
4. **Risk** — impact analysis rating + affected processes
5. **Test Plan** — which callers/processes to verify

## Decision Points

| Condition | Action |
|-----------|--------|
| Complex + volatile + high blast radius | Highest priority — but plan carefully; incremental approach |
| Complex + stable + low blast radius | Can wait; refactor when you're already touching nearby code |
| Dead code with zero callers | Safe to delete — quick win |
| Bridge symbol with many dependents | Extract interface first, then refactor implementation behind it |
| Symbol in cross-repo API | Coordinate with consumers; backward-compatible changes only |

## Output

A phased plan (Phases 1–3). One worked entry:

| Field | Example |
|---|---|
| Target | `process_payment` — `src/billing/processor.py`, complexity 38 |
| Why | Complex + volatile (14 changes/90d in `top_touched_symbols`) + 23-symbol blast radius |
| First move | Extract validation branch to `validate_payment_request`; keep callers untouched |
| Risk | High — upstream spans 3 processes incl. `checkout_flow`; incremental migration |

Acceptance criteria:
- Every plan item cites complexity, volatility, and blast radius — no gut-feel picks.
- High/Critical-risk items include a test plan naming affected callers/processes.

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| Refactoring the most complex function first | Complexity alone isn't enough — prioritize by complexity × volatility |
| Deleting all dead code at once | Some "dead" code is called dynamically; verify before batch deletion |
| Refactoring without checking blast radius | A "simple" refactor on a bridge symbol can cascade across the codebase |
| Not checking temporal evolution | A complex function that hasn't changed in a year is lower priority than a simpler one that changes weekly |
