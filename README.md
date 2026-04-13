<p align="center">
  <img src="assets/logo.svg" alt="Memtrace" width="100" height="100" />
</p>

<h1 align="center">Memtrace</h1>

<p align="center">
  <strong>The persistent memory layer for coding agents.</strong><br/>
  A bi-temporal, episodic, structural knowledge graph — built from AST, not guesswork.
</p>

<p align="center">
  <a href="https://www.npmjs.com/package/memtrace"><img src="https://img.shields.io/npm/v/memtrace?style=flat-square&color=00D4B8&label=npm" alt="npm version" /></a>
  <a href="https://github.com/syncable-dev/memtrace-public/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Proprietary%20EULA-0A1628?style=flat-square" alt="license" /></a>
  <a href="https://memtrace.dev"><img src="https://img.shields.io/badge/docs-memtrace.dev-00D4B8?style=flat-square" alt="docs" /></a>
</p>

> **Early Access** — Memtrace is under active development. Core indexing and structural search are stable. Temporal features (evolution scoring, timeline replay) are functional but may have rough edges. [Report issues here.](https://github.com/syncable-dev/memtrace-public/issues)

---


Memtrace gives coding agents something they've never had: **structural memory**. Not vector similarity. Not semantic chunking. A real knowledge graph compiled from your codebase's AST — where every function, class, interface, and API endpoint exists as a node with deterministic, typed relationships.

Index once. Every agent query after that resolves through graph traversal — callers, callees, implementations, imports, blast radius, temporal evolution — in milliseconds, with zero token waste.

```bash
npm install -g memtrace    # binary + 12 skills + MCP server — one command
memtrace start             # launches the graph database
memtrace index .           # indexes your codebase in seconds
```

That's it. Claude picks up the skills and MCP tools automatically.

https://github.com/user-attachments/assets/e7d6a1e9-c912-4e65-a421-bd0256dffa5a

> Built-in UI at `localhost:3030` — explore your graph, trace dependencies, spot dead code, and visualize architecture at a glance

---

## Why Memtrace Exists

Good code intelligence tools already exist. GitNexus and CodeGrapherContext build AST-based graphs with symbol relationships, and they work well for understanding what's in your codebase *right now*.

Memtrace is a **bi-temporal episodic structural knowledge graph**. It builds on that same AST foundation and adds two dimensions:

- **Temporal memory** — every symbol carries its full version history. Agents can reason about *what changed*, *when it changed*, and *how the architecture evolved* — not just what exists today. Six scoring algorithms (impact, novelty, recency, directional, compound, overview) let agents ask different temporal questions.
- **Cross-service API topology** — Memtrace maps HTTP call graphs between repositories, detecting which services call which endpoints across your architecture.

On top of that, the structural layer is comprehensive:

- **Symbols are nodes** — functions, classes, interfaces, types, endpoints
- **Relationships are edges** — `CALLS`, `IMPLEMENTS`, `IMPORTS`, `EXPORTS`, `CONTAINS`
- **Community detection** — Louvain algorithm identifies architectural modules automatically
- **Hybrid search** — Tantivy BM25 + vector embeddings + Reciprocal Rank Fusion, all on top of the graph
- **Rust-native** — compiled binary, no Python/JS runtime overhead, sub-15ms average query latency

The agent doesn't just search your code. It *remembers* it.

## Benchmarks

All benchmarks run on the same machine, same codebase, same queries. No cherry-picking.

### Does it find the right thing?

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/benchmarks/search-accuracy.svg"/>
  <source media="(prefers-color-scheme: light)" srcset="assets/benchmarks/search-accuracy.svg"/>
  <img alt="Search accuracy: Memtrace 97.3% vs ChromaDB 89.6% vs GitNexus 12.8%" src="assets/benchmarks/search-accuracy.svg" width="720"/>
</picture>

### How fast?

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/benchmarks/search-latency.svg"/>
  <source media="(prefers-color-scheme: light)" srcset="assets/benchmarks/search-latency.svg"/>
  <img alt="Search latency: Memtrace 13.4ms vs ChromaDB 60.6ms vs GitNexus 172.7ms vs CodeGrapher 510.5ms" src="assets/benchmarks/search-latency.svg" width="720"/>
</picture>

### How much context does it save?

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/benchmarks/token-context.svg"/>
  <source media="(prefers-color-scheme: light)" srcset="assets/benchmarks/token-context.svg"/>
  <img alt="Token usage: Memtrace 319K vs ChromaDB 1.91M — 83% reduction" src="assets/benchmarks/token-context.svg" width="720"/>
</picture>

### How long to set up?

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/benchmarks/indexing-speed.svg"/>
  <source media="(prefers-color-scheme: light)" srcset="assets/benchmarks/indexing-speed.svg"/>
  <img alt="Indexing: Memtrace 1.5s vs Graphiti 6h vs Mem0 31m" src="assets/benchmarks/indexing-speed.svg" width="720"/>
</picture>

<details>
<summary><strong>Memtrace vs. general memory systems (Mem0, Graphiti)</strong></summary>

<br/>

Mem0 and Graphiti are strong conversational memory engines designed for tracking entity knowledge (e.g. `User -> Likes -> Apples`). They excel at that. For code intelligence specifically, the tradeoff is that they rely on LLM inference to build their graphs — which adds cost and time when processing thousands of source files.

**Graphiti** processes data through `add_episode()`, which triggers multiple LLM calls per episode — entity extraction, relationship resolution, deduplication. At ~50 episodes/minute ([source](https://github.com/getzep/graphiti)), ingesting 1,500 code files takes **1–2 hours**.

**Mem0** processes data through `client.add()`, which queues async LLM extraction and conflict resolution per memory item ([source](https://mem0.ai)). Bulk ingestion with `infer=True` (default) means every file passes through an LLM pipeline. Throughput is bounded by your LLM provider's rate limits.

**Both** accumulate $10–50+ in API costs for large codebases because every relationship is inferred rather than parsed.

**Memtrace takes a different approach:** it indexes 1,500 files in 1.2–1.8 seconds for $0.00 — no LLM calls, no API costs, no rate limits. Native Tree-sitter AST parsers resolve deterministic symbol references (`CALLS`, `IMPLEMENTS`, `IMPORTS`) locally. The tradeoff is that Memtrace is purpose-built for code — it doesn't handle conversational entity memory the way Mem0 and Graphiti do.

</details>

<details>
<summary><strong>Memtrace vs. code graphers (GitNexus, CodeGrapherContext)</strong></summary>

<br/>

GitNexus and CodeGrapherContext both build AST-based code graphs with structural relationships — solid tools in the same space. Memtrace shares that foundation and extends it with temporal memory, API topology, and a Rust runtime:

| Capability | Memtrace | GitNexus | CodeGrapher |
|:-----------|:---------|:---------|:------------|
| AST-based graph | Yes | Yes | Yes |
| Structural relationships (CALLS, IMPLEMENTS, IMPORTS) | Yes | Yes | Yes |
| Bi-temporal version history per symbol | **Yes — 6 scoring modes** | Git-diff only | No |
| Cross-service HTTP API topology | **Yes** | No | No |
| Community detection (Louvain) | **Yes** | Yes | No |
| Hybrid search (BM25 + vector + RRF) | **Yes — Tantivy + embeddings** | No | BM25 + optional embeddings |
| Language | **Rust (compiled binary)** | JavaScript | Python |
| Search accuracy (1K queries) | **97.3%** | 12.8% | 0%* |
| Query latency (1K queries) | **13.4 ms avg** | 172.7 ms avg | 510.5 ms avg |
| Tokens per query | **319 avg** | 254 avg | 23 avg |
| Index time (1,500 files) | **1.5 sec** | 10.5 sec | ~3.5 min |

*CGC's 0% reflects an output format mismatch — it returns symbol names without file paths, so our Acc@1 evaluator can't match them. CGC likely finds relevant symbols; the metric just can't confirm it. All numbers from [live benchmark](benchmarks/) on the same machine, same codebase, same 1,000 queries.

The latency difference is primarily Rust vs. interpreted runtimes, and Memgraph's Bolt protocol vs. HTTP/embedding pipelines. The feature difference is temporal memory and API topology — dimensions Memtrace adds on top of the shared AST-graph foundation.

</details>

## 25+ MCP Tools

Memtrace exposes a full structural toolkit via the [Model Context Protocol](https://modelcontextprotocol.io):

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
- `get_evolution` — 6 scoring modes (compound, impact, novel, recent, directional, overview)
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

## 12 Agent Skills

Memtrace ships skills that teach Claude *how* to use the graph. They fire automatically based on what you ask — no prompt engineering required.

| | Skill | You say... |
|:--|:------|:-----------|
| **Search** | `memtrace-search` | _"find this function"_, _"where is X defined"_ |
| **Relationships** | `memtrace-relationships` | _"who calls this"_, _"show class hierarchy"_ |
| **Evolution** | `memtrace-evolution` | _"what changed this week"_, _"how did this evolve"_ |
| **Impact** | `memtrace-impact` | _"what breaks if I change this"_, _"blast radius"_ |
| **Quality** | `memtrace-quality` | _"find dead code"_, _"complexity hotspots"_ |
| **Architecture** | `memtrace-graph` | _"show me the architecture"_, _"find bottlenecks"_ |
| **APIs** | `memtrace-api-topology` | _"list API endpoints"_, _"service dependencies"_ |
| **Index** | `memtrace-index` | _"index this project"_, _"parse this codebase"_ |

Plus **4 workflow skills** that chain multiple tools with decision logic:

| Skill | You say... |
|:------|:-----------|
| `memtrace-codebase-exploration` | _"I'm new to this project"_, _"give me an overview"_ |
| `memtrace-change-impact-analysis` | _"what will break if I refactor this"_ |
| `memtrace-incident-investigation` | _"something broke"_, _"root cause analysis"_ |
| `memtrace-refactoring-guide` | _"help me refactor"_, _"clean up tech debt"_ |

## Temporal Engine

Six scoring algorithms for different temporal questions:

| Mode | Best for |
|:-----|:---------|
| **`compound`** | General-purpose _"what changed?"_ — weighted blend of impact, novelty, recency |
| **`impact`** | _"What broke?"_ — ranks by blast radius (`in_degree^0.7 × (1 + out_degree)^0.3`) |
| **`novel`** | _"What's unexpected?"_ — anomaly detection via surprise scoring |
| **`recent`** | _"What changed near the incident?"_ — exponential time decay |
| **`directional`** | _"What was added vs removed?"_ — asymmetric scoring |
| **`overview`** | Quick module-level summary |

Uses **Structural Significance Budgeting** to surface the minimum set of changes covering ≥80% of total significance.

## Compatibility

| Editor / Agent | MCP Tools (25+) | Skills (12) | Install |
|:---------------|:---------------:|:-----------:|:--------|
| **Claude Code** | ✅ | ✅ | `npm install -g memtrace` — fully automatic |
| **Claude Desktop** | ✅ | ✅ | Automatic — shared with Claude Code |
| **Cursor** | ✅ | Coming soon | Add MCP server manually |
| **Windsurf** | ✅ | Coming soon | Add MCP server manually |
| **VS Code (Copilot)** | ✅ | — | Add MCP server manually |
| **Cline / Roo Code** | ✅ | — | Add MCP server manually |
| **Codex CLI** | ✅ | Coming soon | Add MCP server manually |
| **Any MCP client** | ✅ | — | Add MCP server manually |

> **MCP tools** work with any editor or agent that supports the [Model Context Protocol](https://modelcontextprotocol.io). **Skills** are Claude-specific workflow prompts that teach the agent *how* to chain tools — they require Claude Code or Claude Desktop.

## Setup

### Claude Code + Claude Desktop

`npm install -g memtrace` handles everything automatically — binary, 12 skills, MCP server, plugin, and marketplace all register in one command for both Claude Code and Claude Desktop.

For manual setup:

```bash
claude plugin marketplace add syncable-dev/memtrace
claude plugin install memtrace-skills@memtrace --scope user
claude mcp add memtrace -- memtrace mcp -e MEMGRAPH_URL=bolt://localhost:7687
```

### Other Editors (Cursor, Windsurf, VS Code, Cline)

After `npm install -g memtrace`, add the MCP server to your editor's config:

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

<details>
<summary>Config file locations by editor</summary>

| Editor | Config file |
|:-------|:------------|
| **Cursor** | `.cursor/mcp.json` in your project root |
| **Windsurf** | `~/.codeium/windsurf/mcp_config.json` |
| **VS Code (Copilot)** | `.vscode/mcp.json` in your project root |
| **Cline** | Cline MCP settings in the extension panel |

</details>

### Uninstall

```bash
memtrace uninstall              # removes skills, MCP server, plugin, and settings
npm uninstall -g memtrace       # removes the binary
```

Already ran `npm uninstall` first? The cleanup script is persisted at `~/.memtrace/uninstall.js`:

```bash
node ~/.memtrace/uninstall.js
```

## Languages

Rust · Go · TypeScript · JavaScript · Python · Java · C · C++ · C# · Swift · Kotlin · Ruby · PHP · Dart · Scala · Perl — and more via Tree-sitter.

## Requirements

| Dependency | Purpose |
|:-----------|:--------|
| **Memgraph** | Graph database — auto-managed via `memtrace start` |
| **Node.js ≥ 18** | npm installation |
| **Git** | Temporal analysis (commit history) |

<br/>

<p align="center">
  <a href="https://memtrace.dev">Documentation</a> · <a href="https://www.npmjs.com/package/memtrace">npm</a> · <a href="https://github.com/syncable-dev/memtrace-public/issues">Issues</a>
</p>

<p align="center">
  <sub>Built by <a href="https://syncable.dev">Syncable</a> · <a href="LICENSE">Proprietary EULA</a> · Free to use</sub>
</p>
