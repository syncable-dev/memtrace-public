# Memtrace documentation

Practical reference for using Memtrace in your day-to-day agent
workflows. **If you're new, start with [`getting-started.md`](getting-started.md).**

Memtrace is freeware — free to install and use, but not open source.
This documentation covers everything a user needs to be productive
with Memtrace; if you can't find an answer here, ping us on Discord
or open a GitHub issue at
[`syncable-dev/memtrace-public`](https://github.com/syncable-dev/memtrace-public/issues).

## What Memtrace is

A persistent, structural memory layer for coding agents.

Index a codebase once. Every agent query after that — "where is symbol
X defined", "what calls Y", "how does authentication work", "what
breaks if I change Z" — resolves through a knowledge graph in
milliseconds, with the agent receiving compact, exact-line answers
instead of having to grep, glob, and read its way through files.

You install it once per machine. Your AI tool (Claude Code, Cursor,
Codex, Gemini CLI, any MCP-compatible client) picks up the MCP server
automatically. The graph rebuilds itself as you edit code.

## What's in this folder

Topics are roughly ordered "what you need to know first" → "what you
look up later":

| Doc | What's in it |
|---|---|
| [`getting-started.md`](getting-started.md) | Install, first-run walkthrough, how `memtrace start`, `memtrace index`, and `memtrace mcp` relate, what to expect on a fresh machine. |
| [`code-reviewer.md`](code-reviewer.md) | How to use Memtrace as a GitHub PR reviewer from the CLI or an agent: feature branch, PR, posted review, watched commands, and optional local fixes. |
| [`cli-reference.md`](cli-reference.md) | Current `memtrace --help` command surface: start/status/index/MCP, code review, PR watches, embedding provider commands, flags, and key env vars. |
| [`architecture.md`](architecture.md) | High-level picture of the components — daemon, MCP server, MemDB, indexer, embedding pipeline. No deep internals; just enough to reason about behaviour. |
| [`workspaces.md`](workspaces.md) | Share one knowledge graph across multiple repos: when to use a workspace, the `.memtrace-workspace` marker, resolution order, cross-repo edges, and the `--workspace` flag. |
| [`data-directories.md`](data-directories.md) | Every directory Memtrace creates: `.memdb/`, `.memtrace/`, `~/.memtrace/embed-cache/`, model caches. What's in each, where it lives, when to delete it. |
| [`embedding-providers.md`](embedding-providers.md) | Local presets, remote/BYO embedding providers (OpenAI, Voyage, Ollama, Infinity, TEI, …), Matryoshka dim truncation, the `memtrace embed` CLI. |
| [`environment-variables.md`](environment-variables.md) | The full env var reference — transport, ports, model selection, RAM tuning, embedding caps. |
| [`mcp-and-transports.md`](mcp-and-transports.md) | How agents talk to Memtrace. stdio (per-session subprocess) vs streamable-HTTP (one server, many concurrent agents). When to pick which. |
| [`tools.md`](tools.md) | The full MCP tool catalogue — `find_symbol`, `find_code`, `get_symbol_context`, `get_impact`, `get_evolution`, etc. Inputs, outputs, when to use which. |
| [`cortex.md`](cortex.md) | Cortex decision memory: supported MCP setup, `recall_decision`, governance confirmation, status checks, and recovery without deleting the store. |
| [`workflows.md`](workflows.md) | Common patterns: starting a new project, onboarding to an unfamiliar codebase, debugging an incident, refactoring safely, time-travel queries. |
| [`performance-tuning.md`](performance-tuning.md) | Fitting Memtrace to your machine. Auto-tuning by RAM, model selection, batch sizes, RSS guardrails. |
| [`troubleshooting.md`](troubleshooting.md) | Concrete fixes for the most common failure modes — slow startup, swap blowouts, MCP not appearing in your client, indexing hangs. |
| [`privacy-and-telemetry.md`](privacy-and-telemetry.md) | What stays on your machine, what's optionally sent to us, how to turn telemetry off. |
| [`v0.4.60-release-notes.md`](v0.4.60-release-notes.md) | The memory-footprint round: −15% RSS, 35× tighter variance, −41% binary, no behaviour change. Two new env knobs (`MEMTRACE_UNIFIED_CACHE_MB`, `MEMTRACE_ORT_LOW_RSS`); both have sensible defaults. |

**0.6.10 highlights** (see also [`cli-reference.md`](cli-reference.md) and [`environment-variables.md`](environment-variables.md)): fixes for GitHub issues #7–#17 (Claude Code hook JSON, graph/stats/temporal accuracy), unified `--headless` / `MEMTRACE_HEADLESS`, and removal of `memtrace service` / OS login autostart.

## The 90-second tour

```bash
# Install
npm install -g memtrace

# Start the local owner + UI (auto-indexes the project you launch it from)
memtrace start

# In another terminal: open the local UI
open http://localhost:3030

# Tell your agent (Claude Code, Cursor, etc.) to use Memtrace.
# If you installed via npm, the MCP integration is wired automatically.
# Open Claude Code and try a question like:
#
#   "where is the user-authentication logic?"
#
# The agent will use memtrace's `find_code` tool — exact file:line
# answers, no grep needed.
```

For agent-only workflows, your AI tool can launch `memtrace mcp`
without a separate `memtrace start`. It attaches to an existing
workspace owner when one is running, or becomes the owner itself for
that session.

That's the headline. Everything below is for when you want to go
deeper.

## Important conventions in this documentation

- **Commands you run** are shown in fenced bash blocks.
- **MCP tool names** (the agent-facing API) are written
  `mcp__memtrace__find_symbol` — the exact form your agent sees.
- **CLI commands** are written `memtrace <subcommand>`.
- **Env variables** are written `MEMTRACE_FOO`. The full reference is
  in [`environment-variables.md`](environment-variables.md).
- **File paths** that Memtrace creates start with `~/.memtrace/`
  (your home), `.memdb/` (per-project, in your repo), or `.memtrace/`
  (per-project — older convention).

## How to read these docs

If you only have five minutes, read [`getting-started.md`](getting-started.md)
and the section of [`workflows.md`](workflows.md) that matches your
situation. Everything else can be looked up when you need it.

If you're integrating Memtrace into a long-running server (orchestrator,
agent platform, dashboard), [`mcp-and-transports.md`](mcp-and-transports.md)
is the one you want.

If your laptop is being eaten and the dev server is unresponsive,
[`performance-tuning.md`](performance-tuning.md) →
[`troubleshooting.md`](troubleshooting.md).

## Versioning

Memtrace ships frequently. Features described here track the
**latest released version on npm**. If you're on an older version,
some env vars or tools may not exist yet — `memtrace --version` tells
you what you're running. Major user-visible changes are summarised in
release notes on [GitHub Releases](https://github.com/syncable-dev/memtrace-public/releases).

## Where this documentation lives

Source is at
[`syncable-dev/memtrace-public/docs/`](https://github.com/syncable-dev/memtrace-public/tree/main/docs).
Documentation issues and PRs are welcome — even just "this part is
confusing" issues help us a lot.
