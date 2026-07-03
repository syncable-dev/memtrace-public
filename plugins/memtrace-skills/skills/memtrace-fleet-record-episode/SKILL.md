---
name: memtrace-fleet-record-episode
description: "Record an edit you just made in a fleet and get its conflict class (A/B/C) against agents on your branch. Use when you have just finished an edit, when the user says 'I just changed X', or when completing a refactor step while other agents share your repo+branch. Returns conflict_class + replan_hint; a Class C returns an escalation_id and mediation_request that starts the decision loop. Do not finish a coordinated edit without recording it."
allowed-tools:
  - mcp__memtrace__fleet_record_episode
  - mcp__memtrace__fleet_get_escalation
  - mcp__memtrace__fleet_query_episodes
  - mcp__memtrace__fleet_submit_verdict
metadata:
  author: "Syncable <support@syncable.dev>"
  version: "1.0.0"
  category: development
---

## Overview

`fleet_record_episode` is step 3 of the fleet protocol: record the edit you just
made and learn whether it collided with another agent on your branch.

## Call it

```jsonc
fleet_record_episode({
  repo_id: "myrepo",
  branch:  "session/auth-revamp",
  agent_id:"agent-a",
  touched: ["auth::verify_token"],
  intent:  {"refactor": {"pattern": "change_signature"}}
})
```

Full parameter spec for every Memtrace tool: [references/mcp-parameters.md](../../references/mcp-parameters.md).

## The result: conflict_class

- **A → proceed.** Additive, order-independent.
- **B → re-read, then proceed.** Non-destructive overlap; re-read the shared
  symbols so you build on current state.
- **C → a decision is needed.** A destructive change overlaps another agent's
  work. The response includes:
  - `escalation_id` — the decision's id.
  - `mediation_request` — the judging task (every agent's `assignment` + the
    contested symbols). If you're asked to judge, call `fleet_submit_verdict`.
  - `next_action` — poll `fleet_get_escalation({escalation_id, agent_id})` until
    `your_directive` ≠ `wait`, then `proceed` / `defer` / `review`.

Class is computed **only against agents on your branch**. Agents on other branches
never make your edit a Class C.

## After recording

- Class A/B → continue.
- Class C → enter the decision loop (see `memtrace-fleet-coordination`). Don't keep
  editing the contested symbols until your directive is `proceed`.
- Every recorded episode is logged and persists as the fleet's reviewable audit
  trail. Review history with `fleet_query_episodes({repo_id, node?, conflict_class?})`.

## Output

```jsonc
{
  "conflict_class": "C",        // "A" | "B" | "C"
  "replan_hint": "re-read auth::verify_token before continuing",
  // Class C only — absent for A/B:
  "escalation_id": "esc-01J9...",
  "mediation_request": { /* each agent's assignment + contested symbols */ },
  "next_action": "poll fleet_get_escalation until your_directive != wait"
}
```
