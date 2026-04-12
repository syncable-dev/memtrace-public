<p align="center">
  <img src="assets/logo.svg" alt="Memtrace" width="120" height="120" />
</p>

<h1 align="center">Memtrace</h1>

<p align="center">
  <strong>Code intelligence graph for AI agents</strong><br/>
  Structural search · Relationship analysis · Temporal evolution · Architectural understanding
</p>

<p align="center">
  <a href="https://www.npmjs.com/package/memtrace"><img src="https://img.shields.io/npm/v/memtrace?style=flat-square&color=00D4B8&label=npm" alt="npm version" /></a>
  <a href="https://www.npmjs.com/package/memtrace"><img src="https://img.shields.io/npm/dm/memtrace?style=flat-square&color=0A1628&label=downloads" alt="npm downloads" /></a>
  <a href="https://github.com/syncable-dev/memtrace-public/stargazers"><img src="https://img.shields.io/github/stars/syncable-dev/memtrace-public?style=flat-square&color=00D4B8" alt="GitHub stars" /></a>
  <a href="https://github.com/syncable-dev/memtrace-public/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Proprietary%20EULA-0A1628?style=flat-square" alt="license" /></a>
  <a href="https://memtrace.dev"><img src="https://img.shields.io/badge/docs-memtrace.dev-00D4B8?style=flat-square" alt="docs" /></a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#skills">Skills</a> ·
  <a href="#mcp-tools">MCP Tools</a> ·
  <a href="#benchmarks">Benchmarks</a> ·
  <a href="#claude-code-setup">Claude Code</a> ·
  <a href="#claude-desktop-setup">Claude Desktop</a>
</p>

---

