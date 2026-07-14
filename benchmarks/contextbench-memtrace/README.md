# ContextBench + Memtrace

This benchmark lane measures Memtrace on the official
[ContextBench](https://github.com/EuniAI/ContextBench) gold-context task set.
It emits ContextBench's unified trajectory format so the upstream evaluator,
not a Memtrace-owned scorer, computes file, symbol, span, line, and trajectory
metrics.

## Claims this lane can support

- **Retrieval-only:** Memtrace hybrid search plus graph expansion, with no LLM
  used to choose the final context.
- **Agent lift:** a paired run of the same coding agent and backbone with and
  without Memtrace. This is the result required for a fair Pass@1 claim.

`runner.py` implements retrieval-only evaluation. `agent_runner.py` implements
the end-to-end lane with ContextBench's vendored mini-SWE-agent, GPT-5, and the
official task images. It supports all four ContextBench families in the mixed
split: SWE-bench Verified, SWE-bench Pro, SWE-PolyBench, and Multi-SWE-Bench.

The agent lane keeps a task-isolated Memtrace MCP session available throughout
the trajectory through the `memtrace-agent` command mounted into the official
task container. It is not a one-shot context injection. Before submission the
agent must complete and audit an ordered hierarchy: broad search, listwise
2-8-file shortlist, file-scoped symbol search, targeted symbol graph traversal,
co-change/history lookup, and a final ranked 2-12-span file -> symbol -> line
slate. Placeholder queries, partial shortlist coverage, a single-file graph
walk, ungrounded ranges, and over-budget slates are rejected mechanically. A
protocol receipt gates the ordinary MiniSWE submission marker. The sealed
ranked slate is projected into the unmodified ContextBench trajectory format,
so a lossy after-the-fact `PATCH_CONTEXT` cannot discard the agent's evidence.

The runner can optionally use GPT-5 to select the compact final context from
Memtrace's retrieved candidates. This is a GPT-assisted retrieval result, not
yet a Pass@1 agent result:

```bash
python benchmarks/contextbench-memtrace/runner.py \
  --dataset /tmp/contextbench/data/contextbench_verified_train.parquet \
  --limit 10 \
  --selector-model gpt-5 \
  --rerank-model-dir /tmp/contextbench-rerank-model \
  --output /tmp/contextbench-memtrace/predictions-gpt5-10.jsonl
```

The runner intentionally fails if the real ONNX cross-encoder is missing; it
will not silently benchmark Memtrace's lexical fallback. The model directory
must contain `tokenizer.json` and either `model_int8.onnx` or `model.onnx`.
For Apple Silicon, the development slice used
`cross-encoder/ms-marco-MiniLM-L-12-v2`'s `onnx/model_qint8_arm64.onnx`, renamed
to `model_int8.onnx`.

## cb-r0 remediation flags (opt-in, ablation-friendly)

Four flag-gated policies address the cb-r0 failure taxonomy. Defaults
reproduce prior runs byte-for-byte; each flag is independently attributable
(env-var fallbacks in parentheses):

- `--pack-policy v2` (`CB_PACK_POLICY`): S1 discriminative-name scoring gate,
  pack-time junk filter, novel-line accounting, adaptive tight/consolidation
  packing regimes, cross-lane consensus blend, and budget-aware windowing.
  Replay-validated on the cb-r0 15-task slice: macro line F1 0.156 -> 0.264
  with zero per-task regressions ($0 lane, packer layer only).
- `--normalize-issue` (`CB_NORMALIZE_ISSUE=1`): decode JSON-string-encoded
  problem statements; collapse literal escape runs.
- `--query-strategy v2` (`CB_QUERY_STRATEGY`): strip issue-template
  boilerplate, one lane per markdown section covering the whole issue plus a
  tail lane, stack-trace frame queries (deepest repo-local frames first),
  full-text identifier extraction (superset of the v1 head-lane identifiers;
  only `@`-mention names are dropped), and a stemmed-title fallback lane for
  pure-prose issues.
- `--query-strategy v3`: apply cleaning to GPT and locked query plans too,
  recover explicitly backticked lowercase identifiers as exact lanes, request
  query-aware 40-line live-source windows from Memtrace, and use a compact
  title/code-name query for local ranking instead of a full issue dump.
- `--pack-policy v5`: retain v4's hard filler caps and BM25-rank slices inside
  oversized symbols, so implementation branches are not displaced by long
  docstrings from the same already-relevant symbol.
- `--search-limit 100` (`CB_SEARCH_LIMIT`): per-lane `find_code` depth.
  Recommended only together with `--pack-policy v2`, which handles the extra
  pool junk.

Recommended remediation stack for a scored run:

```bash
python benchmarks/contextbench-memtrace/runner.py ... \
  --pack-policy v5 --query-strategy v3 --search-limit 100
```

## Leaderboard-compatible reporting

The paper averages recall, precision, and per-task F1 across tasks. The
upstream evaluator's console summary currently prints micro-averaged recall
and precision and omits F1, so do not copy its summary directly into a
leaderboard row. Generate the paper-compatible row from its per-instance JSONL
output instead:

```bash
python benchmarks/contextbench-memtrace/report.py \
  --predictions /tmp/contextbench-memtrace/predictions.jsonl \
  --results /tmp/contextbench-memtrace/results.jsonl \
  --manifest /tmp/contextbench-memtrace/manifest.json \
  --audit-dir /tmp/contextbench-memtrace/predictions-audit \
  --model "Memtrace + GPT-5" \
  --output /tmp/contextbench-memtrace/leaderboard-report.json
```

The report emits the same columns as the live board: file, block, and line
recall/precision/F1; Pass@1; steps; lines per step; efficiency; redundancy;
and cost. `block` is ContextBench's definition-level AST-symbol metric.
Trajectory steps are individual Memtrace search or graph observations,
efficiency is macro line-level AUC-Coverage, and redundancy follows the
paper's mean per-step overlap formula rather than the evaluator's current
weighted-union implementation.

The manifest is the scoring denominator. By default the report refuses to
write an output if predictions or evaluator rows are missing, duplicated,
malformed, or outside that manifest. Explicit harness failures and evaluator
errors remain in the macro with zero metrics. `--allow-partial` is diagnostic
only: it zero-fills gaps and marks `completeness.score_publishable=false`.

Pass@1 remains `N/A` for this retrieval-only lane. It becomes valid only when
the run supplies generated patches and official task-test outcomes through
`--pass-results`. Cost is also `N/A` if any query plan was loaded without its
original planning-token record; the report will not present cached work as
free inference.

## End-to-end 50-task holdout

Put `OPENAI_API_KEY` in the repository-local `.env`; `/.env` is gitignored.
Then run the mixed 50-task holdout with the vendored ContextBench agent:

```bash
python benchmarks/contextbench-memtrace/agent_runner.py \
  --dataset /tmp/contextbench/data/contextbench_verified_test.parquet \
  --output-dir /tmp/contextbench-holdout-agent-50 \
  --work-dir /tmp/contextbench-holdout-agent-50/work \
  --contextbench-root /tmp/contextbench \
  --agent-python /tmp/contextbench-agent-venv/bin/python \
  --base-agent-config /tmp/contextbench/agent-frameworks/mini-swe-agent/multi-poly-pro-verified/configs/swebench_following_context.yaml \
  --rerank-model-dir /tmp/contextbench-rerank-model \
  --query-plan-file /tmp/contextbench-holdout-agent-50/query-plans.json \
  --graph-cache-dir /srv/contextbench/graph-cache \
  --cache-namespace <memtrace-build-and-embedding-schema-digest> \
  --selector-model gpt-5 \
  --agent-model openai/gpt-5 \
  --history-days 30 \
  --remove-new-images \
  --timeout 3600
```

The adapter resumes completed rows, records OpenAI usage and cost, and strips
generated build copies from patches while preserving source diffs. It uses the
task-specific `swebench_multi.yaml` configuration for Multi-SWE and the
following-context configuration for Verified, Pro, and PolyBench. The optional
image cleanup removes only images first pulled by the current task after its
patch and trajectory have been archived.

## Reproduction contract

1. Tune only on `contextbench_verified_train.parquet`. Lock the retrieval
   policy, evaluate once on `contextbench_verified_test.parquet`, then run the
   500-task `contextbench_verified.parquet` paper-aligned comparison and finally
   confirm on `full.parquet` (1,136 tasks).
2. Check out every repository at the row's exact `base_commit`.
3. Build a fresh trial-local Memtrace database with embeddings enabled. The
   retrieval-only lane defers history replay. History is optional rather than a
   benchmark requirement: the scored Codex lane uses `--history-days 0` and
   solves against current code memory at the exact base commit. A separate
   temporal-memory ablation may replay at most 30 days of ancestors reachable
   behind that snapshot; it never indexes or queries later commits.
4. Never expose `gold_context`, `patch`, `test_patch`, `f2p`, or `p2p` to the
   retrieval system.
5. Record every symbol span returned by search and graph expansion in
   `pred_steps`; record the agent's grounded, line-budgeted ranked slate in
   `pred_spans`.
6. Score with the unmodified upstream evaluator and archive predictions, audit
   records, versions, prompts/configuration, and raw evaluator output.
7. For agent claims, use the same model, temperature, token budget, task
   container, and number of attempts in the baseline and Memtrace arms.

The standalone runner and parallel driver default to an 80-line final-context
budget for local/legacy reproduction. The current scored AWS policy overrides
that default with `LINE_BUDGET=200` in the gitignored `aws/config.env`; the
effective value is recorded in run provenance and must be passed unchanged to
triage. The runner isolates each task under its own `work` directory and
removes the trial-local MemDB after retrieval by default. It does not attach
benchmark repositories to the developer's live Memtrace workspace.

## One-task smoke

```bash
git clone https://github.com/EuniAI/ContextBench.git /tmp/contextbench

python benchmarks/contextbench-memtrace/runner.py \
  --dataset /tmp/contextbench/data/contextbench_verified.parquet \
  --instance-id SWE-Bench-Verified__python__maintenance__bugfix__2e76c8cd \
  --output /tmp/contextbench-memtrace/predictions.jsonl

python -m contextbench.evaluate \
  --gold /tmp/contextbench/data/contextbench_verified.parquet \
  --pred /tmp/contextbench-memtrace/predictions.jsonl \
  --out /tmp/contextbench-memtrace/results.jsonl \
  --cache /tmp/contextbench-memtrace/eval-cache
```

Run the evaluator from the ContextBench checkout (or install its requirements
and add the checkout to `PYTHONPATH`). Its pinned `tree-sitter-languages`
dependency currently requires Python 3.11. The smoke task is Flask's non-empty
Blueprint-name issue; its gold context is `src/flask/blueprints.py:169-207`.
Treat it as a development/plumbing task and exclude it from any locked holdout
claim because its gold was inspected while building the adapter.

The unmodified upstream evaluator produced this development-only signal:

| Metric | Recall | Precision | F1 |
| --- | ---: | ---: | ---: |
| File context | 1.000 | 1.000 | 1.000 |
| Line context | 0.897 | 0.547 | 0.679 |

Trajectory AUC coverage was `1.000`. Indexing with the full local embedding
path took 137 seconds after checkout; retrieval took 1.47 seconds. The raw
evaluator record is committed as `smoke_result.json`.

## GPT-5 three-task pipeline slice

A corrected 2026-07-10 development slice exercised full embeddings, BM25 +
dense RRF, the real ONNX cross-encoder, GPT-5 query planning and structured
selection, and the unmodified upstream evaluator on Flask and two Requests
revisions. Its original summary used the evaluator's micro aggregation and
collapsed its observations into two synthetic steps, so its final-context
signal remains useful but its dynamics are not leaderboard-comparable.

A fresh metric-parity run recorded twelve real retrieval observations per
task, generated new query plans so cost was complete, and macro-averaged the
per-instance results. It produced file recall/precision/F1
`0.778/0.833/0.800`, block `0.667/0.556/0.600`, and line
`0.623/0.502/0.555`, with efficiency `0.816`, redundancy `0.687`, 172.5 lines
per step, and average GPT cost `$0.022`. This is a three-task plumbing check,
not a leaderboard claim.

The Flask task selected `Blueprint.__init__` and reached line F1 `0.769`; the
Requests Content-Length task selected the request-preparation methods and
reached line F1 `0.897`. The proxy-authentication task selected
`HTTPAdapter.get_connection`, `HTTPAdapter.proxy_headers`, and
`get_auth_from_url`, improving from zero to line F1 `0.606`.

An earlier `0.276` line result was invalidated during incident review. Its runner
restarted Memtrace between indexing and retrieval while the recovered HNSW had
only 615 of 1,232 durable vectors, left `MEMTRACE_RERANK` off, had no ONNX model,
and sent a long environment dump that consumed the reranker's 256-token window.
The runner now prevents those failure modes. The legacy micro summary is in
`gpt5_small_slice_result.json`; use `report.py` for future results.

## Gated scale-up

```bash
# 10-task signal check
python benchmarks/contextbench-memtrace/runner.py \
  --dataset /tmp/contextbench/data/contextbench_verified.parquet \
  --limit 10 \
  --output /tmp/contextbench-memtrace/predictions-10.jsonl

# Full verified retrieval run
python benchmarks/contextbench-memtrace/runner.py \
  --dataset /tmp/contextbench/data/contextbench_verified.parquet \
  --output /tmp/contextbench-memtrace/predictions-verified.jsonl
```

Do not launch the 500-task run until the 10-task official-evaluator result
beats the published GPT-5 line-context F1 (`0.312`) with acceptable index time
and failure rate. A credible public report should include bootstrap confidence
intervals and a per-language breakdown, not only the aggregate headline.

The runner now has a content-addressed graph cache keyed by repository URL,
base commit, and a caller-controlled schema namespace. Completed entries use
an atomic manifest and are reused only at the same canonical workspace mount.
Set the namespace to a digest of the Memtrace build, parser/ignore policy, and
embedding model. Cache reuse never includes issue text, gold context, patches,
or trajectories from another task. See `INFRASTRUCTURE.md` for the resumable
1,136-task architecture.

## Published baselines to beat

As of 2026-07-10, the ContextBench leaderboard reports:

| System | Pass@1 | Line context F1 | Efficiency |
| --- | ---: | ---: | ---: |
| mini SWE-agent + Claude Sonnet 4.5 | 0.530 | 0.344 | 0.658 |
| mini SWE-agent + GPT-5 | 0.472 | 0.312 | 0.591 |
| Agentless + GPT-5 | 0.452 | 0.376 | not reported |

The strongest published file-context F1 is `0.634` (mini SWE-agent + GPT-5),
while the strongest published line-context F1 is `0.376` (Agentless + GPT-5).
Those are the retrieval targets. The `0.530` Pass@1 result is a separate agent
target and requires the paired agent lane described above.

## Failure-attribution triage (`triage.py`)

`triage.py` is the feedback flywheel core: it runs strictly AFTER a run's
predictions and evaluator results exist and classifies EVERY task's dominant
failure layer with numeric evidence from the per-instance AUDIT JSONs. Gold
data is consumed only here, post hoc — it never feeds retrieval.

```
python3 triage.py \
  --gold /tmp/cb-slice16/gold_train_with_repo_url.parquet \
  --predictions /tmp/cb-slice16/predictions.jsonl \
  --results /tmp/cb-slice16/results.jsonl \
  --audit-dir /tmp/cb-slice16/runs \
  --manifest /tmp/cb-slice16/slice_manifest.json \
  --out-dir /tmp/cb-triage/baseline \
  [--line-budget 80] [--good-threshold 0.5]
```

`--audit-dir` accepts either layout: flat (`<audit_dir>/<instance>.json`) or
runs (`<audit_dir>/<instance>/prediction-audit/<instance>.json`). PREFER the
runs layout: only it contains hung/watchdog-killed instances (run dir present,
no audit JSON), which the flat merged `predictions-audit/` dir omits entirely.
`--manifest` (a run/slice manifest JSON) makes the manifest the authoritative
task universe, so even never-attempted instances are triaged as INDEXING.
`--line-budget` MUST match the budget the run actually used: triage hard-fails
(rc 2, artifacts still written) when it detects an impossible line F1 > cap or
predictions larger than the stated budget (`budget_check` in
`triage_summary.json` records the evidence). Triage and `report.py` both count
failed instances as line F1 0 over the manifest universe; their macro line F1
should therefore agree for the same artifacts and budget. Outputs: `triage.jsonl`
(one record per task), `triage_summary.json` (aggregates), `triage_report.md`
(tables).

Layer taxonomy, in funnel order (each task gets a primary and optional
secondary, plus a machine-generated one-line explanation):

| Layer | Meaning | Key evidence recorded |
| --- | --- | --- |
| INDEXING | Run hung/errored/emitted nothing, or all lanes empty | sub-cause (`hang_or_empty_prediction`, `empty_search_space`) |
| RETRIEVAL | Gold file never surfaced in any lane (searches, refinement, graph contexts), surfaced only below the pool cutoff, or surfaced only via graph context without reaching the pool | best rank + lane per gold file (`graph_contexts` counts as a lane — it feeds the selector pool and proves the file was indexed) |
| SELECTION | Gold reached the selector pool (or exact-identifier floor) but is missing from the final prediction | pool/selected/final membership per gold file; `dropped_after_selection` when the selector picked gold and final assembly discarded it |
| BUDGET | `cap_f1 = 2*min(g,b)/(g+min(g,b)) < threshold` AND at least one gold file made the final prediction | cap@budget, achieved/cap ratio |
| SPAN | Right file(s), wrong lines | file F1 vs line F1, estimated in-file missed lines |
| NONE | line F1 >= good threshold (default 0.5) | — |

Two deliberate attribution policies, calibrated against manual diagnoses on
the baseline slice:

- A gold file that never appears anywhere cannot be distinguished, from the
  audit alone, between "not indexed" and "indexed but never retrieved" — it is
  attributed to RETRIEVAL; when its path matches a known exclusion pattern
  (e.g. Go `pkg/`, `dist/`, `**/benchmarks/**`) the task gets an INDEXING
  secondary with the matched pattern (`excluded-dir:...`). INDEXING is primary
  only with hard evidence (hang/error/empty prediction/empty search space).
- BUDGET is primary only when the pipeline engaged with gold (some gold file
  reached the final prediction); a budget-capped task where nothing surfaced
  is a retrieval failure first.

The summary includes per-layer counts and task-weighted shares split by
language and dataset family, top exclusion-pattern hits, and a next-lever
ranking (tasks moved + estimated line-F1 headroom per layer, using cap@budget
math). Note the BUDGET lever means "raise the line budget", which is fixed by
the benchmark — it is listed for headroom accounting, not as an action item;
the top actionable lever on the 2026-07-10 baseline is SELECTION.

`test_triage.py` pins the manually verified baseline diagnoses
(astropy/xarray/AutoGPT -> SELECTION, vscode/gh-cli -> RETRIEVAL with the cli
`pkg/` INDEXING secondary, bytes/ponyc/ansible -> BUDGET) and the E1 vscode
hang -> INDEXING; integration tests skip when `/tmp/cb-slice16` or
`/tmp/cb-tune/e1` are absent:

```
uv run --no-project --with pytest --with pandas --with pyarrow \
  python -m pytest test_triage.py -q
```
