"""Tests for TaskQueue priority ordering."""
from ..queue import TaskQueue
from ..task import make_task


def test_priority_order():
    q = TaskQueue()
    q.push(make_task("low", lambda: 1))
    hi = make_task("hi", lambda: 2)
    hi.priority = 10
    q.push(hi)
    assert q.pop().name == "hi"
    assert q.pop().name == "low"


def test_fifo_within_priority():
    q = TaskQueue()
    q.push(make_task("a", lambda: 1))
    q.push(make_task("b", lambda: 2))
    assert q.pop().name == "a"
    assert q.pop().name == "b"
