# Privacy & Data Handling

> **TL;DR — Memtrace runs entirely on your machine. Your source code never leaves it.**

## What Memtrace Does Locally

Memtrace builds a structural knowledge graph from your codebase's AST. Every step happens on your machine:

| Step | Where it runs | What it processes |
|:-----|:-------------|:-----------------|
| **AST parsing** | Local (Tree-sitter, compiled into the binary) | Source files → symbol nodes |
| **Graph construction** | Local (MemDB, embedded or self-hosted) | Nodes + edges (CALLS, IMPLEMENTS, IMPORTS) |
| **Vector embeddings** | Local (ONNX Runtime via fastembed — CoreML on Apple Silicon, CPU elsewhere) | Symbol signatures → vectors stored in local MemDB |
| **Full-text search** | Local (Tantivy BM25 index on disk) | Symbol names + signatures |
| **Git history analysis** | Local (libgit2, vendored) | Commit history → bi-temporal graph |
| **MCP tool queries** | Local (graph traversal + search) | Results returned to your local MCP client |

**No source code, file contents, symbol names, embeddings, or AST data is ever transmitted to any external server.** Sanitised error and crash telemetry can include path fragments; its limits are documented below and in the compliance datasheet.

## What Leaves Your Machine

Memtrace makes exactly four types of network calls:

### 1. License Authentication

| | |
|:--|:--|
| **Endpoint** | `POST https://www.memtrace.io/api/device/auth` |
| **Data sent** | License key (`MTC-COM-...`) + machine hostname |
| **Purpose** | Validate your license and obtain a session token |
| **Frequency** | On startup; refresh when session nears expiry |

### 2. Usage Heartbeat

| | |
|:--|:--|
| **Endpoint** | `POST https://www.memtrace.io/api/device/heartbeat` |
| **Data sent** | Aggregate node, edge, and episode counters; billable query usage; organization and seat identity for organization installs; MemDB endpoint, deployment mode, and aggregate record count for MemDB deployments |
| **Purpose** | Usage metering and entitlement checks |
| **Frequency** | Every 15 minutes while running |

The heartbeat payload contains **no repository names, repository paths, remote URLs, branches, source code, file or symbol names, query text, decision content, or activity-event feed**. The account dashboard can therefore show aggregate usage and authenticated install status, but it cannot reconstruct named codebase activity.

### 3. Embedding Model Download (One-Time)

| | |
|:--|:--|
| **Source** | HuggingFace Hub (via the `fastembed` library) |
| **Data sent** | Nothing — this is an inbound download only |
| **What's downloaded** | ONNX model weights (e.g., BGE-small-en-v1.5) |
| **Frequency** | Once on first run; cached at `~/.cache/fastembed/` |

### 4. Product Telemetry (since v0.3.17)

| | |
|:--|:--|
| **Endpoint** | `POST https://memtrace.io/api/telemetry/ingest` |
| **Data sent** | App-start events, indexing/embedding durations, aggregate PR review/watch counters, panic reports, and `WARN`/`ERROR` log lines from Memtrace's own crates — **all sanitised** to strip home-dir paths, token-shaped strings, and email addresses. Plus content-free **Rail routing-quality** buckets (mode, pattern *shape*, hit/miss, a bucketed score, and a local relevance yes/no) — never the search text or which files matched. The Rail buckets are measured **asynchronously by the background daemon**, so they never add latency to a search |
| **Purpose** | Catch crashes and regressions across the user base (the M3-Air "stuck on Loading embedding model" hang, Windows MSVC build failures, etc. are exactly the kind of thing this is for); and, for Rail, measure whether graph-backed search results are relevant — so the decision to make Rail active by default is backed by real evidence |
| **Frequency** | Batched flush every 60 seconds while running |
| **Opt-out** | `MEMTRACE_TELEMETRY=off` disables all of it (also `0`/`false`/`disabled`/`no`); `MEMTRACE_RAIL_SHADOW=off` disables just the Rail buckets; `MEMTRACE_RAIL_SHADOW_SAMPLE=0..1` bounds the background measurement rate |

The telemetry payload **never** contains source code, file contents,
symbol names, embeddings, repository paths, the text of your search
commands, which files or symbols a search matched, GitHub PR URLs, PR
discussion text, reviewer identities, branch names, or commit
data. The schema on the receiving end has no column to hold any of
those — we'd have to ship a new release to even start collecting them,
and we'd announce it here first. Full breakdown: [TELEMETRY.md](TELEMETRY.md).

## What We Don't Do

- ❌ We do not send source code to any server
- ❌ We do not use cloud-based embedding APIs (OpenAI, Cohere, etc.)
- ❌ We do not transmit symbol names, file paths, or any structural data
  outside the sanitised crash/error/event payloads documented above
- ❌ We do not store or share IP addresses (standard request logs are
  kept 7 days for abuse mitigation only)
- ❌ We do not sell, share, or publish anonymised aggregates of
  telemetry data without notice

## Questions?

If you have questions about data handling or need a security review for your organization, please [open an issue](https://github.com/syncable-dev/memtrace-public/issues) or contact us at support@syncable.dev.
