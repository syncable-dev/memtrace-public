---
name: memtrace-fleet-publish-intent
description: "Declare a structural intent BEFORE editing in a fleet — what symbols you'll touch and why — so other agents on your branch coordinate around you. Use when the user says 'I'm about to edit/rename/refactor X', or when starting any non-trivial edit while other agents share your repo+branch. Returns the graph blast radius, overlapping live intents on your branch, and a shift-left coordination/partition hint. Do not start editing shared symbols without publishing first."
---

## Overview

`fleet_publish_intent` is step 1 of the fleet protocol: announce what you're about
to touch so other agents on the **same `(repo, branch)`** coordinate around you.
It's a typed, ~20-token declaration — not prose.

## Call it

```jsonc
fleet_publish_intent({
  repo_id: "myrepo",
  branch:  "session/auth-revamp",      // your fleet's session branch
  agent_id:"agent-a",
  touched: ["auth::verify_token"],     // qualified symbol identities
  intent:  {"refactor": {"pattern": "change_signature"}},
  assignment: "widen verify_token signature for pagination"   // the alignment anchor
})
```

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

## Output

```json
{
  "impact_preview": "…graph blast radius of the touched symbols…",
  "active_conflicts": [],
  "coordination": { "would_escalate": false, "suggested_partition": null }
}
```

- `impact_preview` — the real graph blast radius of the touched symbols.
- `active_conflicts` — overlapping live intents **on your branch** (none from other
  branches; coordination is branch-scoped).
- `coordination` — a shift-left hint: `would_escalate`, a `suggested_partition`
  (who should own a contested symbol), and advice to realign *before* you edit.

## Intent kinds (JSON, snake_case, externally-tagged)

- `{"refactor":{"pattern":"rename_symbol"|"change_signature"|"move_symbol"|"extract_function"|…}}`
- `{"feature_add":{"surface":"new_symbol"|"new_endpoint"|…}}`
- `{"bug_fix":{"defect":"logic_error"|"null_handling"|…}}`
- `{"cleanup":{"kind":"dead_code"|"unused_import"|…}}`
- `{"performance":{"axis":"latency"|…}}`, `{"security_fix":{"severity":"high"}}`,
  `{"test_add":{"covers":[…]}}`, `"docs_only"`, `"exploratory"`

**Destructive** kinds — `change_signature`, `move_symbol`, `cleanup/dead_code` —
are what raise a Class C decision when they overlap another agent's work.

## Rules

- Always pass `branch` (your session branch) and `assignment` (your task).
- Read-only check first? Use `fleet_preflight` (same inputs, no registration).
- An empty `active_conflicts` is good — proceed and `fleet_record_episode` when done.
