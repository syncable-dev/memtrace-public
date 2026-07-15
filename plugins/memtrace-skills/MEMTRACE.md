# MEMTRACE.md: the plugin's top-level directive (precursor)

> The doctrinal precursor of the memtrace/memfleet plugin. The skills, the four
> `/mt-*` commands, the enforce hook, and the MCP server-instruction block are the
> IMPLEMENTATIONS of the reflexes stated here. This file is the "why and when"; the
> skills are the "which tool". It is meant to ship with the plugin as its guidance
> doc, and to be adapted into an installable directive (see
> `INSTALLABLE-DIRECTIVE.md`) so the reflexes reach the agent's context.

## The one reflex

Inside a repository Memtrace has indexed, route CODE work through the graph BEFORE
raw text tools:

- code discovery (locate a symbol, find callers, map a subsystem) goes to
  `find_code` / `find_symbol` / `get_symbol_context`, not `grep` / `rg` / `git grep`;
- blast radius before an edit goes to `get_impact`, not a manual reference hunt;
- change history and "what moved when" go to `get_evolution` / `get_timeline` /
  `get_changes_since`, not `git log` / `git diff`;
- decision rationale (why does this exist, was this banned) goes to Cortex decision
  memory, not a guess from the diff.

A 0 result from a graph tool means broaden the query or reindex, NOT fall back to
grep. Filename globbing (`find`, a name glob), config / data / docs, and paths
outside any indexed repo are UNAFFECTED: the reflex is about code content, not text
filtering.

## The family map (what fires when)

The plugin's skills group into intent families. Reach for the family, not the tool
name; a router skill picks the leaf.

| Family | When it fires | Entry |
|---|---|---|
| Discovery and structure | locate code, traverse a symbol's edges, map a repo, run graph algorithms, see the API surface | `memtrace-first` (router) then search / relationships / codebase-exploration / graph / api-topology |
| Pre-edit safety and impact | what breaks if I change this; a gate before editing one symbol; a risk-rated plan for a multi-symbol change | impact -> preflight -> change-impact-analysis (a ladder by scope) |
| History and temporal | what changed over a range, one episode's diff, historical coupling, root-cause a failure, catch up at session start | evolution / episode-replay / cochange / incident-investigation / session-continuity / daily |
| Decision memory | why does this exist, was this decided or banned, a symbol's lineage, whether a decision held, where it was implemented | `memtrace-decision-memory` (router over recall_decision / why_is_this_here / governing_contracts / verify_intent / get_arc) |
| Docs | how does Memtrace work, install or config, what a tool does, read a docs page | `memtrace-docs` (router over search_docs / ask_docs / read_doc) |
| Fleet coordination | more than one agent shares a repo and branch: declare intent, record edits, resolve a conflict | `memtrace-fleet` (concept) then the publish-intent / record-episode / resolve leaves |
| Code health and quality | dead code and complexity hotspots, a refactor plan, a GitHub PR review, empirical style norms | quality / refactoring-guide / code-review / style-fingerprint |
| Indexing and lifecycle | index a repo once, or watch it for live re-index | index / continuous-memory |

## The load-bearing reflexes

1. Graph before grep, in an indexed repo, for code content.
2. Recall before you remove: before deleting or refactoring existing code you did not
   just write, query decision memory (`recall_decision` / `why_is_this_here` /
   `governing_contracts`) so a ban or contract is not silently broken.
3. Blast radius before you edit: `get_impact` on a single symbol, a bundled preflight
   on one symbol, a risk-rated plan for a multi-symbol change.
4. Catch up from anchors, not from `git log`: session-continuity and the daily
   briefing diff the graph at save granularity across sessions.
5. Fleet first on a shared branch: when more than one agent works the same repo and
   branch, declare a typed intent before editing.

## The layers that carry this

- SKILLS: per-intent routing (the family map above), one skill per shape.
- COMMANDS: `/mt-onboard`, `/mt-preflight`, `/mt-recall`, `/mt-impact` as one-keystroke
  entries into the most common workflows.
- HOOK: a deterministic PreToolUse gate redirects raw recursive code search to the
  graph inside an indexed repo, and fails open on anything ambiguous (plain grep, a
  filename glob, a path outside any indexed repo, the daemon down, or the opt-out
  flag).
- MCP server-instructions: the always-injected form of reflex 1, leading with the
  routing doctrine rather than a version banner (see `SERVER-INSTRUCTIONS.md`).
- INSTALLABLE directive: the same reflexes as a block a user adds to their `CLAUDE.md`
  or a rules file after installing the plugin, so the doctrine is present in context
  even where the hook is off or absent (see `INSTALLABLE-DIRECTIVE.md`).

Opt-out is always available: `MEMTRACE_ENFORCE=off` disables the hook; the reflexes
stay advisory.
