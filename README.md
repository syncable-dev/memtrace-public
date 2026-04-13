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

---

Memtrace gives coding agents something they've never had: **structural memory**. Not vector similarity. Not semantic chunking. A real knowledge graph compiled from your codebase's AST — where every function, class, interface, and API endpoint exists as a node with deterministic, typed relationships.

Index once. Every agent query after that resolves through graph traversal — callers, callees, implementations, imports, blast radius, temporal evolution — in milliseconds, with zero token waste.

```bash
npm install -g memtrace    # binary + 12 skills + MCP server — one command
memtrace start             # launches the graph database
memtrace index .           # indexes your codebase in seconds
```

That's it. Claude picks up the skills and MCP tools automatically.

---

## Why Memtrace Exists

Static code graphs exist. Tools like GitNexus and CodeGrapherContext build AST-based graphs with symbol relationships — and they're useful. But they solve one dimension: *what exists right now*.

Memtrace is a **bi-temporal episodic structural knowledge graph**. It adds two dimensions no other code intelligence tool has:

- **Temporal memory** — every symbol carries its full version history. Agents can reason about *what changed*, *when it changed*, and *how the architecture evolved* — not just what exists today. Six scoring algorithms (impact, novelty, recency, directional, compound, overview) let agents ask different temporal questions.
- **Cross-service API topology** — Memtrace maps HTTP call graphs between repositories, detecting which services call which endpoints. No other code grapher does inter-service relationship mapping.

On top of that, the structural layer is comprehensive:

- **Symbols are nodes** — functions, classes, interfaces, types, endpoints
- **Relationships are edges** — `CALLS`, `IMPLEMENTS`, `IMPORTS`, `EXPORTS`, `CONTAINS`
- **Community detection** — Louvain algorithm identifies architectural modules automatically
- **Hybrid search** — Tantivy BM25 + vector embeddings + Reciprocal Rank Fusion, all on top of the graph
- **Rust-native** — compiled binary, no Python/JS runtime overhead, single-digit millisecond queries

The agent doesn't just search your code. It *remembers* it.

## Benchmarks

All benchmarks run on the same machine, same codebase, same queries. No cherry-picking.

### Does it find the right thing?

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/benchmarks/search-accuracy.svg"/>
  <source media="(prefers-color-scheme: light)" srcset="assets/benchmarks/search-accuracy.svg"/>
  <img alt="Search accuracy: Memtrace 83.5% vs Vector RAG 25.8%" src="assets/benchmarks/search-accuracy.svg" width="720"/>
</picture>

### How fast?

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/benchmarks/search-latency.svg"/>
  <source media="(prefers-color-scheme: light)" srcset="assets/benchmarks/search-latency.svg"/>
  <img alt="Search latency: Memtrace 4.6ms vs GitNexus 220ms vs CodeGrapher 466.7ms" src="assets/benchmarks/search-latency.svg" width="720"/>
</picture>

### How much context does it save?

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/benchmarks/token-context.svg"/>
  <source media="(prefers-color-scheme: light)" srcset="assets/benchmarks/token-context.svg"/>
  <img alt="Token usage: Memtrace 284K vs Vector RAG 2.4M — 88.2% reduction" src="assets/benchmarks/token-context.svg" width="720"/>
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

Mem0 and Graphiti are excellent conversational memory engines for tracking entity knowledge (e.g. `User -> Likes -> Apples`). They are **architecturally unsuited for code intelligence** because they require LLM inference to build their graphs.

**Graphiti** processes data through `add_episode()`, which triggers multiple LLM calls per episode — entity extraction, relationship resolution, deduplication. At ~50 episodes/minute ([source](https://github.com/getzep/graphiti)), ingesting 1,500 code files takes **1–2 hours**. Every episode costs LLM tokens.

**Mem0** processes data through `client.add()`, which queues async LLM extraction and conflict resolution per memory item ([source](https://mem0.ai)). Bulk ingestion with `infer=True` (default) means every file passes through an LLM distillation pipeline. Throughput is bounded by your LLM provider's rate limits.

**Both** accumulate $10–50+ in API costs because they use LLMs to *guess* code relationships rather than parsing them deterministically.

**Memtrace indexes 1,500 files in 1.2–1.8 seconds for $0.00** — no LLM calls, no API costs, no rate limits. Native Tree-sitter AST parsers resolve deterministic symbol references (`CALLS`, `IMPLEMENTS`, `IMPORTS`) locally.

</details>

<details>
<summary><strong>Memtrace vs. code graphers (GitNexus, CodeGrapherContext)</strong></summary>

<br/>

GitNexus and CodeGrapherContext both build AST-based code graphs with structural relationships — they're real tools solving real problems. Here's what Memtrace adds:

| Capability | Memtrace | GitNexus | CodeGrapher |
|:-----------|:---------|:---------|:------------|
| AST-based graph | Yes | Yes | Yes |
| Structural relationships (CALLS, IMPLEMENTS, IMPORTS) | Yes | Yes | Yes |
| Bi-temporal version history per symbol | **Yes — 6 scoring modes** | Git-diff only | No |
| Cross-service HTTP API topology | **Yes** | No | No |
| Community detection (Louvain) | **Yes** | Yes | No |
| Hybrid search (BM25 + vector + RRF) | **Yes — Tantivy + embeddings** | No | BM25 + optional embeddings |
| Language | **Rust (compiled binary)** | JavaScript | Python |
| Query latency (1K queries) | **4.6 ms avg** | 220 ms avg | 466.7 ms avg |
| Index time (1,500 files) | **1.5 sec** | 10.5 sec | 3.5 min |

The speed difference comes from Rust vs. interpreted runtimes, and Memgraph's Bolt protocol vs. HTTP/embedding pipelines. The feature difference is temporal memory and API topology — dimensions that don't exist in static-snapshot graphs.

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

## Setup

### Claude Code

`npm install -g memtrace` handles everything automatically. For manual setup:

```bash
claude plugin marketplace add syncable-dev/memtrace
claude plugin install memtrace-skills@memtrace --scope user
claude mcp add memtrace -- memtrace mcp -e MEMGRAPH_URL=bolt://localhost:7687
```

<details>
<summary>What this writes to <code>~/.claude/settings.json</code></summary>

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

### Claude Desktop

Skills and plugins are shared between Claude Code and Claude Desktop — both activate after `npm install -g memtrace`. Add the MCP server to `claude_desktop_config.json`:

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
