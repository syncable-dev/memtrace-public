---
name: memtrace-change-impact-analysis
description: "Compute what a planned source-code change will break — blast radius, affected processes, cross-repo callers — and produce a risk-rated change plan. Use when the user needs to know what will break before a change: edits, refactors, API changes, renames, removals, PR reviews, or risk assessments. Do not manually grep references or browse files for impact; this workflow uses Memtrace graph context, impact, and change history. For a quick blast-radius check on one symbol, use memtrace-impact."
allowed-tools:
  - mcp__memtrace__find_symbol
  - mcp__memtrace__find_code
  - mcp__memtrace__get_symbol_context
  - mcp__memtrace__get_impact
  - mcp__memtrace__get_evolution
  - mcp__memtrace__get_timeline
  - mcp__memtrace__analyze_relationships
metadata:
  author: "Syncable <support@syncable.dev>"
  version: "1.0.0"
  category: development
---

## Overview

Pre-change risk assessment workflow. Before modifying code, this workflow maps the full blast radius, identifies affected processes, checks recent change history for instability signals, and produces a risk-rated change plan.

## Steps

### 1. Identify what's being changed

Find the target symbol(s):
- Use `find_symbol` if the user named specific functions/classes
- Use `find_code` if the user described behaviour ("the authentication middleware")

Collect symbol **names** (`name`, `scope_path`, `file_path`) for all targets — graph tools use names, not internal IDs.

### 2. Get 360° context for each target

For each symbol, call `get_symbol_context` (`repo_id`, `symbol`, `file_path`):
- Direct callers and callees
- Community membership (which module is this in?)
- Process membership (which execution flows does this participate in?)
- Cross-repo API callers (is this an endpoint called by other services?)

**Decision:** If cross-repo API callers exist, this change requires coordination with other teams. Flag this immediately.

### 3. Compute blast radius

For each target, call `get_impact` (`repo_id`, `target`) with `direction: "both"`, `depth: 5`:
- Upstream impact: what depends on this symbol
- Downstream impact: what this symbol depends on
- Risk rating: Low / Medium / High / Critical

**Decision:**
| Risk | Action |
|------|--------|
| Low | Proceed with standard testing |
| Medium | Review all direct callers; test affected processes |
| High | Plan incremental migration; consider feature flags |
| Critical | Full migration strategy; backward-compatible changes required |

### 4. Check temporal stability

Call `get_evolution` (`repo_id`, `from: "30d ago"`, `mode: "compound"`), then `get_timeline` on each target symbol (requires `scope_path` + `file_path` from `find_symbol`):
- Sparse timeline history + you're about to change it → structurally surprising; extra scrutiny warranted.
- Target appears in `top_touched_symbols` → high churn + high impact = volatile hotspot.

### 5. Map affected execution flows

From step 2, you already know which processes are affected. For critical changes, use `analyze_relationships` (`repo_id`, `target`) with `query_type: "find_callers"` at `depth: 3` to trace the full transitive caller chain.

`depth: 3` is a JSON number, not a string — the validator rejects `"3"`.
Full parameter spec for every Memtrace tool: [references/mcp-parameters.md](../../references/mcp-parameters.md).

### 6. Produce the risk assessment

Synthesize into a change plan:

1. **Target(s)** — what's being changed and where
2. **Blast Radius** — number of direct/transitive dependents, risk rating
3. **Affected Processes** — which execution flows will be impacted
4. **Cross-Service Impact** — any external callers or consumers
5. **Stability Signal** — sparse `get_timeline` history (stable) vs frequent appearance in `top_touched_symbols` (volatile)
6. **Recommended Approach** — based on risk: direct change, incremental migration, or backward-compatible evolution
7. **Test Coverage** — which callers/processes to verify after the change

## Decision Points

| Condition | Action |
|-----------|--------|
| Risk = Critical | Recommend backward-compatible change + deprecation path |
| Cross-repo callers exist | Flag as requiring multi-service coordination |
| Symbol has sparse timeline but high impact | Extra review — this rarely changes; make sure the change is intentional |
| Multiple processes affected | List each affected flow; recommend testing each one |
| Symbol is a bridge point | Change may disconnect parts of the architecture — verify alternative paths exist |

## Output

The workflow's artifact is the step-6 risk-rated change plan. Compact example:

```text
Target: AuthService::validateToken (src/auth/service.ts)
Blast Radius: 4 direct / 23 transitive dependents — Risk: High
Affected Processes: login-flow, session-refresh
Cross-Service Impact: 2 cross-repo API callers — needs multi-service coordination
Stability Signal: appears in top_touched_symbols (volatile hotspot)
Recommended Approach: incremental migration behind a feature flag
Test Coverage: all 4 direct callers + both affected processes
```