Memtrace is an MCP server that builds a **persistent knowledge graph** from your codebase. It parses source files, resolves cross-file relationships, detects API endpoints, runs community detection, and embeds all symbols for semantic search — then exposes 25+ tools via the [Model Context Protocol](https://modelcontextprotocol.io) so AI agents can explore, analyze, and reason about your code structurally.

## Quick Start

```bash
# Install (binary + skills + MCP server — all in one)
npm install -g memtrace

# Start the graph database
memtrace start

# Index your project
memtrace index /path/to/your/project
```

That's it. The installer handles everything:

| Step | What happens |
|------|-------------|
| **Binary** | Platform-specific native binary installed via npm |
| **Skills** | 12 AI agent skills written to `~/.claude/skills/` and plugin cache |
| **Plugin** | `memtrace-skills@memtrace` enabled in `~/.claude/settings.json` |
| **Marketplace** | GitHub marketplace registered for auto-updates |
| **MCP Server** | `memtrace mcp` registered in `mcpServers` |

> **Zero configuration required** — just install and start exploring your codebase with Claude.

## Skills

Memtrace ships with **12 skills** that teach Claude _how_ to use the knowledge graph. Skills fire automatically based on what you ask.

### Command Skills

| Skill | Triggers when you say... |
|:------|:------------------------|
| `memtrace-index` | _"index this project"_, _"set up code intelligence"_, _"parse this codebase"_ |
| `memtrace-search` | _"find this function"_, _"where is X defined"_, _"search for authentication logic"_ |
| `memtrace-relationships` | _"who calls this"_, _"what does this function call"_, _"show class hierarchy"_ |
| `memtrace-evolution` | _"what changed this week"_, _"how did this evolve"_, _"what's different since Monday"_ |
| `memtrace-impact` | _"what will break if I change this"_, _"blast radius"_, _"risk assessment"_ |
| `memtrace-quality` | _"find dead code"_, _"complexity hotspots"_, _"code smells"_ |
| `memtrace-graph` | _"show me the architecture"_, _"find bottlenecks"_, _"most important functions"_ |
| `memtrace-api-topology` | _"list API endpoints"_, _"service dependencies"_, _"who calls this API"_ |

### Workflow Skills

Multi-step orchestrations that chain tools together with decision logic:

| Skill | Triggers when you say... |
|:------|:------------------------|
| `memtrace-codebase-exploration` | _"explore this codebase"_, _"I'm new to this project"_, _"give me an overview"_ |
| `memtrace-change-impact-analysis` | _"what will break if I refactor this"_, _"pre-change risk assessment"_ |
| `memtrace-incident-investigation` | _"something broke"_, _"root cause analysis"_, _"what went wrong"_ |
| `memtrace-refactoring-guide` | _"help me refactor"_, _"reduce complexity"_, _"clean up tech debt"_ |

## MCP Tools

25+ tools exposed via the Model Context Protocol:

<table>
<tr>
<td width="50%" valign="top">

**Search & Discovery**
- `find_code` — hybrid BM25 + semantic search with RRF
- `find_symbol` — exact/fuzzy name match with Levenshtein

**Relationships**
- `analyze_relationships` — callers, callees, hierarchy, imports
- `get_symbol_context` — 360° view in one call

**Impact Analysis**
- `get_impact` — blast radius with risk rating
- `detect_changes` — diff-to-symbols scope mapping

**Code Quality**
- `find_dead_code` — zero-caller detection
- `find_most_complex_functions` — complexity hotspots
- `calculate_cyclomatic_complexity` — per-symbol scoring
- `get_repository_stats` — repo-wide metrics

</td>
<td width="50%" valign="top">

**Temporal Analysis**
- `get_evolution` — 6 scoring modes (see below)
- `get_timeline` — full symbol version history
- `detect_changes` — diff-based impact scope

**Graph Algorithms**
- `find_bridge_symbols` — betweenness centrality
- `find_central_symbols` — PageRank / degree
- `list_communities` — Louvain module detection
- `list_processes` / `get_process_flow` — execution tracing

**API Topology**
- `get_api_topology` — cross-repo HTTP call graph
- `find_api_endpoints` — all exposed routes
- `find_api_calls` — all outbound HTTP calls

**Indexing & Watch**
- `index_directory` — parse, resolve, embed
- `watch_directory` — live incremental re-indexing
- `execute_cypher` — direct graph queries

</td>
</tr>
</table>

## Evolution Engine

The temporal analysis engine implements **six distinct scoring algorithms** — choose the right one for the question you're asking:

| Mode | Formula | Best for |
|:-----|:--------|:---------|
| **`compound`** | `0.50 × rank(impact) + 0.35 × rank(novel) + 0.15 × rank(recent)` | General-purpose _"what changed?"_ |
| **`impact`** | `sig(n) = in_degree^0.7 × (1 + out_degree)^0.3` | _"What broke?"_ — largest blast radius |
| **`novel`** | `surprise(n) = (1 + in_degree) / (1 + change_freq_90d)` | _"What's unexpected?"_ — anomaly detection |
| **`recent`** | `impact × exp(−0.5 × Δhours)` | _"What changed near the incident?"_ |
| **`directional`** | Asymmetric: added → out_degree, removed → in_degree | _"What was added vs removed?"_ |
| **`overview`** | Module-level rollup only | Quick summary, no per-symbol scoring |

Uses **Structural Significance Budgeting (SSB)** to select the minimum set of changes covering ≥80% of total significance — surfaces what matters without drowning you in noise.

## Benchmarks

Memtrace is purpose-built to defeat traditional memory systems by natively indexing via AST parsers rather than relying on noisy semantic chunking. All benchmarks run on the same machine against complex, real-world codebases.

### Search Accuracy — 1,000 Multi-Hop Queries

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/benchmarks/search-accuracy.svg"/>
  <source media="(prefers-color-scheme: light)" srcset="assets/benchmarks/search-accuracy.svg"/>
  <img alt="Search accuracy: Memtrace 83.5% vs Vector RAG 25.8%" src="assets/benchmarks/search-accuracy.svg" width="720"/>
</picture>

### Token Context Reduction

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/benchmarks/token-context.svg"/>
  <source media="(prefers-color-scheme: light)" srcset="assets/benchmarks/token-context.svg"/>
  <img alt="Token usage: Memtrace 284K vs Vector RAG 2.4M — 88.2% reduction" src="assets/benchmarks/token-context.svg" width="720"/>
</picture>

### Indexing Speed — 1,500 Files

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/benchmarks/indexing-speed.svg"/>
  <source media="(prefers-color-scheme: light)" srcset="assets/benchmarks/indexing-speed.svg"/>
  <img alt="Indexing: Memtrace 1.5s vs Graphiti 6h vs Mem0 31m" src="assets/benchmarks/indexing-speed.svg" width="720"/>
</picture>

<details>
<summary><strong>Why the difference?</strong></summary>

<br/>

**AST vs. LLM-based ingestion** — General-purpose memory engines (Mem0, Graphiti) use LLM API calls to *guess* code relationships, costing ~$25+ per medium repository and taking minutes to hours. Memtrace compiles native AST parsers via Tree-sitter, resolving deterministic symbol references in seconds for $0.

**Graph traversal vs. vector similarity** — Vector RAG retrieves chunks by cosine similarity, returning noisy context that floods the agent's context window. Memtrace traverses the knowledge graph along explicit `CALLS`, `IMPLEMENTS`, `IMPORTS` edges — returning only the exact subgraph the agent needs.

**Structural integrity** — Memtrace links AST constructs (`(Interface)←[:IMPLEMENTS]-(Class)`) with deterministic precision. No fuzzy semantic nodes, no hallucinated relationships.

</details>

## Claude Code Setup

`npm install -g memtrace` handles everything. For manual setup:

```bash
# 1. Register the marketplace
claude plugin marketplace add syncable-dev/memtrace

# 2. Install the skills plugin
claude plugin install memtrace-skills@memtrace --scope user

# 3. Register the MCP server
claude mcp add memtrace -- memtrace mcp -e MEMGRAPH_URL=bolt://localhost:7687
```

<details>
<summary><strong>What this writes to <code>~/.claude/settings.json</code></strong></summary>

```json
{
  "mcpServers": {
    "memtrace": {
      "command": "memtrace",
      "args": ["mcp"],
      "env": { "MEMGRAPH_URL": "bolt://localhost:7687" }
    }
  },
  "enabledPlugins": {
    "memtrace-skills@memtrace": true
  },
  "extraKnownMarketplaces": {
    "memtrace": {
      "source": { "source": "github", "repo": "syncable-dev/memtrace" }
    }
  }
}
```

</details>

### Installing skills separately

```bash
npx memtrace-skills install     # install skills + register MCP
npx memtrace-skills uninstall   # remove everything
```

## Claude Desktop Setup

Skills are installed to `~/.claude/skills/` which is shared between Claude Code and Claude Desktop — both pick up skills automatically after `npm install -g memtrace`.

Add the MCP server to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "memtrace": {
      "command": "memtrace",
      "args": ["mcp"],
      "env": { "MEMGRAPH_URL": "bolt://localhost:7687" }
    }
  }
}
```

## Supported Languages

Rust · Go · TypeScript · JavaScript · Python · Java · C · C++ · C# · Swift · Kotlin · Ruby · PHP · Dart · Scala · Perl — and more via Tree-sitter.

## Requirements

| Dependency | Purpose |
|:-----------|:--------|
| **Memgraph** | Knowledge graph backend — auto-managed via `memtrace start` |
| **Node.js ≥ 18** | npm installation |
| **Git** | Temporal analysis (commit history) |

## Links

- [Documentation](https://memtrace.dev)
- [npm Package](https://www.npmjs.com/package/memtrace)
- [Report an Issue](https://github.com/syncable-dev/memtrace-public/issues)

---

<p align="center">
  <sub>Built by <a href="https://syncable.dev">Syncable</a> · <a href="LICENSE">Proprietary EULA</a> · Free to use</sub>
</p>
