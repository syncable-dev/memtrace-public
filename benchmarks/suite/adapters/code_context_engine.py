"""Code Context Engine adapter via its documented local `cce` CLI."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from benchmarks.suite.contract import Adapter, NotSupported, QueryResult, SetupReport, SymbolRef


ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
RESULT_PATH = re.compile(r"^\s*\d+\.\s+(.+?):(\d+)-(\d+)", re.MULTILINE)


class CodeContextEngineAdapter(Adapter):
    name = "code-context-engine"
    description = "Code Context Engine local cce index/search CLI"
    version = "code-context-engine@external"

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or os.environ.get("CCE_BIN") or shutil.which("cce") or "cce"
        self._corpus_path: Path | None = None

    def setup(self, corpus) -> SetupReport:
        self._corpus_path = corpus.path
        if not self._available():
            return SetupReport()
        t0 = time.time()
        self._command(["index", "--full"], timeout=300)
        return SetupReport(wall_ms=(time.time() - t0) * 1000)

    def teardown(self) -> None:
        pass

    def query_symbol(self, name: str, limit: int) -> QueryResult:
        if self._corpus_path is None:
            raise RuntimeError("CodeContextEngineAdapter: call setup() before query_symbol()")
        if not self._available():
            return QueryResult(raw_response_text="<cce not installed>")
        t0 = time.time()
        text = self._command(["search", name, "--top-k", str(limit)], timeout=60) or "<query failed>"
        clean = ANSI_ESCAPE.sub("", text)
        paths: list[str] = []
        symbols: list[SymbolRef] = []
        for match in RESULT_PATH.finditer(clean):
            path = self._relative_path(match.group(1))
            if path not in paths:
                paths.append(path)
            symbols.append(SymbolRef(name=name, file_path=path, line=int(match.group(2))))
        return QueryResult(paths=paths, ranked_symbols=symbols, raw_response_text=clean,
                           latency_ms=(time.time() - t0) * 1000, tokens_used=len(clean) // 4)

    def query_natural(self, text: str, limit: int):
        return self.query_symbol(text, limit)

    def _available(self) -> bool:
        return shutil.which(self.binary) is not None or Path(self.binary).exists()

    def _command(self, args: list[str], timeout: int) -> str | None:
        try:
            result = subprocess.run([self.binary, *args], cwd=self._corpus_path,
                                    capture_output=True, text=True, timeout=timeout)
            if result.returncode:
                return None
            return result.stdout + result.stderr
        except (OSError, subprocess.TimeoutExpired):
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
