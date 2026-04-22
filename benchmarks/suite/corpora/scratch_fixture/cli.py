"""Command-line entrypoint — wires Scheduler + MetricsCollector + sample tasks."""
from __future__ import annotations
import sys

from .scheduler import Scheduler
from .task import make_task
from .metrics import MetricsCollector


def greet(name: str) -> str:
    return f"hello, {name}"


def run_demo() -> dict:
    sched = Scheduler(num_workers=2)
    metrics = MetricsCollector()
    sched.bus.subscribe("task.done", metrics.on_done)
    for i in range(5):
        sched.submit(make_task(f"greet-{i}", greet, f"user-{i}"))
    sched.run_to_empty()
    return metrics.summary()


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    summary = run_demo()
    print(summary)
    return 0
