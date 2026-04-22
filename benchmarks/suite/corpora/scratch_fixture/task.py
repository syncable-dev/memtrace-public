"""Task — the unit of work the queue shuffles around."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Task:
    name: str
    fn: Callable[..., Any]
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    priority: int = 0

    def run(self) -> Any:
        return self.fn(*self.args, **self.kwargs)


def make_task(name: str, fn: Callable[..., Any], *args, **kwargs) -> Task:
    return Task(name=name, fn=fn, args=args, kwargs=kwargs)
