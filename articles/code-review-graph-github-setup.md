---
title: "Code-Review-Graph GitHub Setup Explained for Dev Teams"
description: "Learn how to set up code-review-graph github repos step by step and give your dev team precise AI context without wasting tokens on every review."
ogTitle: "Code-Review-Graph GitHub Setup Guide for Dev Teams Using AI Code Review Tools"
ogDescription: "Set up code-review-graph on your GitHub repos to give AI coding tools a queryable local graph. Cut token usage, speed up reviews, and keep context precise."
date: 2026-11-02
image: ./code-review-graph-github-setup.png
imageAlt: "Developer configuring code-review-graph on a GitHub connected repository in a terminal window"
---

# Code-Review-Graph GitHub Setup Explained for Dev Teams

Setting up code-review-graph on GitHub-connected repositories gives AI coding tools a queryable local graph instead of raw file dumps. Install it with pip, pipx, or uv, run the auto-configurator, and initialize the graph to give your AI assistant precise codebase context without burning excess tokens.

AI-assisted code reviews are token-hungry by default. Without structural context, your coding agent reads raw file dumps, guesses at call chains, and spends most of its context window just orienting itself. Code-review-graph changes that model: it builds a queryable local graph of functions, callers, and tests, then delivers exactly the right slice of context over MCP when the agent needs it. This guide walks through every step, from installation to a configured, running graph ready for your AI coding tool to query.

## What Is code-review-graph and Why Dev Teams Are Adopting It

Code-review-graph builds a persistent, local structural map of your repository using Tree-sitter, then serves precise context to AI coding tools over the Model Context Protocol. Instead of feeding an agent a wall of raw source files, it answers targeted queries: which functions call this method, which tests cover this path, what changed in this module. The agent gets structural context, not a file dump.

The token savings are real. According to benchmarks provided by the developer, code-review-graph reduced token usage for the FastAPI repository by 8.1x across a 2,915-file sample. That figure is vendor-reported, not an independent audit, but it reflects the core mechanic: a graph query returns a precise subgraph, not a broad chunk of source text.

The architecture is local-first. The graph stores in SQLite on your machine, no telemetry runs, and the project ships under the MIT license. Incremental re-indexing after a file change takes roughly 2 seconds, so the graph stays current as your team pushes commits.

Community adoption has moved fast, with the project accumulating 26,588 GitHub stars.

What sets it apart from generic code search is the graph structure itself. A traditional search tool returns file matches. Code-review-graph returns relationships: callers, callees, API surfaces, and test coverage, all linked. That structural context is what lets an AI agent reason about blast radius before it makes an edit, rather than after.

## How to Install code-review-graph on GitHub-Connected Repos

Code-review-graph requires Python 3.10 or later. Confirm your version before starting.

```bash
python --version
```

Once you have a compatible Python environment, choose one of three install paths:

1. **pip**, the standard choice for most developers already working in a virtual environment:
   ```bash
   pip install code-review-graph
   ```

2. **pipx**, installs the tool in an isolated environment so it does not interfere with project dependencies. Recommended if you want a clean global install:
   ```bash
   pipx install code-review-graph
   ```

3. **uv**, the fastest option if your team already uses uv for dependency management:
   ```bash
   uv tool install code-review-graph
   ```

All three paths install the same package. The difference is environment isolation: pip installs into the active environment, pipx creates its own, and uv handles it through its tool layer.

After installation, verify the CLI is available:

```bash
crg --version
```

A successful response confirms the binary is on your path. If the command is not found after a pip install, check that your virtual environment is activated and that its `bin` or `Scripts` directory is in `PATH`.

For GitHub-connected repos, no special GitHub configuration is required at this stage. Code-review-graph reads your local checkout directly, so it works with any repository you have cloned, regardless of the remote host. According to the project's GitHub page, a cold build on a repository of approximately 3,000 files completes in around 40 seconds. That is a vendor-reported reference point, not a guaranteed spec, and build time will vary with repository size and hardware.

Run the install from the root of your repository. The tool uses the working directory to scope the graph, so positioning matters before you move to the next step.

## How to Configure MCP for Claude Code, Cursor, and Other AI Coding Tools

With the package installed, run the auto-configurator from your repository root:

```bash
crg install
```

This command scans your machine for supported AI coding tools and writes MCP server entries to each tool's configuration file automatically. You do not need to edit JSON or TOML by hand.

The tools it detects and configures include Claude Code, Cursor, Windsurf, Zed, and other editors that support the Model Context Protocol. For each detected tool, the installer adds an entry that points to the local MCP server code-review-graph exposes. When the AI agent needs context, it queries that server rather than reading raw source files.

