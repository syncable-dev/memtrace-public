"""Codebase Memory MCP adapter via its documented local CLI JSON mode."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from benchmarks.suite.contract import Adapter, NotSupported, QueryResult, SetupReport, SymbolRef


def _default_binary() -> str:
    return os.environ.get("CODEBASE_MEMORY_BIN") or shutil.which("codebase-memory-mcp") or "codebase-memory-mcp"


class CodebaseMemoryAdapter(Adapter):
    name = "codebase-memory"
    description = "Codebase Memory MCP local CLI (search_graph)"
    version = "codebase-memory-mcp@external"

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or _default_binary()
        self._corpus_path: Path | None = None
        self._project: str | None = None

    def setup(self, corpus) -> SetupReport:
        self._corpus_path = corpus.path
        if shutil.which(self.binary) is None and not Path(self.binary).exists():
            return SetupReport()
        t0 = time.time()
        result = self._run("index_repository", {"repo_path": str(corpus.path)}, timeout=300)
        if result is None:
            return SetupReport(wall_ms=(time.time() - t0) * 1000)
        projects = self._run("list_projects", {}, timeout=30) or {}
        entries = projects.get("projects", projects.get("results", projects)) if isinstance(projects, dict) else projects
        if isinstance(entries, list):
            for entry in entries:
                root_path = entry.get("root_path") or entry.get("path") if isinstance(entry, dict) else None
                if root_path and Path(root_path).resolve() == corpus.path.resolve():
                    self._project = entry.get("name")
                    break
        return SetupReport(wall_ms=(time.time() - t0) * 1000)

    def teardown(self) -> None:
        pass

    def query_symbol(self, name: str, limit: int) -> QueryResult:
        if self._corpus_path is None:
            raise RuntimeError("CodebaseMemoryAdapter: call setup() before query_symbol()")
        t0 = time.time()
        if shutil.which(self.binary) is None and not Path(self.binary).exists():
            return QueryResult(raw_response_text="<codebase-memory-mcp not installed>")
        args = {"name_pattern": rf"^{name}$", "limit": limit}
        if self._project:
            args["project"] = self._project
        payload = self._run("search_graph", args, timeout=30)
        text = json.dumps(payload) if payload is not None else "<query failed>"
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        paths: list[str] = []
        symbols: list[SymbolRef] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            path = row.get("file_path") or row.get("path") or ""
            if not path:
                continue
            rel = self._relative_path(str(path))
            if rel not in paths:
                paths.append(rel)
            symbols.append(SymbolRef(name=str(row.get("name", name)), file_path=rel, line=row.get("line")))
        return QueryResult(paths=paths, ranked_symbols=symbols, raw_response_text=text,
                           latency_ms=(time.time() - t0) * 1000, tokens_used=len(text) // 4)

    def query_natural(self, text: str, limit: int):
        return NotSupported(reason="Codebase Memory CLI exposes structural graph search, not a stable natural-language CLI query")

    def _run(self, tool: str, args: dict, timeout: int):
        try:
            result = subprocess.run([self.binary, "cli", tool, json.dumps(args)],
                                    capture_output=True, text=True, timeout=timeout)
            if result.returncode:
                return None
            return json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return None

    def _relative_path(self, path: str) -> str:
        if self._corpus_path is None:
            return path
        candidate = Path(path)
        if not candidate.is_absolute():
            return f"{self._corpus_path.name}/{candidate.as_posix().removeprefix(self._corpus_path.name + '/')}"
        try:
            return f"{self._corpus_path.name}/{candidate.resolve().relative_to(self._corpus_path.resolve())}"
        except ValueError:
            return path
