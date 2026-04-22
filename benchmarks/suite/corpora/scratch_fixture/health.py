"""Health-check routines for the Scheduler."""
from __future__ import annotations

from .scheduler import Scheduler


def is_healthy(sched: Scheduler) -> bool:
    return len(sched.workers) > 0


def pending_work(sched: Scheduler) -> int:
    return len(sched.queue)


def health_report(sched: Scheduler) -> dict:
    return {
        "healthy": is_healthy(sched),
        "workers": len(sched.workers),
        "pending": pending_work(sched),
    }