**Verifying the Claude Code configuration**

After running `crg install`, open Claude Code's MCP settings and confirm an entry for `code-review-graph` appears in the server list. The entry should show the local server path and a status of connected or enabled, depending on your Claude Code version.

**Verifying the Cursor configuration**

In Cursor, navigate to Settings and then MCP. The server list should include a `code-review-graph` entry pointing to your local install. If it does not appear, run `crg install` again from the repository root and restart Cursor.

**Manual configuration**

If the auto-configurator does not detect your editor, you can add the MCP server entry manually. The server command is `crg serve`, and the entry follows your editor's standard MCP server format. Check your editor's documentation for the exact config key names.

**Why MCP matters for AI coding code reviews on GitHub**

MCP delivery means the agent queries the graph on demand, mid-session, without you pasting file contents into the chat. For claude code review graph workflows, this keeps the context window focused on the change under review rather than the entire repository. The same applies to any MCP-compatible tool: the graph answers questions about callers, tests, and API surfaces in real time, so the agent reasons about your actual code structure before it acts.

## Building and Updating the code-review-graph Knowledge Graph

Once MCP is configured, you need to build the graph before any agent can query it. There are two paths depending on how you prefer to work.

**Path 1: CLI initialization**

From your repository root, run:

```bash
crg init
```

This command parses the current codebase with Tree-sitter and writes the structural map to a local SQLite database. According to the project's GitHub page, a cold build on roughly 3,000 files completes in about 40 seconds. That is a vendor-reported reference point. Larger repositories will take longer; smaller ones will finish faster.

**Path 2: Chat-interface build**

If you are already inside Claude Code, Cursor, or another supported chat interface, you can trigger the build without leaving the session. The MCP server handles the request and runs the same build process in the background.

**Keeping the graph current**

After the initial build, code-review-graph updates incrementally. When a file changes, re-indexing that file takes roughly 2 seconds. You do not need to rebuild the entire graph after every commit. The tool tracks modifications and updates only the affected nodes.

For a knowledge graph for claude code to stay useful, freshness matters. A stale graph returns outdated caller relationships and misses new tests. The incremental update model means the graph reflects your current codebase as long as you run the tool in the background during active development.

A few practical habits help here. Run `crg init` once per new clone. Let the background process handle incremental updates during normal work. If you pull a large branch with significant structural changes, a full re-index is worth the 40-second wait rather than relying on incremental patches across hundreds of moved functions.

## Can code-review-graph Be Integrated into a GitHub Actions Workflow?

Yes, code-review-graph can run inside a GitHub Actions workflow, though the setup requires some assembly. The tool is a Python package, so any Actions runner with Python 3.10 or later can install it with a standard `pip install` step. From there, you run `crg init` to build the graph against the checked-out repository, then use the graph output as part of a review or analysis job.

The general shape of a CI integration looks like this: check out the repository, set up a Python environment, install code-review-graph, run the initialization command, and then invoke whatever downstream step consumes the graph, whether that is an AI review agent, a custom script, or a graph export. No detailed YAML examples exist in the project's current documentation, so teams will need to compose that workflow themselves based on their CI structure.

**When CI integration makes sense**

Running the graph build in CI is worth the effort when your team wants graph-assisted reviews on every pull request, not just on developer machines. A CI-built graph ensures every reviewer, human or AI, works from the same structural snapshot of the branch under review.

**When local-only use is the better fit**

For smaller teams or early adoption, local-only use is simpler. Each developer runs `crg init` on their clone and queries the graph through their editor. There is no shared state, no CI configuration to maintain, and no runner costs. The trade-off is that graph freshness depends on each developer keeping their local index current.

The decision comes down to consistency versus simplicity. CI integration gives the whole team a single source of truth for the graph on each PR. Local use gives each developer a fast, private graph with no infrastructure overhead. Most teams start local and add CI once they confirm the workflow fits their review process.

## Is My Code Sent to the Cloud During a Graph Build?

No code leaves your machine during a graph build. Code-review-graph uses a local-first architecture: Tree-sitter parses your files on-device, and the resulting graph writes to a SQLite database stored locally. No telemetry runs, and the project ships under the MIT license with no network calls built into the core indexing process.

What stays on your machine: all source files, the parsed AST data, the SQLite graph database, and every query result.

What leaves your machine: nothing, during the build itself. If you then query the graph through an AI coding tool like Claude Code or Cursor, the agent receives a precise subgraph in response to its MCP query. Whether that subgraph travels beyond your machine depends on the AI tool's own data handling, not on code-review-graph.

