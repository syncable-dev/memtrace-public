"""Middleware — lightweight hooks that run around task execution."""
from __future__ import annotations
from typing import Callable

from .logger import get_logger


def with_logging(fn: Callable) -> Callable:
    log = get_logger("mw.logging")

    def wrapper(*args, **kwargs):
        log.debug(f"enter {fn.__name__}")
        out = fn(*args, **kwargs)
        log.debug(f"exit  {fn.__name__}")
        return out
    return wrapper


def with_timing(fn: Callable) -> Callable:
    import time
    log = get_logger("mw.timing")

    def wrapper(*args, **kwargs):
        t0 = time.time()
        out = fn(*args, **kwargs)
        log.info(f"{fn.__name__} took {(time.time() - t0) * 1000:.1f}ms")
        return out
    return wrapper
