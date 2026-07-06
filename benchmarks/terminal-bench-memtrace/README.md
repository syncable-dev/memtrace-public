# Terminal-Bench 2.1 + Codex GPT-5.5 + Memtrace

Date: 2026-06-13
Status: investigation/runbook plus local Harbor adapter

This folder captures the reproduction plan for testing whether Codex CLI
running GPT-5.5 with Memtrace beats the current Terminal-Bench 2.1 public
baseline and Sentra's reported internal result.

## What Sentra Claimed

Sentra reports an internal, not-yet-verified run of Terminal-Bench 2.1:

- Dataset: `terminal-bench/terminal-bench-2-1`
- Harness: Harbor with Docker task environments
- Agent/model: Codex CLI + GPT-5.5
- Reasoning effort: `xhigh`
- Trials: 89 tasks x 5 attempts = 445 trials
- Result: 393 / 445 successes, 88.31% mean reward
- Baseline they compare against: public Codex CLI + GPT-5.5 xhigh, 371 / 445,
  83.37%
- Important caveat: Sentra-side retrieval/embedding/reranking costs are not
  included in their model-cost field.

Primary source: https://www.sentra.app/research/terminal-bench

The official Terminal-Bench 2.1 leaderboard currently shows Codex CLI +
GPT-5.5 at 83.4% and documents the canonical run shape:

```bash
harbor run -d terminal-bench/terminal-bench-2-1 -a "agent" -m "model" -k 5
```

Primary source: https://www.tbench.ai/leaderboard/terminal-bench/2.1

## Reproduction Contract

To make our result comparable, the Memtrace run should keep everything equal
except the memory/tool layer:

- Use the official Harbor dataset `terminal-bench/terminal-bench-2-1`.
- Run exactly `-k 5` for leaderboard-comparable numbers.
- Do not modify task timeouts, verifier timeouts, CPU, memory, or storage.
- Use Codex CLI with `model = "gpt-5.5"`.
- Use `model_reasoning_effort = "xhigh"`.
- Scope Memtrace state per trial, not globally across tasks.
- Index the task repository before the agent loop starts.
- Wait for indexing and embeddings to finish before Codex sees the task.
- Ensure the Memtrace retrieval stack is ready before Codex sees the task:
  BM25/text, semantic embeddings, graph relationships/transitive traversal, and
  any cross-encoder/rerank path used by the configured Memtrace build.
- Keep a Memtrace watcher active while Codex edits files.
- Publish complete trajectories and per-task trial records.
- Report both leaderboard-style model cost and all-in cost including Memtrace
  embedding/retrieval overhead.

## Current Local State

Checked from `/Users/alexholmberg/Desktop/Memtrace` on 2026-06-13:

- Codex CLI is installed: `codex-cli 0.140.0-alpha.2`.
- Local Codex config already has `model = "gpt-5.5"`.
- Local Codex config already has `model_reasoning_effort = "xhigh"`.
- Memtrace CLI is installed: `memtrace 0.6.20`.
- Memtrace is running for this workspace at `http://localhost:3030`.
- Codex has a `memtrace` MCP server registered locally.
- Harbor is installed through `uv tool install harbor`.
- OrbStack exposes Docker at `/Applications/OrbStack.app/Contents/MacOS/xbin/docker`.
- `.env` is gitignored and is expected to provide `OPENAI_API_KEY` and
  `MEMTRACE_LICENSE_KEY`.
- `DAYTONA_API_KEY` is not set in this shell.

## Adapter Shape

The clean way to run this is a Harbor custom installed agent:

1. In each Terminal-Bench trial container, install Codex CLI and Memtrace.
   Codex is installed first, then Memtrace, then the Memtrace Codex skill
   installer runs with `--only codex --global --skip-mcp -y`.
2. Set a trial-local Memtrace data directory, for example:

   ```bash
   export MEMTRACE_MEMDB_DATA_DIR="$HARBOR_TRIAL_DIR/.memdb"
   export MEMTRACE_HEADLESS=1
   export MEMTRACE_WORKSPACE_ROOT=/app
   ```

3. Start Memtrace headless, index the task repo, and block until the index
   and embedding pass are complete:

   ```bash
   memtrace index --clear /app
   memtrace start --headless --force --workspace /app &
   # Adapter wait barrier:
   # poll Memtrace until parsing/indexing is done AND embeddings are complete.
   ```

   The adapter must treat incomplete embeddings as a setup failure, not as a
   valid trial. Codex should not receive the Terminal-Bench instruction until
   Memtrace can answer graph + BM25 + semantic/reranked retrieval queries
   against the task repository. This setup time should be recorded separately
   from agent solve time. If the adapter indexes through MCP instead of the
   CLI, use `index_directory` followed by `check_job_status` and wait for
   `status = "completed"` and `embed_done == embed_total`.

