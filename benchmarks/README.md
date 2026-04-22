# Memtrace Benchmarks

Reproduction instructions and full numbers for every claim in the main README and the benchmark-overview image.

Two parallel harnesses live here:

- **`fair/`** — the original 1,000-query exact-symbol bench (Bench #0) preserved at the exact scripts that generated the headline table. Four tools, one dataset, one machine.
- **`suite/`** — the extended harness used for Benches #0–#4. Same `fair/` dataset for #0, plus new datasets (pyright call-hierarchy, Django NL queries, 50 incremental edits, scratch-fixture).

Both write per-query JSONL + a markdown rollup. The suite's rollups are the source of truth for the consolidated numbers below.

---

## The consolidated table

Same machine (Apple M3 Max, 14 cores, 36 GB), same checkout, same query order. Green = Memtrace wins the declared primary axis for that bench; amber = Memtrace trails.

| # | Bench | Corpus | Primary axis | Memtrace | Runner-up | Δ |
|:-:|:------|:-------|:-------------|---------:|:----------|---:|
| 0 | Exact-symbol lookup (1,000 queries) | mempalace | `acc_at_1_pct` | **96.7%** 🟢 | ChromaDB 62.3% | 1.55× |
| 1 | Token economy (same 1,000 queries) | mempalace | `acc_at_1_per_kilo_token` | **495.52** 🟢 | GitNexus 126.90 | 3.90× |
| 2 | Intent retrieval (100 NL PR titles) | Django | `recall_at_10` | 58.6% 🟡 | ChromaDB 66.8% | −8.2 pp |
| 3 | Graph queries — mempalace (pyright GT) | mempalace | `callers_of.recall` (filtered) | **0.851** 🟢 | CGC 0.584 | 1.46× |
| 3 | Graph queries — Django (pyright GT) | Django | `callers_of.recall` (filtered) | **0.816** 🟢 | GitNexus 0.053 | 15.4× |
| 4 | Incremental freshness (50 edits) | scratch-fixture | `time_to_queryable_p95` | **42.5 ms** 🟢 | CGC 613.7 ms | 14.4× faster |

Bench #5 (agent-level SWE completion) has a 5-task dataset and skeleton; the agent driver is gated behind `RUN_AGENT_BENCH=1` because it spends LLM credits and was not run in this pass.

**Honest losses:**

- **Bench #2** — Django intent retrieval is a natural-language semantic workload, which is what vector DBs are built for. Memtrace places 2nd on recall/precision while still beating GitNexus on tokens-per-hit (57.2 vs 219.8 recall/1k-tok → GitNexus wins that dimension specifically because its flow outputs are shorter).
- **Classical baselines** — in a separate Tier-3 run, `ctags` and `ripgrep` beat Memtrace on exact-symbol Acc@1 (96.6% / 96.4% vs 95.5%). They're purpose-built for this one workload, return no structure, and have nothing to say on Benches #2–#4. See [`suite/results/bench_0_full_with_baselines/`](../benchmarks/suite/) in an internal branch — not shipped publicly.

---

## Environment

- **Machine:** Apple M3 Max, 14 cores (10P + 4E), 36 GB RAM
- **OS:** macOS
- **Memtrace:** Rust release binary (`npm install -g memtrace` or `cargo build --release`)
- **ArcadeDB:** `arcadedata/arcadedb:latest` Docker container (auto-managed via `memtrace start`)
- **Corpora:**
  - [`mempalace`](https://github.com/mempalace/mempalace) — ~250 Python files (exact-symbol and graph benches)
  - [`django/django`](https://github.com/django/django) — ~3,300 Python files (intent + Django graph generalization)
  - `suite/corpora/scratch_fixture/` — 21 hand-authored files (incremental bench)

## Prerequisites

```bash
cd benchmarks
python -m venv .venv
./suite/scripts/bootstrap.sh       # installs chromadb, cgc, neo4j, pytest

npm install -g gitnexus            # GitNexus eval-server
npm install -g memtrace            # or: build from source, set MEMTRACE_BIN
```

Point the suite at your corpora:

```bash
export MEMPALACE_PATH=/path/to/mempalace    # Bench #0/#1/#3-mempalace
export DJANGO_PATH=/path/to/django          # Bench #2 + Bench #3-django
export MEMTRACE_BIN=$(which memtrace)       # else: auto-detect via which
export CGC_BIN=$(which cgc)                 # else: auto-detect via which
```

Index each corpus once:

```bash
memtrace start
memtrace index "$MEMPALACE_PATH"
memtrace index "$DJANGO_PATH"

cd "$MEMPALACE_PATH" && npx gitnexus analyze . && cd -
cgc index "$MEMPALACE_PATH"

# GitNexus eval-server runs persistent for query-time fairness
cd "$MEMPALACE_PATH" && npx gitnexus eval-server &
```

---

## Bench #0 — Exact-symbol lookup

Ground truth: Python's stdlib `ast.parse` over mempalace, 1,000 unique symbols (no symbols short or trivial, no reserved words). No tool's parser is involved in building the dataset.

**Two runners live here, both valid:**

```bash
# (a) fair/ — the original published harness that produced the headline numbers
.venv/bin/python fair/run_fair_benchmark.py       # writes fair/results.json

# (b) suite/ — same dataset, new contract, same tolerance
.venv/bin/python -m benchmarks.suite run \
    --bench 0 --adapters memtrace,chromadb,gitnexus,cgc
# writes suite/results/bench_0/rollup.md
```

Parity (`fair/` vs `suite/`, 1,000-query run, 2026-04-21, ±0.5% absolute tolerance):

| Adapter | suite Acc@1 | fair Acc@1 | suite Acc@5 | fair Acc@5 | suite latency | fair latency |
|:--------|:-----------:|:----------:|:-----------:|:----------:|:-------------:|:------------:|
| memtrace | 96.7% | 96.7% | 99.9% | 100.0% | 7.54 ms | 9.16 ms |
| chromadb | 62.2% | 62.3% | 85.9% | 86.1% | 57.1 ms | 58.5 ms |
| gitnexus | 27.1% | 27.1% | 89.7% | 89.7% | 167.7 ms | 191.2 ms |
| cgc      |  6.4% |  6.4% | 66.0% | 66.4% | 1,452.8 ms | 1,627.2 ms |

Headline numbers cite `fair/` because that's the audit trail. The suite matches within tolerance.

Scoring:

- **Coverage** = fraction of queries the adapter returned any result for.
- **Acc@k** = expected file appeared in the top-k ranked paths. Scoring is path-based, not name-based, so path-normalisation is key — `adapters/` implements the per-tool extraction.
- **Latency** = wall-clock `time.time()` per query including all protocol overhead (MCP JSON-RPC / HTTP / subprocess spawn / in-process).
- **Tokens** = `len(response_text) // 4` — how much context the response would eat in an LLM window.

---

## Bench #1 — Token economy

Same 1,000 queries as Bench #0, re-scored through the lens that matters most to MCP coding agents: **how many correct top-1 hits do you get per 1,000 response tokens?**

Primary axis: `acc_at_1_per_kilo_token` = `acc_at_1_pct ÷ (avg_tokens ÷ 1000)`.

| Adapter | Acc@1 | Avg tokens | Tokens/hit | **Acc@1/k-tok** | SNR |
|:--------|------:|-----------:|-----------:|----------------:|----:|
| **memtrace** | 96.7% | 195 | 202 | **495.52** | 86.7% |
| gitnexus     | 27.1% | 214 | 788 | 126.90 | 19.2% |
| chromadb     | 62.2% | 1,937 | 3,114 | 32.11 | 62.6% |
| cgc          |  6.4% | 221 | 3,452 | 28.97 |  9.4% |

SNR = fraction of response tokens that were part of a correct top-1 hit.

Reproduce from the committed Bench #0 JSONL:

```bash
.venv/bin/python -c "
from pathlib import Path
from benchmarks.suite.benches.bench_1_snr_mrr.run import run_from_bench_0_jsonl
combined = next(Path('suite/results/bench_0').glob('combined-*.jsonl'))
run_from_bench_0_jsonl(combined, Path('suite/results/bench_1_token_economy'))
"
cat suite/results/bench_1_token_economy/rollup.md
```

---

## Bench #2 — Intent retrieval (Django, NL → files)

**Semantic workload — this is ChromaDB's home turf.** 100 natural-language queries drawn from Django PR titles, scoring file-level recall@10 / precision@10.

| Adapter | Recall@10 | Precision@10 | Coverage | Recall / 1k tokens |
|:--------|----------:|-------------:|---------:|-------------------:|
| **chromadb** 🏆 | **66.8%** | **13.1%** | 100% | 56 |
| memtrace         | 58.6% (−8.2 pp) | 11.3% | 100% (tie) | 57.2 (2nd) |
| gitnexus         | N/A | N/A | — | 219.8 (wins this slice) |

Memtrace places 2nd on the primary axis. **This is documented in the SVG; we don't claim it as a win.** Memtrace's BM25 + symbol-embedding + RRF is competitive but not better than purpose-built semantic vectors on this workload.

The reason Memtrace is close at all: symbol embeddings + Tantivy BM25 over docstrings + Reciprocal Rank Fusion gives it genuine semantic recall. The reason it's not ahead: ChromaDB embeds 800-char chunks that include surrounding code context, which helps PR-title-style queries. Memtrace's structural strengths (graph traversal, impact, temporal) don't help here.

Reproduce:

```bash
.venv/bin/python -m benchmarks.suite.benches.bench_2_intent_retrieval.driver
# results in suite/results/bench_2_intent_django/
```

---

## Bench #3 — Graph queries (pyright ground truth)

200 symbols from mempalace, 31 from Django. For each symbol we ask pyright (LSP `callHierarchy/incomingCalls` + `outgoingCalls`) for the gold caller / callee set, then ask every adapter the same question via its own graph API (Memtrace `analyze_relationships`, GitNexus flow-text, CGC `analyze callers/calls`). Recall / precision / F1 computed by symbol-name set intersection.

**Filtered** rows only include symbols whose pyright gold is non-empty on that axis — otherwise every adapter scores 1.0 on empty sets and the average is noise.

### mempalace (~250 files · fair extractors)

| Adapter | Callers recall (N=70) | Callers precision | Callees recall (N=193) | Impact recall (N=70) | Avg latency |
|:--------|---:|---:|---:|---:|---:|
| **memtrace** 🏆 | **0.851** | **0.347** | **0.429** | **0.874** | 243 ms |
| cgc              | 0.584 | 0.214 | 0.326 | *not impl.* | 463 ms |
| gitnexus         | 0.013 | 0.003 | 0.027 | 0.007 | 195 ms |
| chromadb         | `NotSupported` | — | — | — | — |

### Django (~3,300 files · 13× larger corpus)

| Adapter | Callers recall (N=19) | Callees recall (N=25) | Impact recall (N=19) | Avg latency |
|:--------|---:|---:|---:|---:|
| **memtrace** 🏆 | **0.816** | **0.169** | **0.751** | 2,104 ms |
| gitnexus | 0.053 | 0.000 | 0.053 | 378 ms |
| cgc      | 0.000 | 0.000 | 0.000 | 455 ms |
| chromadb | `NotSupported` | — | — | — |

**Generalization verified** — the Memtrace graph win is not mempalace-specific.

Two critical fixes went into these numbers, disclosed honestly:

1. **Memtrace Cypher anchor fix** (`relationships.rs:108-110` — 2026-04-22) — the inbound traversal from a variable-length `CALLS*1..3` path was anchored at the END node, which ArcadeDB's planner cannot backward-chain. Flipping the anchor to start at the target took Memtrace callers recall from **0.000 → 0.851**.

2. **Fair GitNexus + CGC extractors** — the initial adapters pulled only file paths from the free-text / Rich-table output. Bench #3 scores by symbol NAMES, so path-only extraction scored 0.000 everywhere. Rewrote the GitNexus flow-text parser (`Symbol <name> → <file>` pairs) and the CGC table parser (first `Caller Function` column, `COLUMNS=400` to defeat Rich truncation). This is what revealed CGC's real 0.584 on mempalace callers — previously reported as 0.000 due to the parser bug, not the algorithm.

Reproduce:

```bash
# mempalace (generate gold if not present)
.venv/bin/python suite/datasets/generators/pyright_graph.py
.venv/bin/python -m benchmarks.suite.benches.bench_3_graph_queries.driver

# Django
.venv/bin/python suite/datasets/generators/pyright_graph_django.py
.venv/bin/python -m benchmarks.suite.benches.bench_3_graph_queries.driver_django
```

---

## Bench #4 — Incremental freshness

50 deterministic edits (add / rename / move / delete) applied to a hand-authored 21-file `scratch_fixture` task-queue corpus. Primary axis: `time_to_queryable_p95` — wall-time from file change to first successful re-query (lower is better, 5,000 ms deadline).

| Adapter | Queryable | **p95** | p50 | Reindex p50 | Staleness | Errors |
|:--------|:---------:|--------:|----:|------------:|:---------:|-------:|
| **memtrace** 🏆 | 97.8% | **42.5 ms** | 20.7 ms | 1,168 ms | 40.0% | 0 |
| cgc              | 100.0% | 613.7 ms | 519.8 ms | 1,127 ms | 34.0% | 0 |
| chromadb         | 76.1% | 5,001 ms (timeout) | 55.7 ms | 66.8 ms | 68.0% | 0 |
| gitnexus         | 0% (`NotSupported`) | N/A | N/A | — | — | 0 |

- **Memtrace** — `index_directory(incremental=true, skip_embed=true)` re-parses only the changed files and their dependents.
- **ChromaDB** — re-embeds chunks of touched files, but the renamed symbol rarely appears in the top-k vector-similar chunks (24% never become queryable before the 5s deadline).
- **GitNexus** eval-server is batch-only — it returns `NotSupported` across all 50 edits. That IS the Bench #4 story for GitNexus.
- **CGC** `cgc index <file>` works per-file but reboots FalkorDB on each call (~1.1 s p50). Solid 614 ms p95.

**Staleness caveat for Memtrace (40%):** renames with `incremental=true, clear_existing=false` leave the pre-rename `CodeNode` in the graph — `find_symbol(old_name)` still resolves until a full re-index drops the orphans. This is the honest trade for sub-50-ms freshness. An orphan-sweep pass on incremental reindex is the obvious next optimisation.

Reproduce:

```bash
.venv/bin/python -m benchmarks.suite.benches.bench_4_incremental.driver
cat suite/results/bench_4_full/rollup.md
```

---

## Bench #5 — Agent-level SWE completion (skeleton only)

5 SWE-bench-style tasks against mempalace (`suite/datasets/bench_5_tasks.json`). Each task names a reproduction bug, a golden test command, and a repo path. An agent runs against each system's MCP server and its completion rate + LLM cost are scored.

**Not run in this pass** — gated behind `RUN_AGENT_BENCH=1` to prevent accidental API-credit burn. Skeleton lives in `suite/benches/bench_5_agent_level/`.

---

## Why `fair/` and `suite/` coexist

`fair/` is preserved unchanged so the published numbers remain reproducible at the exact script that generated them. The suite is the forward-going framework: same dataset, new adapter contract (`NotSupported` is first-class data, graph + incremental methods, JSONL everywhere), new reporting format.

A ±0.5% absolute-accuracy parity tolerance bridges the two — any drift beyond that in either harness would be investigated.

## Methodology notes

- **Accuracy** — every adapter's response is normalised to a list of file paths (Bench #0/#1) or a set of symbol names (Bench #3). Tools that return only symbol names (CGC for Bench #0) get a grep fallback so a format mismatch doesn't get mis-scored as a miss. Every adapter is disclosed in its source file with its exact call shape.
- **Latency** — wall-clock `time.time()` per query. No warm-up runs excluded.
- **Tokens** — `len(response_text) // 4`. Approximates what the agent would actually send to the LLM.
- **Indexing speed (headline)** — `/usr/bin/time -p memtrace index …`. Memtrace's headline ~4-second time does NOT include async embedding (runs in background after graph write; the graph is queryable immediately after the timed portion).
- **No cherry-picking** — losses are documented alongside wins. Bench #2 (semantic NL) is the standing 2nd-place result. Classical baselines (ctags / ripgrep) beat Memtrace on exact-symbol by ~1 pp Acc@1 in a separate internal run; they have nothing to say on #2–#4.

## Output layout

```
benchmarks/
├── fair/                           # original Bench #0 harness (kept frozen)
│   ├── run_fair_benchmark.py
│   ├── extract_ground_truth.py
│   ├── dataset.json                # 1,000 queries, committed
│   ├── results.json                # committed snapshot
│   └── README.md
└── suite/
    ├── contract.py                 # Adapter base + QueryResult / GraphResult / NotSupported
    ├── runner.py                   # Bench-agnostic harness
    ├── scoring.py                  # Pure metrics (acc_at_k, mrr, snr, recall, precision, …)
    ├── reporting.py                # JSONL → markdown / CSV rollups
    ├── adapters/                   # memtrace.py, chromadb.py, gitnexus.py, cgc.py
    ├── benches/
    │   ├── bench_0_exact_symbol/
    │   ├── bench_1_snr_mrr/
    │   ├── bench_3_graph_queries/  # driver.py + driver_django.py
    │   ├── bench_4_incremental/    # driver.py + edits.py
    │   └── bench_5_agent_level/    # skeleton
    ├── corpora/
    │   ├── mempalace.py            # honors MEMPALACE_PATH
    │   ├── django.py               # honors DJANGO_PATH
    │   └── scratch_fixture/        # 21-file task-queue corpus
    ├── datasets/                   # committed gold: bench_3_graph.json, bench_3_graph_django.json, bench_4_edits.json, bench_5_tasks.json
    ├── versions.toml               # pinned adapter + service versions
    └── tests/                      # unit + integration tests (marker-gated)
```
