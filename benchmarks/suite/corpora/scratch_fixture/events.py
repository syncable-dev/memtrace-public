"""EventBus — pub/sub used by worker + scheduler for completion signals."""
from __future__ import annotations
from collections import defaultdict
from typing import Callable


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Callable) -> None:
        self._subs[topic].append(handler)

    def publish(self, topic: str, payload: object) -> None:
        for h in self._subs.get(topic, []):
            h(payload)


def make_bus() -> EventBus:
    return EventBus()
