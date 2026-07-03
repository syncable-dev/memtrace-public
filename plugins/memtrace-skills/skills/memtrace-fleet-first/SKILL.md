---
name: memtrace-fleet-first
description: "Coordinate fleets of coding agents sharing one repo+branch: declare typed intents, classify edit episodes, and resolve conflicts before they collide. Use FIRST when more than one coding agent works the same repo+branch at once (a 'fleet'), before reading code, planning a refactor, or making an edit — triggered by 'I'm about to edit X', 'rename Y across the codebase', joining a running fleet/session branch, coordinating with other agents, or prose hand-offs. Do not grep for 'who else is touching this' and do not skip fleet_publish_intent because 'it's a small change'. Fleet coordination is branch-scoped: pass your session branch so your fleet coordinates and stays isolated from agents on other branches. Skip ONLY for genuinely solo sessions or pure docs-only edits where coordination has zero value."
allowed-tools:
  - mcp__memtrace__fleet_status
  - mcp__memtrace__fleet_branch_context
  - mcp__memtrace__fleet_preflight
  - mcp__memtrace__fleet_publish_intent
  - mcp__memtrace__fleet_record_episode
  - mcp__memtrace__fleet_get_escalation
  - mcp__memtrace__fleet_submit_verdict
  - mcp__memtrace__fleet_get_node_state
metadata:
  author: "Syncable <support@syncable.dev>"
  version: "1.0.0"
  category: development
---

# Fleet First

The coordination layer for **fleets of coding agents** working the same repo at the
same time. It stops agents from silently clobbering each other's edits, and turns
unsafe overlaps into a clear decision instead of a merge-time surprise.

## The Iron Law

```
IN A FLEET → FLEET TOOLS BEFORE EDITS. NO EXCEPTIONS.
  1. fleet_publish_intent   (declare what you'll touch; get blast radius + conflicts)
  2. edit                   (your normal edit loop)
  3. fleet_record_episode   (classify A/B/C; if C, the loop resolves it)
```

A typed intent serializes to ~20 tokens; a prose "I'm going to change X" averages
200+. A 10-agent fleet × 100 edits = tens of thousands of tokens saved per
fleet-turn — and zero silent overwrites — when the protocol is followed.

## A fleet = agents on ONE branch (always pass it)

**Coordination is branch-scoped.** Two agents only coordinate when they're on the
**same `(repo, branch)`**. The branch name is the fleet identifier: a *session
branch* is how a group of agents opts into one coordinating fleet.

- Working a session branch (`session/auth-revamp`)? Pass `branch` on **every**
  fleet call. Your fleet coordinates together and stays isolated from agents on
  other branches.
- Omit `branch` only for the shared default pool (single, unnamed fleet).
- Agents on **different** branches never conflict — git already isolates them, and
  whether branches merge isn't guaranteed, so the fleet never reasons across them.

```jsonc
{ "repo_id": "myrepo", "branch": "session/auth-revamp",
  "agent_id": "agent-a", "touched": ["auth::verify_token"],
  "intent": {"refactor": {"pattern": "change_signature"}},
  "assignment": "widen verify_token signature for pagination" }
```

Always include **`assignment`** — your natural-language task. When a conflict
happens, that's what the judge (another agent) or a human reads to reconcile.

## Check the fleet first (once per session and after idle)

Set a stable identity before any fleet call:

```
MEMTRACE_AGENT_ID=<your-agent-id>   # required in fleet / hosted environments
```

```
fleet_branch_context({repo_id, branch, agent_id})
  → you, peers[], pending_escalations, recent_peer_episodes, graph_revision
fleet_status()        → live_intents, active_agents, pending_escalations, mediator_mode
```

Call **`fleet_branch_context`** at session start and after idle periods so you do not
confuse peer WIP with settled graph truth. If fleet tools respond, coordination is
active — follow this skill for every edit. An empty peer list is **not** permission
to skip: it just means you're the first agent in this window.

## The protocol, step by step

