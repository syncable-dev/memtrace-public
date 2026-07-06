---
name: memtrace-fleet-coordination
description: "Resolve fleet conflicts between coordinated agents: what conflict class A/B/C means, how a Class C destructive overlap gets decided (by an agent judge or a human), how to be the judge (fleet_submit_verdict), how to read your directive after a decision, and how branch-scoping isolates fleets. Use when the user says 'two agents are changing the same thing', 'resolve this conflict', 'who should proceed', 'a decision is waiting', or asks you to act as a mediator between agents. This skill explains the conflict model and decision loop; for just the verdict/directive/resolution tool calls on a specific escalation, use memtrace-fleet-resolve."
---

# Fleet Coordination

How the fleet turns overlapping edits into a safe decision. Read
`memtrace-fleet-first` for the publish→edit→record protocol; this skill is the
*conflict resolution* half.

## Conflict classes (what `fleet_record_episode` returns)

| Class | Meaning | What to do |
|---|---|---|
| **A** | Additive, order-independent | Proceed. |
| **B** | Non-destructive overlap with another agent's work | Re-read the shared symbols, then proceed. |
| **C** | A **destructive** change (signature change / move / dead-code removal) overlaps another agent's work | A decision is needed — it does NOT auto-resolve. |

Class is computed **only against agents on your branch** (`(repo, branch)`).
Agents on other branches are never conflict peers.

## The Class C decision loop

When `fleet_record_episode` returns class C it also returns an `escalation_id` and
(when mediation is on) a `mediation_request`. The decision is made by an **agent
judge**, a **human**, or — only when provably safe — the **deterministic referee**.

### Being the judge (the user's own agent does the judging — no API keys)

The `mediation_request` bundles **every agent's `assignment`** plus the contested
symbols. Read the *other* agent's task and decide on merit (including against your
own change). Submit:

```jsonc
fleet_submit_verdict({
  escalation_id: "01J…",
  agent_id: "agent-a",
  verdict: { "kind": "recommend", "winner": "agent-b",
             "rationale": "the signature change is the wider contract; rebase the fix onto it",
             "confidence": 0.8 }
})
```

Verdict kinds: `reconcile {merge_plan}` · `recommend {winner, rationale, confidence}`
· `defer_to_human {question}`.

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

The referee then decides the outcome:
- **Auto-apply** only when safe: the clear machine case, or ≥2 independent agents
  agree — and **never** for a destructive *removal* (delete/move), which always
  needs a human.
- **Human confirm** — a recommendation is surfaced for one-click confirmation.
- **Human required** — both sides destructive, agents disagree, or an agent
  deferred → a person decides.

### Reading your directive

Poll `fleet_get_escalation({escalation_id, agent_id})` until `your_directive` ≠
`wait`: `proceed` (you continue), `defer` (stand down / rebase onto the winner),
`review` (read `resolution`).

### Resolving as a human (or on a human's behalf via the dashboard)

`fleet_resolve_escalation({escalation_id, resolution, winner})` records a human
decision and clears the queue. Prefer the agent-judge path; use this for the
genuine human-decision cases. `fleet_list_escalations({repo_id})` shows the
"needs human" queue.

## Why branch-scoping matters here

A "winner / defer" only makes sense on a shared surface. Across branches, the
loser can't defer (its branch needs the change too), so the fleet never escalates
cross-branch — that's a merge-time concern git already owns. Keep your fleet to
one session branch and conflicts stay real and resolvable.

## Inspecting state

`fleet_get_node_state({repo_id, node})` — recent episodes, active intents, dominant
intent, and conflict density for one symbol. Use it to understand pressure on a
hot symbol before you pile on.

## Output

`fleet_record_episode` on a destructive overlap, then `fleet_get_escalation` once decided:

```jsonc
// fleet_record_episode
{ "conflict_class": "C", "escalation_id": "01J…",
  "mediation_request": { /* every agent's assignment + the contested symbols */ } }

// fleet_get_escalation — poll until your_directive ≠ "wait"
{ "your_directive": "defer",
  "resolution": "recommend: agent-b — rebase the fix onto the signature change" }
```