For security-conscious teams, this distinction matters. The graph builder is fully offline. The AI tool that consumes graph results operates under its own privacy terms. Teams handling sensitive code should review their AI tool's data policy separately from their assessment of code-review-graph itself.

The local-first design also means there is no account creation, no API key, and no cloud dependency for the core graph functionality. You install, build, and query entirely within your own environment.

## When a Managed Code Graph Makes More Sense for Your Team

Code-review-graph works well for individual developers and small teams comfortable running and maintaining their own tooling. For larger teams, the operational overhead adds up: every developer manages their own index, CI integration requires custom YAML, and there is no shared graph state across the organization.

[Memtrace](https://memtrace.io/) is a code graph service that builds and maintains a local graph connecting functions, callers, APIs, and tests across a repository, then delivers that context to AI coding agents over MCP. The key difference from a self-hosted setup is that Memtrace keeps the graph current automatically as the codebase evolves. Your team does not run rebuild commands or manage SQLite files manually.

For teams evaluating options, Memtrace offers several tiers. The Community plan is free and includes 1,000 graph queries per month, then 50 per day after that. Pro costs $9/month and raises the limit to 5,000 queries with no daily throttle. Pro+ at $15/month covers 20,000 queries per month. The Teams plan at $29 per seat per month adds unlimited queries, a self-hosted MemDB, an organization workspace, and seat pool enforcement. Enterprise pricing is custom and includes SSO, an air-gapped MemDB, and deployment support.

The Teams tier is where Memtrace diverges most clearly from a self-hosted code-review-graph setup. Shared organizational workspace means every agent on the team queries the same graph, so a review in one editor reflects the same structural state as a review in another. Seat pool enforcement lets engineering leads control access without per-tool configuration.

Community discussion on code review graph reddit threads consistently surfaces two concerns: graph freshness after large merges, and the effort of keeping local indexes in sync across a team. Memtrace's managed approach addresses both directly.

For teams that want [persistent structural memory for AI coding agents](https://memtrace.io/codemap) without the maintenance burden, a managed graph is the practical path.

Prices and plan limits verified as of November 2026.

## FAQs

### How do I install code-review-graph for a specific editor like Zed?

Run `crg install` from your repository root after installing the package. The auto-configurator detects supported editors, including Zed, and writes the MCP server entry to that editor's configuration file automatically. If Zed does not appear in the detected list, you can add the MCP server entry manually using `crg serve` as the server command, following Zed's standard MCP server configuration format.

### How much token savings can I expect on a large repository?

The developer-reported figure for the FastAPI repository, at 2,915 files, is an 8.1x reduction in token usage. That is a vendor-reported benchmark, not an independently audited result. Your actual savings depend on repository size, query patterns, and which AI tool you use. The mechanic is consistent: a graph query returns a precise subgraph rather than broad file contents, so token usage scales with query specificity rather than repository size.

### Does code-review-graph work with monorepos that have multiple languages?

Code-review-graph uses Tree-sitter for parsing, which supports a wide range of programming languages. The tool scopes the graph to your working directory, so you run `crg init` from the monorepo root to cover the full repository. Multi-language support depends on which Tree-sitter grammars the current version includes. Check the project's GitHub page for the current list of supported languages before committing to a monorepo setup.

### What is the difference between code-review-graph and a traditional code search tool?

A traditional code search tool returns file matches for a query string. Code-review-graph returns structural relationships: which functions call a given method, which tests cover a given path, which API surfaces a module exposes. That distinction matters for AI-assisted reviews because an agent reasoning about blast radius needs caller graphs and test coverage, not just file locations. Search finds where code lives; a code graph explains how it connects.

### Where can I find community discussion and real-world feedback on code-review-graph?

The most active discussion appears on Hacker News, where the developer posted benchmarks and fielded questions from the community. Reddit threads covering code review graph topics surface in programming and AI coding subreddits, though coverage is uneven. The project's GitHub repository includes issues and discussions that reflect real-world usage patterns. For teams evaluating the tool, the Hacker News thread linked to the FastAPI benchmark is a practical starting point for understanding real adoption experience.

## Conclusion

Code-review-graph gives AI coding tools a queryable local graph instead of raw file dumps, which keeps context windows focused and reduces token waste on every review. Install it with pip, pipx, or uv, run `crg install` to configure your editor, then `crg init` to build the graph. For teams that want shared graph state and automatic freshness without managing local indexes, a managed service like Memtrace covers that gap with tiered pricing and organization-level access control.

Start by cloning a representative repository and running a cold build to see how the graph performs against your actual codebase size. Then decide whether local-only use fits your team's workflow or whether a shared, managed graph is worth the step up.
