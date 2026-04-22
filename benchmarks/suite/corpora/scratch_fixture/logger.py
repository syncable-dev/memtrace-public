"""Tiny logger facade — centralises formatting for the fixture."""
from __future__ import annotations
import sys
import time


class Logger:
    def __init__(self, name: str) -> None:
        self.name = name

    def _emit(self, level: str, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        print(f"{ts} [{level}] {self.name}: {msg}", file=sys.stderr)

    def info(self, msg: str) -> None:
        self._emit("INFO", msg)

    def debug(self, msg: str) -> None:
        self._emit("DEBUG", msg)

    def error(self, msg: str) -> None:
        self._emit("ERROR", msg)


def get_logger(name: str) -> Logger:
    return Logger(name)
