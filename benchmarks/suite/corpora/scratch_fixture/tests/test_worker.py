"""Tests for Worker drain loop."""
from ..queue import TaskQueue
from ..events import EventBus
from ..worker import Worker
from ..task import make_task


def test_drain_runs_all_tasks():
    q = TaskQueue()
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("task.done", lambda p: seen.append(p["name"]))
    q.push(make_task("a", lambda: 1))
    q.push(make_task("b", lambda: 2))
    w = Worker(q, bus)
    assert w.drain() == 2
    assert set(seen) == {"a", "b"}
