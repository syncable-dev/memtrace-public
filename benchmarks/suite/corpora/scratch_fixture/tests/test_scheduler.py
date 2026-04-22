"""Tests for Scheduler fan-out."""
from ..scheduler import Scheduler
from ..task import make_task


def test_run_to_empty_completes_all():
    s = Scheduler(num_workers=2)
    for i in range(6):
        s.submit(make_task(f"t{i}", lambda v=i: v * 2))
    s.run_to_empty()
    assert len(s.queue) == 0
