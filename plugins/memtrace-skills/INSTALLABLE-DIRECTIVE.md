# Installable directive: put the Memtrace reflex in the agent's context

> The plugin ships skills, commands, a hook, and an MCP server-instruction block. Two
> of those carry the "graph before grep" reflex only conditionally: the hook is
> scoped to indexed repos and can be opted out, and the injected MCP block can be
> truncated in a long session. To GUARANTEE the reflex is present, add the compact
> directive below to the agent's context, the same way you would add any standing
> instruction to `CLAUDE.md`. Do this AFTER installing the plugin (the directive
> names tools the plugin provides). It is optional but recommended.

## When to install it

- You want the memtrace-first reflex even where the hook is off (`MEMTRACE_ENFORCE=off`)
  or absent (a build without the hook).
- You want the recall-before-edit and impact-before-edit reflexes stated once, in
  context, rather than relying on each skill's description being ranked correctly.
- You run long sessions where the injected MCP block may be truncated.

## Form A: a block for `CLAUDE.md` (project or global)

Paste this into your project `CLAUDE.md`, or your global `~/.claude/CLAUDE.md`, under
a heading of your choice. It is plain guidance, no tool of its own.

```md
## Memtrace: graph before grep

In a repository Memtrace has indexed:

- Code discovery goes through the graph first. To locate a symbol, find callers, or
  map a subsystem, call `find_code` / `find_symbol` / `get_symbol_context` before
  `grep` / `rg` / `git grep`. A 0 result means broaden the query or reindex, not fall
  back to grep.
- Blast radius before an edit. Before changing an existing symbol, call `get_impact`
  (or the `/mt-preflight` command) rather than hunting references by hand.
- Recall before you remove. Before deleting or refactoring existing code you did not
  just write, query decision memory (`recall_decision` / `why_is_this_here` /
  `governing_contracts`, or `/mt-recall`) so a ban or contract is not silently broken.
- History from the graph. For what changed and when, prefer `get_evolution` /
  `get_timeline` / `get_changes_since` over `git log` / `git diff`.

Unaffected: filename globbing (`find`, a name glob), config / data / docs, a plain or
piped single-file `grep`, and any path outside an indexed repo. This is about code
content, not text filtering.
```

## Form B: a rules file (for setups that use `~/.claude/rules/`)

Some setups load standing instructions from per-topic files instead of `CLAUDE.md`.
For those, save the SAME block above as `~/.claude/rules/memtrace.md` (drop the `##`
heading line if your loader adds its own). One topic per file; this file's topic is
the graph-before-grep reflex.

## Optional: let the installer offer it

A future `memtrace install` could OFFER (never silently write) to append Form A to a
`CLAUDE.md` the user names, or to drop Form B into `~/.claude/rules/`, with an explicit
prompt and an easy removal path. Writing to a user's `CLAUDE.md` or rules directory is
the user's decision, so an installer should present the block and let the user place
it, not modify config unprompted.

## Relationship to the other directive surfaces

This installable block is the always-in-context form of the same doctrine that
`MEMTRACE.md` states in full and that `SERVER-INSTRUCTIONS.md` injects via the MCP
server. Installing it is belt-and-suspenders: the hook enforces deterministically
where it runs, the MCP block nudges each session, and this block guarantees the reflex
is in context regardless. All three are opt-out friendly.
