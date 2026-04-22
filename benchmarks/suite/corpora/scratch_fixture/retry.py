"""Retry policies — wraps a task fn with exponential backoff."""
from __future__ import annotations
import time
from typing import Callable


def retry(fn: Callable, attempts: int = 3, base_delay: float = 0.01) -> Callable:
    def wrapper(*args, **kwargs):
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last_exc = e
                time.sleep(base_delay * (2 ** i))
        raise last_exc  # type: ignore[misc]
    return wrapper


def jitter(base: float, n: int) -> float:
    return base * (1 + 0.1 * (n % 7))