1. **Before editing** — `fleet_preflight({repo_id, branch, agent_id, touched, intent})`
   (read-only) or go straight to `fleet_publish_intent(...)`. You get the blast
   radius, any overlapping live intents on your branch, and a `coordination` block
   that may already suggest who owns a contested symbol.
2. **Edit** — your normal loop.
3. **After editing** — `fleet_record_episode({repo_id, branch, agent_id, touched, intent})`.
   It returns a `conflict_class`:
   - **A — proceed.** Additive, order-independent. Nothing to do.
   - **B — re-read, then proceed.** You overlap non-destructively; re-read the
     shared symbols so you build on current state.
   - **C — a decision is needed.** A destructive change overlaps another agent's
     work. This does **not** auto-resolve — see below.

## Class C: the decision loop (read this)

A Class C means two edit paths can't both land safely. `fleet_record_episode`
returns an `escalation_id` and a `mediation_request` (when mediation is enabled).
What happens next depends on who judges:

- **You may be asked to judge.** The `mediation_request` carries **every agent's
  assignment** — including the other side's. Read them and submit a verdict with
  `fleet_submit_verdict({escalation_id, agent_id, verdict})`, where `verdict` is one
  of:
  - `{"kind":"reconcile","merge_plan":"…"}` — the changes combine; here's how.
  - `{"kind":"recommend","winner":"<agent_id>","rationale":"…","confidence":0.0-1.0}` —
    one path should continue.
  - `{"kind":"defer_to_human","question":"…"}` — a real product call; ask a human.
- **A human may decide** in the Fleet dashboard. Either way the outcome flows back
  to you.
- **Close YOUR loop**: poll `fleet_get_escalation({escalation_id, agent_id})` until
  `your_directive` is no longer `wait`:
  - `proceed` — you were chosen; continue.
  - `defer` — another path won; stand down and rebase your work onto it.
  - `review` — read the free-text `resolution`.

The daemon is a deterministic referee: it never auto-applies a destructive
*removal* (delete/move) without a human, and only auto-applies when it's safe (a
clear machine case, or independent agent consensus). So the judge being wrong
degrades to "a human reviews a suggestion," never a silent bad merge.

## Routing — what do you need?

| You're about to… | Do this |
|---|---|
| Start any edit in a fleet | `fleet_publish_intent` (declare it) — never skip |
| Check before declaring | `fleet_preflight` (read-only "is the coast clear?") |
| Finish an edit | `fleet_record_episode` (get A/B/C) |
| Got a Class C as the judge | `fleet_submit_verdict` (reconcile/recommend/defer) |
| Blocked on a Class C | poll `fleet_get_escalation` until `your_directive ≠ wait` |
| See who's in the fleet | `fleet_status` (active_agents, pending decisions) |
| Inspect a symbol's coordination state | `fleet_get_node_state` |

## Parameter notes

- Pass `intent` as JSON (externally-tagged, snake_case):
  `{"refactor":{"pattern":"rename_symbol"}}`, `{"bug_fix":{"defect":"logic_error"}}`.
  Destructive kinds: `refactor/change_signature`, `refactor/move_symbol`,
  `cleanup/dead_code` — these are what trigger Class C over shared symbols.
- `touched` is a list of qualified symbol identities (e.g. `"module::Symbol"`).
- Always include `branch` (your session branch) and `assignment` (your task).

Full parameter spec for every Memtrace tool: [references/mcp-parameters.md](../../references/mcp-parameters.md).

## When to skip

Skip the protocol only for a genuinely **solo** session (you're the only agent and
no one else shares your branch) or **pure docs-only** edits where coordination has
zero value. Everything else in a fleet goes through the protocol.

## Output

Key return shapes (fields documented above):

```jsonc
// fleet_record_episode →
{ "conflict_class": "C", "escalation_id": "…", "mediation_request": { /* every agent's assignment */ } }
// fleet_get_escalation →
{ "your_directive": "wait | proceed | defer | review", "resolution": "free text (read on review)" }
// fleet_status →
{ "live_intents": [ /* … */ ], "active_agents": [ /* … */ ], "pending_escalations": [ /* … */ ], "mediator_mode": "…" }
```
