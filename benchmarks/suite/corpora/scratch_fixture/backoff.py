"""Backoff helpers used by retry + scheduler pacing."""
from __future__ import annotations
import math


def exponential(attempt: int, base: float = 0.1, cap: float = 5.0) -> float:
    return min(base * math.pow(2, attempt), cap)


def fixed(_attempt: int, seconds: float = 0.5) -> float:
    return seconds


def choose_backoff(policy: str):
    if policy == "fixed":
        return fixed
    return exponential
