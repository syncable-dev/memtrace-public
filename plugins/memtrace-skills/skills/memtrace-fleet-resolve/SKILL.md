---
name: memtrace-fleet-resolve
description: "Resolve a Class C fleet decision: submit your verdict as an agent judge (fleet_submit_verdict), poll your own directive (fleet_get_escalation), see the needs-human queue (fleet_list_escalations), or record a human decision (fleet_resolve_escalation). Use when the user says 'a decision is waiting' or 'who should proceed', when you are handed a mediation_request, when acting as a mediator between two agents, or when a human chooses a winner in the dashboard. For the underlying conflict-class model and decision loop, see memtrace-fleet-coordination."
---

## Overview

The tools that resolve a Class C conflict. The judging is done by the user's own
agents (no API keys): an agent reads the bundle and submits a verdict; the daemon
is the deterministic referee that decides the outcome and routes it back.

## Submit a verdict (you are the judge)

You were handed a `mediation_request` (from `fleet_record_episode`). Read **every
agent's `assignment`** in it and decide on merit — including against your own
change:

```jsonc
fleet_submit_verdict({
  escalation_id: "01J…",
  agent_id: "agent-a",
  verdict: { "kind": "recommend", "winner": "agent-b",
             "rationale": "wider contract; rebase the fix onto it", "confidence": 0.8 }
})
// kinds: reconcile {merge_plan} | recommend {winner, rationale, confidence} | defer_to_human {question}
```

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

The response tells you the `outcome` (`auto_apply` | `human_confirm` |
`human_required` | `pending`) and `your_directive`.

## Read your own directive (you are blocked)

```jsonc
fleet_get_escalation({ escalation_id: "01J…", agent_id: "agent-a" })
// → your_directive: wait | proceed | defer | review
```

Poll until it's not `wait`. `proceed` = continue; `defer` = stand down and rebase
onto the winner; `review` = read `resolution`.

## Human paths

- `fleet_list_escalations({repo_id})` — the per-repo "needs human" queue.
- `fleet_resolve_escalation({escalation_id, resolution, winner})` — record a human
  decision (pick which agent proceeds) and clear it. Prefer the agent-judge path;
  use this for genuine human/product calls.

Every verdict and resolution persists to the escalation record as the audit
trail — review any decision later via `fleet_get_escalation` /
`fleet_list_escalations`.

## Safety the referee guarantees

- A destructive **removal** (delete/move) is **never** auto-applied — always a human.
- Auto-apply happens only for the clear-safe machine case or ≥2 agents agreeing on
  a non-destructive resolution.
- So a wrong verdict degrades to "a human reviews a suggestion," never a silent
  bad merge.

## Output

`fleet_submit_verdict` and `fleet_get_escalation` both return the referee's decision:

```jsonc
{
  "outcome": "human_confirm",  // auto_apply | human_confirm | human_required | pending
  "your_directive": "wait",    // wait | proceed | defer | review
  "resolution": "agent-b proceeds; agent-a rebases onto the wider contract"
                               // set once the escalation is resolved (directive = review)
}
```
