"""CodeGrapherContext adapter — `cgc find name` CLI + grep fallback."""
from __future__ import annotations
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from benchmarks.suite.contract import (
    Adapter, GraphResult, NotSupported, QueryResult, ReindexReport,
    SetupReport, SymbolRef,
)


def _default_cgc_binary() -> Path:
    env = os.environ.get("CGC_BIN")
    if env:
        return Path(env)
    which = shutil.which("cgc")
    if which:
        return Path(which)
    # Original benchmark-host layout — internal runs.
    return Path("/Users/alexthh/Desktop/ZeroToDemo/Memtrace/benchmarks/.venv/bin/cgc")


DEFAULT_BIN = _default_cgc_binary()


class CGCAdapter(Adapter):
    name = "cgc"
    description = "cgc find name CLI + grep fallback to recover file paths"
    version = "codegraphcontext@<pin in versions.toml>"

    TABLE_RE = re.compile(r"[│┌┐└┘├┤┬┴┼╭╮╯╰─]")
    WS_RE    = re.compile(r"\s+")

    def __init__(self, binary: Path | None = None):
        self.binary = binary or DEFAULT_BIN
        self._corpus_path: Path | None = None
        self._corpus_parent: Path | None = None
        self._grep_cache: dict[str, list[str]] = {}

    def setup(self, corpus) -> SetupReport:
        self._corpus_path = corpus.path
        self._corpus_parent = corpus.parent
        return SetupReport(indexed_files=0, wall_ms=0.0)

    def teardown(self) -> None:
        self._grep_cache.clear()

    def query_symbol(self, name: str, limit: int) -> QueryResult:
        if self._corpus_path is None:
            raise RuntimeError("CGCAdapter: call setup() before query_symbol()")
        if not self.binary.exists():
            return QueryResult(raw_response_text="<cgc not installed>")

        t0 = time.time()
        text = ""
        paths: list[str] = []
        try:
            out = subprocess.run(
                [str(self.binary), "find", "name", name],
                capture_output=True, text=True, timeout=15,
                env={**os.environ, "CI": "1", "NO_COLOR": "1", "TERM": "dumb"},
                cwd=str(self._corpus_path),
            )
            text = out.stdout + "\n" + out.stderr
            cleaned = self.TABLE_RE.sub(" ", text)
            cleaned = self.WS_RE.sub(" ", cleaned)
            corpus_name = self._corpus_path.name
            abs_re = re.compile(
                rf"/{re.escape(corpus_name)}\s*(/[A-Za-z0-9_./\-]+?\.(?:py|ts|tsx|js|rs|go|java))"
            )
            for m in abs_re.finditer(cleaned):
                rel = f"{corpus_name}{m.group(1)}"
                if rel not in paths:
                    paths.append(rel)
            if not paths and name.lower() in text.lower() and "Found" in text:
                paths = self._grep_files(name)
        except Exception as e:
            text = f"<error: {e}>"
        return QueryResult(
            paths=paths,
            ranked_symbols=[SymbolRef(name=name, file_path=p, line=None) for p in paths],
            raw_response_text=text,
            latency_ms=(time.time() - t0) * 1000,
            tokens_used=len(text) // 4,
        )

    # ── Bench #3: graph queries ──────────────────────────────────────────────
    #
    # cgc ships `cgc analyze callers`, `cgc analyze calls`, and `cgc analyze
    # dead-code`. The CLI prints box-drawing output; we reuse the same
    # cleanup pipeline as query_symbol and regex-extract file paths + names.

    def callers_of(self, name: str) -> GraphResult | NotSupported:
        return self._graph_cli(["analyze", "callers", name])

    def callees_of(self, name: str) -> GraphResult | NotSupported:
        return self._graph_cli(["analyze", "calls", name])

    def impact_of(self, name: str) -> GraphResult | NotSupported:
        # cgc has no single "impact" command; we approximate with transitive
        # callers via --depth if exposed, otherwise disclose honestly.
        return NotSupported(
            reason="cgc does not expose an impact / transitive-callers command"
        )

    def find_dead_code(self) -> GraphResult | NotSupported:
        return self._graph_cli(["analyze", "dead-code"])

    def _graph_cli(self, argv: list[str]) -> GraphResult | NotSupported:
        if self._corpus_path is None:
            raise RuntimeError("CGCAdapter: call setup() before graph queries")
        if not self.binary.exists():
            return NotSupported(reason="cgc binary not installed")
        t0 = time.time()
        try:
            # `COLUMNS=400` tells Rich (the CLI's rendering library) to
            # render tables wide enough that NO column gets truncated with
            # the "…" ellipsis character.  Without it CGC truncates to
            # ~30-char columns and long symbol names come back incomplete,
            # killing set-intersection scoring.
            env = {**os.environ, "CI": "1", "NO_COLOR": "1", "TERM": "dumb",
                   "COLUMNS": "400"}
            out = subprocess.run(
                [str(self.binary), *argv],
                capture_output=True, text=True, timeout=30,
                env=env,
                cwd=str(self._corpus_path),
            )
            text = (out.stdout or "") + "\n" + (out.stderr or "")
        except Exception:
            return GraphResult(nodes=[], latency_ms=(time.time() - t0) * 1000)

        # Parse Rich-rendered 3-column table directly.
        #
        # Expected shape (with COLUMNS=400 wide rendering):
        #   ╭──────┬──────┬──────╮
        #   │ Caller Function   │ Location            │ Call Type │
        #   ├──────┼──────┼──────┤
        #   │ test_tools_call_… │ /path/file.py:45    │ 📝 Project │
        #   │ ...
        #   ╰──────┴──────┴──────╯
        # Every data row starts with "│" and contains exactly 3 "│" pipes
        # splitting it into columns.
        nodes: list[SymbolRef] = []
        seen: set[tuple[str, str]] = set()
        corpus_name = self._corpus_path.name

        # Path-extract regex for the Location column.  It may contain an
        # absolute path (CGC's default) — relativise to the corpus.
        loc_re = re.compile(
            rf"(?:.*?/)?{re.escape(corpus_name)}(/[A-Za-z0-9_./\-]+?\.(?:py|ts|tsx|js|rs|go|java))"
            r"(?::(\d+))?"
        )

        # Identify header to know which columns are which.
        header_cols: list[str] = []
        for raw_line in text.splitlines():
            if not raw_line.startswith("│"):
                continue
            parts = [p.strip() for p in raw_line.strip("│").split("│")]
            # First row with a "Caller Function" or "Callee Function"
            # or "Function" heading is the header.
            if not header_cols and any(
                "Function" in p or "Name" in p for p in parts
            ):
                header_cols = parts
                continue
            if not header_cols:
                continue
            if len(parts) != len(header_cols):
                continue
            # Data row: first column is the function name.
            name = parts[0]
            if not name or name.startswith("-") or name == header_cols[0]:
                continue
            # Strip any trailing "…" (defensive — with COLUMNS=400 this
            # shouldn't happen, but safer than silently mis-matching).
            name = name.rstrip("…").strip()
            if not name:
                continue
            # Second column usually has the path.
            file_rel = ""
            line_no = None
            if len(parts) >= 2:
                m = loc_re.search(parts[1])
                if m:
                    file_rel = f"{corpus_name}{m.group(1)}"
                    if m.group(2):
                        try:
                            line_no = int(m.group(2))
                        except ValueError:
                            pass
            key = (name, file_rel)
            if key in seen:
                continue
            seen.add(key)
            nodes.append(SymbolRef(name=name, file_path=file_rel, line=line_no))
        return GraphResult(nodes=nodes, latency_ms=(time.time() - t0) * 1000)

    # ── Bench #4: incremental indexing ───────────────────────────────────────

    def reindex_paths(self, paths: list[Path]) -> ReindexReport | NotSupported:
        """`cgc index <path>` is the documented re-index path. We invoke it
        once per unique parent directory to batch related files."""
        if self._corpus_path is None:
            raise RuntimeError("CGCAdapter: call setup() before reindex_paths()")
        if not self.binary.exists():
            return NotSupported(reason="cgc binary not installed")
        t0 = time.time()
        targets = {str(Path(p).resolve()) for p in paths}
        touched = 0
        for t in targets:
            try:
                out = subprocess.run(
                    [str(self.binary), "index", t],
                    capture_output=True, text=True, timeout=120,
                    env={**os.environ, "CI": "1", "NO_COLOR": "1", "TERM": "dumb"},
                    cwd=str(self._corpus_path),
                )
                if out.returncode == 0:
                    touched += 1
            except Exception:
                continue
        return ReindexReport(
            files_reindexed=touched,
            wall_ms=(time.time() - t0) * 1000,
            incremental=True,
        )

    def time_to_queryable(self, name: str, deadline_ms: int) -> float:
        if self._corpus_path is None:
            raise RuntimeError("CGCAdapter: call setup() before time_to_queryable()")
        if not self.binary.exists():
            return float(deadline_ms + 1)
        t0 = time.time()
        poll_interval_s = 0.2
        while True:
            elapsed_ms = (time.time() - t0) * 1000
            if elapsed_ms > deadline_ms:
                return float(deadline_ms + 1)
            res = self.query_symbol(name, limit=5)
            if res.paths:
                return (time.time() - t0) * 1000
            time.sleep(poll_interval_s)

    def _grep_files(self, symbol: str) -> list[str]:
        if symbol in self._grep_cache:
            return self._grep_cache[symbol]
        try:
            out = subprocess.run(
                ["grep", "-rln", "-E",
                 rf"(def {symbol}\b|class {symbol}\b|async def {symbol}\b)",
                 str(self._corpus_path)],
                capture_output=True, text=True, timeout=5,
            )
            paths: list[str] = []
            for line in out.stdout.splitlines():
                try:
                    rel = str(Path(line).relative_to(self._corpus_parent))
                    if rel not in paths:
                        paths.append(rel)
                except ValueError:
                    pass
            self._grep_cache[symbol] = paths
            return paths
        except Exception:
            self._grep_cache[symbol] = []
            return []
