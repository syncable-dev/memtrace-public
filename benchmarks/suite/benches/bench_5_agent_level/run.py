"""Bench #5 runner — skeleton only.

Status: **DESIGN DOC + DATASET LOADER**. The agent-driving code is
deliberately NOT implemented in this file. Bench #5 spends real LLM
API credits and is gated behind `RUN_AGENT_BENCH=1`; landing the driver
requires separate authorization from the benchmark owner.

## Intended execution

For each task in `bench_5_tasks.json`:

1. **Snapshot** — copy `initial_state.repo_path` into a clean worktree.
2. **Boot adapter** — start the adapter (Memtrace / ChromaDB / Aider
   RepoMap) on the worktree so it can serve tool calls to the agent.
3. **Drive Claude Code** with:
     - the system prompt (below)
     - the task description
     - the adapter exposed as an MCP server (for Memtrace) or a
       tool-registry facade (for ChromaDB / Aider RepoMap)
     - max 20 turns, temperature 0, fixed model version
4. **Verify** by running `task.golden_test_command` in the worktree.
   Success = exit code 0.
5. **Record**:
     - task_id, adapter_name, success: bool,
     - turns_used, tokens_in, tokens_out, est_cost_usd,
     - duration_s, terminal state summary.

## System prompt outline

    You are implementing a change in the repository. You have access to
    an MCP server that provides code-memory tools. Use them to locate
    relevant code before editing. Make the minimum change required to
    pass the hidden verifier command. Do not run git commands.

## Noise controls

- Three trials per (task, adapter) pair.
- Report mean ± std of `task_completion_rate` across trials.
- Task order is deterministically shuffled per trial-seed so model
  memory across trials can't bias results.

## Out of scope here

- SWE-bench-lite integration (separate follow-up PR).
- Aider RepoMap adapter wiring (follow-up; stubbed only).
- Prompt iteration (frozen once benchmark publishes).
"""
from __future__ import annotations
import json
import os
from pathlib import Path

from benchmarks.suite.contract import Adapter


BENCH_ID = "Bench #5 — Agent-Level SWE Tasks (GATED)"
PRIMARY_AXIS = "task_completion_rate"
DATASET_VERSION = "hand-authored-2026-04-21"
RUN_GATE_ENV = "RUN_AGENT_BENCH"


def default_dataset_path() -> Path:
    return Path(__file__).resolve().parents[2] / "datasets" / "bench_5_tasks.json"


def load_tasks(path: Path | None = None) -> list[dict]:
    p = path or default_dataset_path()
    with p.open() as f:
        return json.load(f)


def run_with_adapter(adapter: Adapter, tasks: list[dict], out_dir: Path) -> None:
    """Runs Claude Code on `tasks` with `adapter` as the code-memory backend.

    GATED behind RUN_AGENT_BENCH=1. Raises RuntimeError otherwise so no
    accidental CI run burns LLM credits.
    """
    if os.environ.get(RUN_GATE_ENV) != "1":
        raise RuntimeError(
            f"Bench #5 is gated. Set {RUN_GATE_ENV}=1 to spend LLM credits. "
            "Docs: benches/bench_5_agent_level/run.py module docstring."
        )
    raise NotImplementedError(
        "Bench #5 driver is not implemented in bench-345-infra. "
        "It lives behind a separate authorization gate; see module docstring."
    )
