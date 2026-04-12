# Memtrace

Code intelligence graph — an MCP server that builds a persistent knowledge graph from your codebase, enabling structural search, relationship analysis, temporal evolution tracking, and architectural understanding.

## Quick Start

```bash
# Install globally via npm (installs binary + skills + MCP server config)
npm install -g memtrace

# Start Memgraph (required — the knowledge graph backend)
memtrace start

# Index your project
memtrace index /path/to/your/project

# Start the MCP server (for direct use)
memtrace mcp
```

After `npm install -g memtrace`, the installer automatically:

1. Installs the platform-specific memtrace binary
2. Installs 12 AI agent skills (8 commands + 4 workflows) to `~/.claude/skills/` and `~/.claude/plugins/cache/`
3. Registers the `memtrace` marketplace from GitHub (`syncable-dev/memtrace`) in `~/.claude/settings.json` for auto-updates
4. Enables the `memtrace-skills@memtrace` plugin in `enabledPlugins`
5. Registers the memtrace MCP server in `mcpServers`
6. Attempts `claude plugin marketplace add` + `claude plugin install` via CLI if available

Skills are bundled inside the npm package — no network fetch needed during install. The GitHub marketplace registration enables Claude to discover plugin updates automatically.

**No manual configuration needed** — just install and start exploring your codebase with Claude.

## Claude Code Setup

The `npm install -g memtrace` command handles everything automatically. It registers the marketplace, installs the plugin, adds the MCP server, and writes skills to `~/.claude/skills/`.

If you need to set it up manually, run these three commands:

```bash
# 1. Register the memtrace marketplace (tells Claude where to find plugin updates)
claude plugin marketplace add syncable-dev/memtrace

# 2. Install the skills plugin (writes skills + enables them)
claude plugin install memtrace-skills@memtrace --scope user

# 3. Register the MCP server (gives Claude access to memtrace tools)
claude mcp add memtrace -- memtrace mcp -e MEMGRAPH_URL=bolt://localhost:7687
```

This writes three things to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "memtrace": {
      "command": "memtrace",
      "args": ["mcp"],
      "env": {
        "MEMGRAPH_URL": "bolt://localhost:7687"
      }
    }
  },
  "enabledPlugins": {
    "memtrace-skills@memtrace": true
  },
  "extraKnownMarketplaces": {
    "memtrace": {
      "source": {
        "source": "github",
        "repo": "syncable-dev/memtrace"
      }
    }
  }
}
```

The `enabledPlugins` key activates the skill plugin. The `extraKnownMarketplaces` key registers the GitHub marketplace so Claude can auto-discover updates. Skills are cached at `~/.claude/plugins/cache/memtrace/memtrace-skills/<version>/skills/` and also written to `~/.claude/skills/` for broader compatibility.

### Skills (Installed Automatically)

Memtrace ships with 12 skills that teach Claude how to use the knowledge graph effectively:

**Command Skills** (single-tool wrappers with usage guidance):

| Skill | Triggers on |
|-------|-------------|
| `memtrace-index` | "index this project", "set up code intelligence", "parse this codebase" |
| `memtrace-search` | "find this function", "where is X defined", "search for Y" |
| `memtrace-relationships` | "who calls this", "what does this call", "show class hierarchy" |
| `memtrace-evolution` | "what changed", "how did this evolve", "what's different since last week" |
| `memtrace-impact` | "what will break if I change this", "blast radius", "risk assessment" |
| `memtrace-quality` | "find dead code", "complexity hotspots", "code smells" |
| `memtrace-graph` | "show architecture", "find bottlenecks", "important functions" |
| `memtrace-api-topology` | "list API endpoints", "service dependencies", "who calls this API" |

**Workflow Skills** (multi-step orchestrations):

| Skill | Triggers on |
|-------|-------------|
| `memtrace-codebase-exploration` | "explore this codebase", "I'm new to this project", "give me an overview" |
| `memtrace-change-impact-analysis` | "what will break if I refactor this", "pre-change risk assessment" |
| `memtrace-incident-investigation` | "something broke", "root cause analysis", "what went wrong" |
| `memtrace-refactoring-guide` | "help me refactor", "reduce complexity", "clean up tech debt" |

### Installing Skills Separately

If you installed memtrace via cargo or the skills weren't installed:

```bash
npx memtrace-skills install
```

To remove skills:

```bash
npx memtrace-skills uninstall
```

## Claude Desktop Setup

Skills are installed to `~/.claude/skills/` which is shared between Claude Code and Claude Desktop — both pick up memtrace skills after `npm install -g memtrace`.

The plugin is enabled via `~/.claude/settings.json` (the `enabledPlugins` and `extraKnownMarketplaces` keys written by the installer), which is also shared.

The only additional step for Claude Desktop is adding the MCP server to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "memtrace": {
      "command": "memtrace",
      "args": ["mcp"],
      "env": {
        "MEMGRAPH_URL": "bolt://localhost:7687"
      }
    }
  }
}
```

After that, Claude Desktop has both the skills (how to use memtrace) and the MCP tools (the actual capabilities).

## MCP Tools

Memtrace exposes 25+ tools via the MCP protocol:

**Indexing:** `index_directory`, `list_indexed_repositories`, `delete_repository`, `check_job_status`, `list_jobs`

**Search:** `find_code` (hybrid BM25 + semantic), `find_symbol` (exact/fuzzy name match)

**Relationships:** `analyze_relationships` (callers, callees, hierarchy, imports), `get_symbol_context` (360° view)

**Impact:** `get_impact` (blast radius), `detect_changes` (diff-based scope)

**Temporal:** `get_evolution` (6 scoring modes — compound, impact, novel, recent, directional, overview), `get_timeline` (symbol version history)

**Quality:** `find_dead_code`, `calculate_cyclomatic_complexity`, `find_most_complex_functions`, `get_repository_stats`

**Graph Algorithms:** `find_bridge_symbols` (betweenness centrality), `find_central_symbols` (PageRank), `list_communities` (Louvain), `list_processes`, `get_process_flow`

**API Topology:** `get_api_topology`, `find_api_endpoints`, `find_api_calls`, `link_repositories`

**Watch:** `watch_directory` (live incremental re-indexing), `list_watched_paths`

**Low-level:** `execute_cypher` (read-only Cypher queries)

## Evolution Scoring Modes

The `get_evolution` tool implements six distinct temporal analysis algorithms:

| Mode | Formula | Use Case |
|------|---------|----------|
| `compound` | `0.50×rank(impact) + 0.35×rank(novel) + 0.15×rank(recent)` | General-purpose "what changed?" |
| `impact` | `sig(n) = in_degree^0.7 × (1 + out_degree)^0.3` | "What broke?" — largest blast radius |
| `novel` | `surprise(n) = (1 + in_degree) / (1 + change_freq_90d)` | "What's unexpected?" — anomaly detection |
| `recent` | `impact × exp(−0.5 × Δhours)` | "What changed near the incident?" |
| `directional` | Asymmetric (added→out_degree, removed→in_degree) | "What was added vs removed?" |
| `overview` | Module-level rollup only | Quick summary |

## Requirements

- **Memgraph** — knowledge graph backend (auto-managed via `memtrace start`, or bring your own)
- **Node.js ≥ 18** — for npm installation
- **Git** — for temporal analysis (commit history)

## Development

```bash
# Build from source
cargo build --release --bin memtrace

# Run the MCP server
cargo run --release --bin memtrace -- mcp

# Docker Compose (Memgraph + memtrace-mcp)
docker-compose up
```

## License

FSL-1.1-MIT
