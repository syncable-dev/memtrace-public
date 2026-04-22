"""TaskQueue — priority-aware FIFO of Tasks."""
from __future__ import annotations
import heapq
from itertools import count

from .task import Task


class TaskQueue:
    def __init__(self) -> None:
        self._heap: list[tuple[int, int, Task]] = []
        self._counter = count()

    def push(self, task: Task) -> None:
        tiebreak = next(self._counter)
        heapq.heappush(self._heap, (-task.priority, tiebreak, task))

    def pop(self) -> Task | None:
        if not self._heap:
            return None
        return heapq.heappop(self._heap)[2]

    def __len__(self) -> int:
        return len(self._heap)
