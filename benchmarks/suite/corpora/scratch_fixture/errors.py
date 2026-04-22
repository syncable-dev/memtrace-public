"""Exception types the fixture raises — kept trivial on purpose."""
from __future__ import annotations


class TaskError(Exception):
    pass


class QueueFullError(TaskError):
    pass


class SchedulerStoppedError(TaskError):
    pass


def wrap_error(exc: Exception, context: str) -> TaskError:
    return TaskError(f"[{context}] {type(exc).__name__}: {exc}")