The current smoke adapter uses the Memtrace CLI. Because `memtrace index`
expects a git checkout, it creates an ephemeral `.git` directory in the task
root when the task image does not already provide one. That is acceptable for
local signal validation, but a fully publishable run should switch the
pre-index barrier to the MCP `index_directory` path so no task files are
mutated just to satisfy CLI repository discovery.

## Baseline Stats Sources

Use these in order:

1. Official Terminal-Bench 2.1 leaderboard: the public verified headline
   baseline. As of 2026-06-13, Codex CLI + GPT-5.5 is listed at 83.4% ± 2.2.
   https://www.tbench.ai/leaderboard/terminal-bench/2.1
2. Snorkel Terminal-Bench 2.1 page: mirrors the leaderboard, explains the 2.1
   task changes, and links the per-task change discussion in PR #53.
   https://snorkel.ai/leaderboard/terminal-bench-2-1/
3. Sentra Terminal-Bench report: opponent-specific comparison with per-task
   Sentra-vs-baseline success counts, costs, and display tokens for all 89
   tasks. Treat as unverified until Terminal-Bench accepts their submission.
   https://www.sentra.app/research/terminal-bench
4. Harbor run artifacts: source of truth for our own run. Archive the job
   directory, `config.json`, every trial `result.json`, trajectories, and a
   derived per-task CSV so others can recompute accuracy/cost/tokens.

4. Write a trial-local `CODEX_HOME/config.toml`:

   ```toml
   model = "gpt-5.5"
   model_reasoning_effort = "xhigh"
   approval_policy = "never"
   sandbox_mode = "danger-full-access"

   [mcp_servers.memtrace]
   command = "memtrace"
   args = ["mcp"]
   startup_timeout_sec = 60

   [mcp_servers.memtrace.env]
   MEMTRACE_MEMDB_MODE = "embedded"
   MEMTRACE_MEMDB_DATA_DIR = "/tmp/memtrace-memdb"
   MEMTRACE_DATA_DIR = "/tmp/memtrace-state"
   MEMTRACE_WORKSPACE_ROOT = "/app"
   ```

5. Run Codex non-interactively from `/app`:

   ```bash
   HOME="$HARBOR_TRIAL_DIR/home" \
   CODEX_HOME="$HARBOR_TRIAL_DIR/codex-home" \
   codex exec \
     --ignore-rules \
     -C /app \
     -m gpt-5.5 \
     -c 'model_reasoning_effort="xhigh"' \
     --dangerously-bypass-approvals-and-sandbox \
     "$TERMINAL_BENCH_INSTRUCTION"
   ```

   The clean `HOME` and `CODEX_HOME` are load-bearing. Do not run the benchmark
   with a normal desktop Codex profile, personal plugins, personal skills, or
   project rules loaded; they contaminate both behavior and token accounting.
   Do not pass `--ignore-user-config` here: it also skips the clean
   `$CODEX_HOME/config.toml` that registers Memtrace MCP.
   Benchmark-pinned Memtrace instructions/skills are allowed if they are
   treated as part of the tested Memtrace agent, committed to this folder, and
   reported in the artifact bundle. In the Harbor container, the container
   itself is the sandbox, so Codex can run with approval/sandbox bypass.

6. Let Harbor collect the task result and artifacts.

OpenAI Codex CLI install/config references:

- https://developers.openai.com/codex/cli
- https://developers.openai.com/codex/config-reference

Harbor custom-agent reference:

- https://www.harborframework.com/docs/agents

## Commands Once Prereqs Exist

Install prerequisites:

```bash
# Per Harbor docs. Install uv first if it is not available locally.
uv tool install harbor

# Docker Desktop or a cloud sandbox provider is required.
docker info

# For local/OpenAI API auth inside containers.
export OPENAI_API_KEY="..."
```

Baseline smoke, before custom Memtrace:

```bash
harbor run -d terminal-bench/terminal-bench-2-1 -a oracle
harbor run -d terminal-bench/terminal-bench-2-1 -a codex-cli -m gpt-5.5 -k 1
```

Full public-baseline reproduction:

```bash
harbor run -d terminal-bench/terminal-bench-2-1 -a codex-cli -m gpt-5.5 -k 5
```

Memtrace run, after the custom Harbor agent exists:

```bash
scripts/run_smoke.sh
```

Scaled cloud run, if using Daytona:

```bash
export DAYTONA_API_KEY="..."

harbor run \
  -d terminal-bench/terminal-bench-2-1 \
  --agent-import-path benchmarks.terminal_bench_memtrace.agent:CodexMemtraceAgent \
  --env daytona \
  -n 32 \
  -k 5
```

## Cost Control Plan

Do not start with the 445-trial run. Use gated stages:

1. No-model setup validation: install Harbor, run oracle, confirm the custom
   agent can install Codex + Memtrace, index, embed, and expose MCP tools.
