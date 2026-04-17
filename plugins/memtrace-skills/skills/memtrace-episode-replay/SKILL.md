---
name: memtrace-episode-replay
description: "Replay the sub-commit narrative for a specific symbol in an indexed codebase — every working_tree save between two commits, including abandoned attempts. USE when you need to understand WHY current code looks the way it does, reconstruct an incident timeline, or avoid repeating a previously-reverted approach. DO NOT USE for a snapshot of current code (read the file), for ranked 'what changed' across a window (→ memtrace-evolution), for a symbol's plain version list (→ `get_timeline`), or if the target repo was indexed without the file watcher running (there are no working_tree episodes to replay)."
---

## Overview

Replay the sub-commit implementation narrative for any symbol. Between any two commits, Memtrace recorded every file save as a `working_tree` episode. This tool surfaces that sequence — the attempts, the reversions, the iterative refinements — not just the final committed state.

**Git shows A→B. Episode replay shows every step in between.**

This is the only tool that can answer: "why does this code look like this?" without relying on commit messages or comments.

## CRITICAL: parameter types are strict

Full schema for every Memtrace tool: **`../../references/mcp-parameters.md`**. Pitfalls specific to this skill:

* `symbol` is a NAME string (e.g. `"validateToken"`), NOT a `symbol_id` UUID.
* `from` / `to` are ISO-8601 strings with timezone — `"2026-04-10T00:00:00Z"`, not `"2026-04-10"`.
* `include_working_tree` is a boolean. `"true"` as a string fails validation.

## `get_episode_replay` — parameters

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | From `list_indexed_repositories` |
| `symbol` | string | yes | — | Exact name, e.g. `"validateToken"` |
| `from` | string (ISO-8601) | yes | — | Window start |
| `to` | string (ISO-8601) | yes | — | Window end |
| `branch` | string | no | `"main"` | |
| `include_working_tree` | boolean | no | `true` | `false` = commits only |

## `get_timeline` — parameters (for locating the window first)

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `repo_id` | string | yes | — | |
| `scope_path` | string | yes | — | e.g. `"AuthService::validateToken"` |
| `file_path` | string | yes | — | Containing file |
| `branch` | string | no | `"main"` | |

## Workflow

1. **Confirm the symbol name** — call `find_symbol` if you're unsure of the exact name.
2. **Find the window** — if the user didn't specify dates, call `get_timeline` with `scope_path` + `file_path` to see when this symbol changed, then pick a window around the interesting change.
3. **Call `get_episode_replay`** with `symbol` name + the window. Keep `include_working_tree: true` for the full sub-commit narrative.

### 3. Read the narrative_hint sequence

Each episode has a `narrative_hint` — derived automatically from AST hash patterns:

| Hint | What it means |
|---|---|
| `committed` | A real git commit — the "public record" checkpoint |
| `pre_commit_finalization` | Last working_tree save before a commit — the final draft |
| `iterative_refinement` | 3+ consecutive working_tree saves — active development in progress |
| `attempted_and_reverted` | Hash returned to a prior state — something was tried and backed out |
| `no_change` | File was saved but this symbol didn't change |
| `working_tree_save` | A single file save with structural changes |

### 4. Reconstruct the implementation story

Read the sequence like a narrative:

```
committed              ← "here's where we started"
working_tree_save      ← "first attempt"
iterative_refinement   ← "refining the approach"
attempted_and_reverted ← "tried X, it was wrong, backed out"
pre_commit_finalization← "final version before commit"
committed              ← "here's what shipped"
```

The gap between `committed` entries is the implementation story.

### 5. Identify what to act on

| Pattern | Implication |
|---|---|
| `attempted_and_reverted` appears | There was a tried-and-abandoned approach — understand why before trying similar |
| Multiple `iterative_refinement` clusters | The author was unsure — this area may need extra care |
| No working_tree episodes (commits only) | Code was written elsewhere or pasted in — less implementation history available |
| Very short episode sequence | Straightforward change — low implementation complexity |

## When to Use

- **Before modifying unfamiliar code** — understand the intent, not just the current state
- **Post-session debugging** — replay what was tried during a broken session
- **Code review** — understand the reasoning behind non-obvious implementations
- **Avoiding dead ends** — check if the approach you're about to try was already attempted and reverted

## Compression

With `compress: true` (default), consecutive episodes with identical `ast_hash` are collapsed to first+last of the run. Cosmetic saves and whitespace-only edits are filtered out. Only structurally significant transitions are shown.

With `compress: false`, every single save is shown — useful when you want to see exact timing between saves.

## Common Mistakes

| Mistake | Reality |
|---------|---------|
| Only reading the final committed code | The commit shows *what*, the episode replay shows *why* — always check both for unfamiliar code |
| Ignoring `attempted_and_reverted` hints | These are the most valuable entries — they represent knowledge about what doesn't work |
| Using `include_working_tree: false` by default | Commits-only loses all the sub-commit narrative — only use this if you explicitly want commit-level granularity |
| Large windows with compress off | Very long histories produce noise; use `compress: true` unless you need exact save-by-save granularity |
