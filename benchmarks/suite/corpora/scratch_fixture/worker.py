"""Worker — pulls from TaskQueue, runs tasks, publishes completion."""
from __future__ import annotations
from typing import Any

from .queue import TaskQueue
from .events import EventBus
from .logger import get_logger


class Worker:
    def __init__(self, queue: TaskQueue, bus: EventBus, name: str = "w0") -> None:
        self.queue = queue
        self.bus = bus
        self.name = name
        self.log = get_logger(f"worker.{name}")

    def run_one(self) -> Any:
        task = self.queue.pop()
        if task is None:
            return None
        self.log.debug(f"running {task.name}")
        result = task.run()
        self.bus.publish("task.done", {"name": task.name, "result": result})
        return result

    def drain(self) -> int:
        n = 0
        while self.run_one() is not None:
            n += 1
        return n