2. Single-task smoke: one easy task, `k = 1`, prove the full loop works.
3. Sentra-delta smoke: 8-12 tasks from the gain/regression lists, `k = 1`.
4. Targeted confidence run: 20 high-signal tasks, `k = 5`.
5. Full run: all 89 tasks, `k = 5`, only after earlier stages show accuracy
   lift and token reduction.

Cost baselines from Sentra's report:

- Public Codex CLI + GPT-5.5 baseline: $1,862.98 / 445 = about $4.19 per trial.
- Sentra Code Memory run: $510.30 / 445 = about $1.15 per trial.

OpenAI API pricing as of 2026-06-13 lists GPT-5.5 at:

- Short context: $2.50 / 1M input, $0.25 / 1M cached input, $15 / 1M output.
- Long context: $5.00 / 1M input, $0.50 / 1M cached input, $22.50 / 1M output.

Run through a dedicated OpenAI API project with a project budget and usage
alerts. Treat that as a guardrail, not the only stop condition; OpenAI notes
budget enforcement can lag. The Harbor wrapper should also stop launching new
trials once observed spend crosses the stage budget.

Suggested stage budgets:

- Stage 1: $10-25.
- Stage 2: $25-75.
- Stage 3: $150-300.
- Full 445-trial run: approve explicitly after Stage 3; expect a rough range
  from Sentra-like $500 to baseline-like $1,900 plus sandbox/infra costs.

## Local Smoke Findings

Smoke date: 2026-06-13.

Disposable fixture: `/tmp/memtrace-tbench-smoke-*`.

What passed:

- A fresh task repo was indexed before Codex saw the task.
- Memtrace reported 10 files scanned, 11 embeddings created, 31 nodes, 38 edges.
- A direct retrieval probe returned the target file first:
  `src/ledger_pipeline/exposure.py`.
- With minimal MCP config and approval/sandbox bypass, Codex MCP calls succeeded:
  `list_indexed_repositories`, `find_code`, `get_symbol_context`, `get_impact`,
  `get_style_fingerprint`, and `get_source_window`.
- Codex made the minimal fix and tests passed:
  `PYTHONPATH=src python3 -m unittest discover -s tests`.

What failed / needs adapter hardening:

- Running with the normal desktop Codex profile caused Memtrace MCP calls to
  fail as `user cancelled MCP tool call` under non-interactive execution.
- The successful local solve still consumed about 746k input tokens because the
  desktop environment exposed local Memtrace skill files and Codex read them.
  This is not acceptable unless those exact files are intentionally pinned as
  part of the benchmarked agent.

Adapter requirement from the smoke:

- Use a clean benchmark-only `HOME` and `CODEX_HOME`.
- Inject only the Memtrace MCP server needed for the task.
- Optionally inject a benchmark-pinned Memtrace skill/prompt, but publish it and
  count its tokens.
- Run Codex with `--ignore-rules`, but let it load the clean
  `$CODEX_HOME/config.toml` so MCP servers are visible.
- In the task container, use `--dangerously-bypass-approvals-and-sandbox` so MCP
  tools do not get cancelled by the non-interactive approval path.
- Archive event JSONL and reject any trial where Memtrace MCP calls fail before
  the first edit.

Harbor adapter smoke:

- `codex-memtrace-1-smoke-v5` reached Memtrace index, embeddings, and server
  startup, then Codex started solving. It was stopped because
  `--ignore-user-config` hid the clean MCP config, so Codex reported Memtrace
  tools unavailable.
- `codex-memtrace-1-smoke-v6` and `codex-memtrace-license-preflight` failed
  before Codex at `memtrace index` with HTTP 401 from Memtrace auth.
- The license preflight artifact confirms the container received
  `MEMTRACE_LICENSE_KEY` and saw a 20-character value. The current blocker is a
  valid Memtrace license key for fresh containers, not OpenAI or OrbStack.

## Tasks To Watch

Sentra says their gains concentrated on discovery-heavy tasks. These are good
early smoke targets before paying for all 445 trials:

- `extract-moves-from-video`
- `qemu-alpine-ssh`
- `protein-assembly`
- `configure-git-webserver`
- `extract-elf`
- `train-fasttext`
- `compile-compcert`

Regression targets to inspect carefully:

- `torch-pipeline-parallelism`
- `vulnerable-secret`
- `make-doom-for-mips`
- `raman-fitting`

## Credibility Checklist

Before publishing a claim:

- Pin Codex CLI version. Sentra's baseline says Codex CLI `0.125.0`; local
  Codex is `0.140.0-alpha.2`, which is not apples-to-apples.
- Record Memtrace version and embedder config.
- Record Harbor version and dataset version/hash.
- Keep per-task isolated `.memdb` state.
- Archive Codex config for each trial.
- Export every trajectory.
- Recompute baseline from public artifacts or rerun the baseline with the same
  Codex CLI version.
- Separate "model cost" from "Memtrace side cost".
- Submit through the official Terminal-Bench verification path before using
  leaderboard language.
