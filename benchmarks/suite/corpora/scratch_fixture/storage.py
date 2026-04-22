"""In-memory result storage — a trivial key/value map for task outputs."""
from __future__ import annotations


class ResultStore:
    def __init__(self) -> None:
        self._data: dict[str, object] = {}

    def put(self, key: str, value: object) -> None:
        self._data[key] = value

    def get(self, key: str) -> object | None:
        return self._data.get(key)

    def keys(self) -> list[str]:
        return list(self._data.keys())

    def clear(self) -> None:
        self._data.clear()


def new_store() -> ResultStore:
    return ResultStore()
