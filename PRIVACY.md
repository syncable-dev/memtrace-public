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

**No source code, file contents, symbol names, embeddings, file paths, or AST data is ever transmitted to any external server.**

## What Leaves Your Machine

Memtrace makes exactly three types of network calls:

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
| **Data sent** | Aggregate integer counts only: total nodes, edges, episodes, repositories |
| **Purpose** | Usage metering and entitlement checks |
| **Frequency** | Every 15 minutes while running |

The heartbeat payload contains **no symbol names, no file paths, no code, and no embeddings** — only integer totals like `{ "totalNodes": 4022, "totalEdges": 18441 }`.

### 3. Embedding Model Download (One-Time)

| | |
|:--|:--|
| **Source** | HuggingFace Hub (via the `fastembed` library) |
| **Data sent** | Nothing — this is an inbound download only |
| **What's downloaded** | ONNX model weights (e.g., BGE-small-en-v1.5) |
| **Frequency** | Once on first run; cached at `~/.cache/fastembed/` |

## What We Don't Do

- ❌ We do not send source code to any server
- ❌ We do not use cloud-based embedding APIs (OpenAI, Cohere, etc.)
- ❌ We do not collect telemetry or analytics
- ❌ We do not track usage patterns beyond aggregate node/edge counts
- ❌ We do not store or transmit file paths, symbol names, or any structural data externally

## Questions?

If you have questions about data handling or need a security review for your organization, please [open an issue](https://github.com/syncable-dev/memtrace-public/issues) or contact us at support@syncable.dev.
