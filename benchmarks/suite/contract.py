"""Shared adapter contract: every competitor implements this surface."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union


@dataclass
class SymbolRef:
    name: str
    file_path: str
    line: int | None = None


@dataclass
class QueryResult:
    paths: list[str] = field(default_factory=list)          # ranked, best first
    ranked_symbols: list[SymbolRef] = field(default_factory=list)
    raw_response_text: str = ""
    latency_ms: float = 0.0
    tokens_used: int = 0


@dataclass
class GraphResult:
    nodes: list[SymbolRef] = field(default_factory=list)
    latency_ms: float = 0.0


@dataclass
class ReindexReport:
    files_reindexed: int = 0
    wall_ms: float = 0.0
    incremental: bool = True


@dataclass
class SetupReport:
    indexed_files: int = 0
    wall_ms: float = 0.0


@dataclass
class NotSupported:
    reason: str


# Return-type aliases for readability in adapter signatures.
MaybeQuery = Union[QueryResult, NotSupported]
MaybeGraph = Union[GraphResult, NotSupported]
MaybeReindex = Union[ReindexReport, NotSupported]


class Adapter:
    """Base class. Concrete adapters override methods they support.

    Methods not overridden return NotSupported — this is data, not failure.
    """
    name: str
    description: str
    version: str

    # Lifecycle — concrete adapters MUST override these two.
    def setup(self, corpus) -> SetupReport:
        raise NotImplementedError

    def teardown(self) -> None:
        raise NotImplementedError

    # Bench #0, #1 — concrete adapters MUST override this (it's the Bench #0 API).
    def query_symbol(self, name: str, limit: int) -> QueryResult:
        raise NotImplementedError

    # Bench #2
    def query_natural(self, text: str, limit: int) -> MaybeQuery:
        return NotSupported(reason="query_natural not implemented")

    # Bench #3
    def callers_of(self, name: str) -> MaybeGraph:
        return NotSupported(reason="callers_of not implemented")

    def callees_of(self, name: str) -> MaybeGraph:
        return NotSupported(reason="callees_of not implemented")

    def impact_of(self, name: str) -> MaybeGraph:
        return NotSupported(reason="impact_of not implemented")

    def find_dead_code(self) -> MaybeGraph:
        return NotSupported(reason="find_dead_code not implemented")

    # Bench #4
    def reindex_paths(self, paths: list[Path]) -> MaybeReindex:
        return NotSupported(reason="reindex_paths not implemented")

    def time_to_queryable(self, name: str, deadline_ms: int) -> float:
        return float("inf")
