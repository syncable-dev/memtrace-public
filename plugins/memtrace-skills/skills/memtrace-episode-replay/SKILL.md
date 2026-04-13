---
name: memtrace-episode-replay
description: "Use when an agent needs to understand why code looks the way it does, replay implementation steps between commits, find what was tried and reverted, understand a colleague's (or your past self's) reasoning, or avoid repeating a previously-abandoned approach"
allowed-tools:
  - mcp__memtrace__get_episode_replay
  - mcp__memtrace__get_timeline
  - mcp__memtrace__find_symbol
  - mcp__memtrace__list_indexed_repositories
user-invocable: true
---

## Overview

Replay the sub-commit implementation narrative for any symbol. Between any two commits, Memtrace recorded every file save as a `working_tree` episode. This tool surfaces that sequence — the attempts, the reversions, the iterative refinements — not just the final committed state.

**Git shows A→B. Episode replay shows every step in between.**

This is the only tool that can answer: "why does this code look like this?" without relying on commit messages or comments.

## Steps

### 1. Identify the symbol and time window

Use `find_symbol` to get the exact symbol name if needed. Determine the window:
- `from` — when to start (e.g. a few days before a confusing commit)
- `to` — when to end (usually the commit timestamp or now)

If you don't know the window, call `get_timeline` first to find when the symbol changed.

### 2. Call `get_episode_replay`

```
get_episode_replay(
  repo_id: "...",
  symbol: "execute",
  from: "2026-04-10T00:00:00Z",
  to:   "2026-04-13T00:00:00Z",
  include_working_tree: true,   // false = commits only
  compress: true                // collapse identical-hash runs
)
```

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
