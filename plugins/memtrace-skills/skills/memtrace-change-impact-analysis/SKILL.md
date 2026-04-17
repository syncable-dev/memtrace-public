---
name: memtrace-change-impact-analysis
description: "Multi-tool pre-change risk workflow — chains find_symbol → get_symbol_context → get_impact → get_evolution → cochange to produce a written risk-rated change plan. USE when the user is about to refactor / rename / delete a symbol and wants a FULL risk write-up before they touch anything, or when reviewing a PR touching a critical symbol. DO NOT USE for a quick one-shot blast-radius number (→ memtrace-impact), for exploratory 'what does this code do' (→ memtrace-codebase-exploration / memtrace-relationships), or for after-the-fact incident investigation (→ memtrace-incident-investigation)."
---

> **Tool argument schemas:** this skill calls several MCP tools. For the authoritative name / type / required / default / constraint of every argument, see `references/mcp-parameters.md`. Pass JSON numbers as numbers, booleans as booleans — the MCP validator does not coerce.

## Overview

Pre-change risk assessment workflow. Before modifying code, this workflow maps the full blast radius, identifies affected processes, checks recent change history for instability signals, and produces a risk-rated change plan.

## Steps

### 1. Identify what's being changed

Find the target symbol(s):
- Use `find_symbol` if the user named specific functions/classes
- Use `find_code` if the user described behaviour ("the authentication middleware")

Collect symbol IDs for all targets.

### 2. Get 360° context for each target

For each symbol, call `get_symbol_context`:
- Direct callers and callees
- Community membership (which module is this in?)
- Process membership (which execution flows does this participate in?)
- Cross-repo API callers (is this an endpoint called by other services?)

**Decision:** If cross-repo API callers exist, this change requires coordination with other teams. Flag this immediately.

### 3. Compute blast radius

For each target, call `get_impact` with `direction: both`:
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

Call `get_evolution` with mode `novel` for a 30-day window on the repo:
- Are any of the target symbols flagged as "rarely changing"? If so, this change is structurally surprising and deserves extra scrutiny.
- Have the target symbols been changing frequently? High churn + high impact = volatile hotspot.

### 5. Map affected execution flows

From step 2, you already know which processes are affected. For critical changes, use `analyze_relationships` with `query_type: find_callers` at `depth: 3` to trace the full transitive caller chain.

### 6. Produce the risk assessment

Synthesize into a change plan:

1. **Target(s)** — what's being changed and where
2. **Blast Radius** — number of direct/transitive dependents, risk rating
3. **Affected Processes** — which execution flows will be impacted
4. **Cross-Service Impact** — any external callers or consumers
5. **Stability Signal** — is this code stable (novel) or volatile (frequent changes)?
6. **Recommended Approach** — based on risk: direct change, incremental migration, or backward-compatible evolution
7. **Test Coverage** — which callers/processes to verify after the change

## Decision Points

| Condition | Action |
|-----------|--------|
| Risk = Critical | Recommend backward-compatible change + deprecation path |
| Cross-repo callers exist | Flag as requiring multi-service coordination |
| Symbol has high novelty score | Extra review — this rarely changes; make sure the change is intentional |
| Multiple processes affected | List each affected flow; recommend testing each one |
| Symbol is a bridge point | Change may disconnect parts of the architecture — verify alternative paths exist |
