"""Scheduler — fans tasks out to multiple Workers round-robin."""
from __future__ import annotations

from .queue import TaskQueue
from .worker import Worker
from .events import EventBus
from .logger import get_logger


class Scheduler:
    def __init__(self, num_workers: int = 2) -> None:
        self.queue = TaskQueue()
        self.bus = EventBus()
        self.workers = [Worker(self.queue, self.bus, name=f"w{i}")
                        for i in range(num_workers)]
        self.log = get_logger("scheduler")

    def submit(self, task) -> None:
        self.queue.push(task)

    def tick(self) -> int:
        return sum(1 for w in self.workers if w.run_one() is not None)

    def run_to_empty(self) -> int:
        rounds = 0
        while self.tick() > 0:
            rounds += 1
        return rounds
