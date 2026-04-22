"""Run-time metrics collector — counts task outcomes, emits rollups."""
from __future__ import annotations


class MetricsCollector:
    def __init__(self) -> None:
        self.completed = 0
        self.failed = 0

    def on_done(self, payload: dict) -> None:
        self.completed += 1

    def on_error(self, payload: dict) -> None:
        self.failed += 1

    def summary(self) -> dict:
        total = self.completed + self.failed
        rate = (self.completed / total) if total else 0.0
        return {"completed": self.completed, "failed": self.failed, "success_rate": rate}


def reset(metrics: MetricsCollector) -> None:
    metrics.completed = 0
    metrics.failed = 0
