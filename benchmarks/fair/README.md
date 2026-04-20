# Fair multi-tool code-search benchmark

A reproduction harness designed to avoid the usual footguns:

1. **Dataset is NOT derived from any tool's index.**  Ground truth comes from
   Python's stdlib `ast` module over mempalace source files.  Every tool
   competes on the same 1,000 queries against the same ground truth.

2. **Per-tool adapters normalise output** to a list of file paths.  Tools
   that don't emit file paths (CGC) get a grep-based fallback so a format
   mismatch doesn't get mis-counted as a search failure.

3. **Three orthogonal metrics** separate "did you index it?" from "did you
   rank it well?":

   - `coverage_pct` — tool returned *any* result for the query
   - `acc_at_1_pct` — the correct file was the top-ranked result
   - `acc_at_10_pct` — the correct file appeared anywhere in the top 10

## How to reproduce

```bash
# 1. Install everything (Memtrace, GitNexus, CGC, ChromaDB)
npm install -g memtrace gitnexus
cd benchmarks
python -m venv .venv
.venv/bin/pip install chromadb sentence-transformers codegraphcontext neo4j

# 2. Index mempalace with every tool
memtrace index /path/to/mempalace
cd /path/to/mempalace && npx gitnexus analyze .
cgc index /path/to/mempalace

# 3. Start GitNexus eval-server (persistent; needed by the adapter)
cd /path/to/mempalace && npx gitnexus eval-server &

# 4. Extract ground truth from mempalace source via Python AST
.venv/bin/python fair/extract_ground_truth.py   # writes fair/dataset.json

# 5. Run the fair benchmark
.venv/bin/python fair/run_fair_benchmark.py     # writes fair/results.json
```

Set `MAX_QUERIES=50` for a smoke test (~1 minute) before committing to the
full 1000-query run (~27 minutes total, dominated by CGC's CLI boot time).

## Results (1,000 queries on mempalace, 2026-04-20)

| Tool        | Coverage | Acc@1 | Acc@5 | Acc@10 | Avg lat | Tokens |
|:------------|---------:|------:|------:|-------:|--------:|-------:|
| **Memtrace** (ArcadeDB) | **100.0%** | **96.7%** | **100.0%** | **100.0%** |  **9.16 ms** |   195 |
| ChromaDB (all-MiniLM-L6-v2) | 100.0%  | 62.3%  |  86.1%  |  87.9%  |  58.46 ms | 1,937 |
| GitNexus (eval-server)      |  99.5%  | 27.1%  |  89.7%  |  89.9%  | 191.21 ms |   213 |
| CodeGrapherContext (CLI)    |  67.2%  |  6.4%  |  66.4%  |  66.7%  | 1627.17 ms |   221 |

### What the numbers mean, read honestly

- **Memtrace**'s 96.7% Acc@1 (100% Acc@10) is the top number.  The 3.3%
  Acc@1 losses are mostly name collisions where BM25 ranked a different
  function with the same name first — the correct file was still in the
  top-5 every time.  100% coverage comes from tree-sitter's broad
  extraction rules matching Python AST's for this corpus.

- **ChromaDB**'s 62.3% / 87.9% is what semantic embeddings look like on
  exact-symbol retrieval: the right chunk is almost always in the top-10,
  but rank-1 is probabilistic.  Token cost is ~10× higher because ChromaDB
  returns 800-char chunks, not symbol metadata.

- **GitNexus**'s 27.1% Acc@1 jumps to 89.9% Acc@10 because its response
  leads with execution *flows* (e.g. "Cmd_init → _canonical_lang") and
  pushes standalone definitions to a lower section.  The correct file is
  almost always returned — it's rarely first.  A benchmark that reported
  only Acc@1 (as our previous, unfair run did) would understate GitNexus
  by ~60 points.

- **CGC**'s 67.2% coverage means its parser indexed ~2/3 of the symbols
  Python's AST finds.  Among symbols it did index, top-10 hit rate is
  ~99% (conditional Acc@10 = 66.7% / 67.2% = 99.3%), so the "search
  quality" story is different from the "parser coverage" story.  The big
  latency figure is CGC's CLI re-initialising FalkorDB on every call —
  an operational limitation, not an algorithmic one.

### Why this is fair

- Ground truth is extracted by `ast.parse` from the CPython standard
  library.  No tool's parser (not Memtrace's tree-sitter, not GitNexus's
  AST walker, not CGC's extractor) was involved in building the dataset.

- Every adapter is disclosed in `run_fair_benchmark.py` with its exact
  call shape and post-processing.  The CGC adapter's grep fallback is
  explicitly flagged — if a tool returns only a symbol name we run a
  `grep -rln "def <name>|class <name>"` to recover the file path it
  implicitly refers to, rather than mark it a miss.

- Every system runs on the same host, same mempalace checkout, same
  query ordering.  Raw per-query results live in `results.json`.

### Where each tool shines

The fair benchmark measures *exact-symbol lookup*.  Different workloads
would produce different rankings:

| Workload                                 | Winner we'd expect |
|:-----------------------------------------|:-------------------|
| Exact symbol-name lookup (this bench)    | Memtrace (BM25 on name + structured response) |
| Natural-language query ("auth retry logic") | ChromaDB (semantic embeddings) |
| Cross-service API topology               | Memtrace (cross-repo HTTP edges) / GitNexus (flows) |
| "Who calls this?" (graph traversal)      | Memtrace / GitNexus / CGC (AST graph tools) |
| Typo'd query (`autheticate` → `authenticate`) | Memtrace (native `text.levenshteinDistance`) |
| Historical snapshot ("code as of 2 months ago") | Memtrace (bi-temporal) |

The numbers above are specifically the "exact-symbol lookup" column, not a
declaration that Memtrace wins every category.
