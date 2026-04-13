# Memtrace Benchmarks

Reproduction instructions for all benchmark claims in the main README.

## Environment

- **Machine:** Apple M3 Max, 14 cores (10P + 4E), 36GB RAM
- **OS:** macOS
- **Memtrace:** Rust release binary (`target/release/memtrace`)
- **Memgraph:** Docker container, auto-managed via `memtrace start`
- **Target codebase:** [mempalace](https://github.com/mempalace/mempalace) (~1,500 files)

## Prerequisites

```bash
cd benchmarks
python -m venv .venv
.venv/bin/pip install neo4j chromadb sentence-transformers codegraphcontext
npm install -g gitnexus
```

## Environment Variables

All scripts use configurable paths via environment variables (with sensible defaults):

```bash
export MEMPALACE_DIR="$HOME/mempalace"   # path to the target codebase
export MEMTRACE_BIN="memtrace"           # path to memtrace binary (default: on PATH)
```

## Step 1: Index the target repository

```bash
# Memtrace
memtrace start
memtrace index /path/to/mempalace

# GitNexus
cd /path/to/mempalace && npx gitnexus analyze .

# CodeGrapherContext
cgc index /path/to/mempalace
```

## Step 2: Generate the dataset

Samples 1,000 unique symbols from the live Memgraph (mempalace-only, excluding trivial names):

```bash
.venv/bin/python datasets/generate_dataset.py
```

Output: `datasets/real_code_dataset.json`

## Step 3: Start competitor servers

```bash
# GitNexus eval-server (persistent HTTP — fair comparison, no CLI boot overhead)
cd /path/to/mempalace && npx gitnexus eval-server
```

## Step 4: Run the full benchmark

```bash
.venv/bin/python benchmark_full.py
```

This runs all four systems live:
- **ChromaDB** with default `all-MiniLM-L6-v2` sentence-transformer embeddings
- **Memtrace** via MCP JSON-RPC subprocess (real Rust binary)
- **GitNexus** via eval-server HTTP API (port 4848)
- **CodeGrapherContext** via CLI (`cgc find name`)

Output: `benchmark_results.json`

## Accuracy methodology

- **Acc@1:** "Does the expected file path appear anywhere in the system's response?"
- This favors systems that return file paths in their output. CGC's CLI returns symbol names without paths, which is why it scored 0% — it may be finding symbols, but the output format doesn't expose file paths for our evaluator to match.
- Memtrace returns full `file_path` fields in its MCP response.
- ChromaDB returns chunk metadata including the source file.
- GitNexus returns JSON with `filePath` fields.

## Latency methodology

- Wall-clock `time.time()` wrapping each query, including all protocol overhead (JSON-RPC for Memtrace, HTTP for GitNexus, subprocess for CGC, in-process for ChromaDB).
- No warm-up runs excluded. First query included in averages.

## Token methodology

- `len(response_text) // 4` — standard approximation of token count from character count.
- Measures how much context each system would inject into an LLM's context window per query.

## Indexing speed methodology

- Measured with `/usr/bin/time -p` on the command line.
- Measures wall-clock time from command start to completion.
- Memtrace's time does NOT include async embedding (runs in background after graph write). The graph is queryable immediately after the timed portion completes.
