"""Bench #5 — Agent-level SWE-task completion (GATED, directional).

Primary axis: `task_completion_rate`.
Secondary: median turns-to-solution, total API cost per task.

GATING: This bench drives a real LLM (Claude) through Claude Code to
attempt coding tasks. It costs money per run and is therefore gated
behind `RUN_AGENT_BENCH=1`. The in-repo skeleton documents the intended
driver loop but does **NOT** spend API credits unless explicitly
authorized by the operator.
"""
from .run import (
    BENCH_ID, PRIMARY_AXIS, DATASET_VERSION,
    load_tasks, run_with_adapter,
)

__all__ = [
    "BENCH_ID", "PRIMARY_AXIS", "DATASET_VERSION",
    "load_tasks", "run_with_adapter",
]
