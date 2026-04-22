"""Memtrace adapter — MCP JSON-RPC over stdio, find_symbol tool.

Ported from benchmarks/fair/run_fair_benchmark.py::MemtraceAdapter. The shape
changes: __init__ no longer starts the subprocess (moved into setup()), and
query_symbol returns a QueryResult dataclass instead of a dict.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from benchmarks.suite.contract import (
    Adapter, GraphResult, NotSupported, QueryResult, ReindexReport,
    SetupReport, SymbolRef,
)


def _default_memtrace_binary() -> Path:
    env = os.environ.get("MEMTRACE_BIN")
    if env:
        return Path(env)
    which = shutil.which("memtrace")
    if which:
        return Path(which)
    # Original benchmark-host layout — internal runs.
    return Path("/Users/alexthh/Desktop/ZeroToDemo/Memtrace/target/release/memtrace")


DEFAULT_BINARY = _default_memtrace_binary()
DEFAULT_REPO_ID = "mempalace"  # matches benchmarks/fair setup


class MemtraceAdapter(Adapter):
    name = "memtrace"
    description = "Memtrace Rust binary, MCP JSON-RPC over stdio, find_symbol"
    version = "memtrace@eda2aa5"   # updated in versions.toml during releases

    def __init__(self, binary: Path | None = None, repo_id: str = DEFAULT_REPO_ID):
        self.binary = binary or DEFAULT_BINARY
        self.repo_id = repo_id
        self.p: subprocess.Popen | None = None

    def setup(self, corpus) -> SetupReport:
        t0 = time.time()
        self.p = subprocess.Popen(
            [str(self.binary), "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        try:
            self._rpc("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "suite-bench", "version": "1.0"},
            })
            self._notify("notifications/initialized")
        except Exception:
            self.teardown()
            raise
        return SetupReport(indexed_files=0, wall_ms=(time.time() - t0) * 1000)

    def teardown(self) -> None:
        if self.p and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        self.p = None

    def query_symbol(self, name: str, limit: int) -> QueryResult:
        if self.p is None:
            raise RuntimeError("MemtraceAdapter: call setup() before query_symbol()")
        t0 = time.time()
        resp = self._rpc("tools/call", {
            "name": "find_symbol",
            "arguments": {"name": name, "limit": limit},
        })
        latency_ms = (time.time() - t0) * 1000
        text = ""
        paths: list[str] = []
        ranked: list[SymbolRef] = []
        if resp and "result" in resp:
            for c in resp["result"].get("content", []):
                if c.get("type") == "text":
                    text += c.get("text", "")
            try:
                data = json.loads(text)
                for r in data.get("results", []):
                    fp = r.get("file_path")
                    if fp and fp not in paths:
                        paths.append(fp)
                        ranked.append(SymbolRef(
                            name=r.get("name", name),
                            file_path=fp,
                            line=r.get("line"),
                        ))
            except json.JSONDecodeError:
                pass
        return QueryResult(
            paths=paths, ranked_symbols=ranked, raw_response_text=text,
            latency_ms=latency_ms, tokens_used=len(text) // 4,
        )

    def _rpc(self, method: str, params: dict) -> dict | None:
        rid = str(uuid.uuid4())
        self.p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        ) + "\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid:
                return msg

    def _notify(self, method: str, params: dict | None = None) -> None:
        self.p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params or {}}
        ) + "\n")
        self.p.stdin.flush()

    # ── Bench #3: graph queries ──────────────────────────────────────────────
    #
    # analyze_relationships / get_impact / find_dead_code all return MCP
    # text-content JSON on success, a JSON-RPC "error" object on failure.
    # We marshal the success path into GraphResult(nodes=[SymbolRef]); on
    # "Symbol not found" the adapter returns GraphResult(nodes=[]) so the
    # bench scorer records 0 recall (not NotSupported, which would be an
    # honest-loss disclosure for tools that can't do this at all).

    def _call_tool(self, name: str, arguments: dict) -> tuple[dict | None, float, str]:
        """Invoke a Memtrace MCP tool. Returns (parsed_json_or_None, latency_ms, raw_text)."""
        t0 = time.time()
        resp = self._rpc("tools/call", {"name": name, "arguments": arguments})
        latency_ms = (time.time() - t0) * 1000
        text = ""
        parsed: dict | None = None
        if resp and "result" in resp:
            for c in resp["result"].get("content", []):
                if c.get("type") == "text":
                    text += c.get("text", "")
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
        return parsed, latency_ms, text

    def callers_of(self, name: str) -> GraphResult | NotSupported:
        if self.p is None:
            raise RuntimeError("MemtraceAdapter: call setup() before callers_of()")
        parsed, lat, _ = self._call_tool("analyze_relationships", {
            "target": name, "query_type": "find_callers", "repo_id": self.repo_id,
        })
        return self._graph_from_relationships(parsed, lat)

    def callees_of(self, name: str) -> GraphResult | NotSupported:
        if self.p is None:
            raise RuntimeError("MemtraceAdapter: call setup() before callees_of()")
        parsed, lat, _ = self._call_tool("analyze_relationships", {
            "target": name, "query_type": "find_callees", "repo_id": self.repo_id,
        })
        return self._graph_from_relationships(parsed, lat)

    def impact_of(self, name: str) -> GraphResult | NotSupported:
        if self.p is None:
            raise RuntimeError("MemtraceAdapter: call setup() before impact_of()")
        parsed, lat, _ = self._call_tool("get_impact", {
            "target": name, "direction": "upstream", "repo_id": self.repo_id,
        })
        nodes: list[SymbolRef] = []
        if parsed:
            # Response shape: {by_depth: {"1": [AffectedSymbol, ...], "2": [...]}}
            by_depth = parsed.get("by_depth") or {}
            for _d, syms in by_depth.items():
                for s in syms or []:
                    nodes.append(SymbolRef(
                        name=s.get("name", ""),
                        file_path=s.get("file_path", ""),
                        line=None,
                    ))
        return GraphResult(nodes=nodes, latency_ms=lat)

    def find_dead_code(self) -> GraphResult | NotSupported:
        if self.p is None:
            raise RuntimeError("MemtraceAdapter: call setup() before find_dead_code()")
        parsed, lat, _ = self._call_tool("find_dead_code", {
            "repo_id": self.repo_id, "limit": 200,
        })
        nodes: list[SymbolRef] = []
        if parsed:
            for s in parsed.get("dead_code", []) or []:
                nodes.append(SymbolRef(
                    name=s.get("name", ""),
                    file_path=s.get("file_path", ""),
                    line=s.get("start_line"),
                ))
        return GraphResult(nodes=nodes, latency_ms=lat)

    @staticmethod
    def _graph_from_relationships(parsed: dict | None, lat: float) -> GraphResult:
        """analyze_relationships success response shape:
        {"results": [{"name": ..., "file_path": ..., "start_line": ..., ...}, ...], ...}"""
        nodes: list[SymbolRef] = []
        if parsed:
            for r in parsed.get("results", []) or []:
                nodes.append(SymbolRef(
                    name=r.get("name", ""),
                    file_path=r.get("file_path", ""),
                    line=r.get("start_line"),
                ))
        return GraphResult(nodes=nodes, latency_ms=lat)

    # ── Bench #4: incremental indexing ───────────────────────────────────────

    _corpus_root: Path | None = None  # set by ensure_indexed; used by reindex_paths

    def ensure_indexed(self, path: Path, timeout_s: int = 180) -> dict:
        """Make sure `path` is indexed in Memtrace's graph under `self.repo_id`.

        Called by the Bench #4 driver once per scratch_fixture run (the bench
        corpus is not a real git repo, and the fair harness relies on
        mempalace being pre-indexed by the user). Polls `check_job_status`
        until the indexing job reports `state: completed` or the timeout
        elapses.

        Returns the final job status dict (may be empty on timeout).
        """
        if self.p is None:
            raise RuntimeError("MemtraceAdapter: call setup() before ensure_indexed()")
        self._corpus_root = Path(path).resolve()
        # Best-effort: delete any prior scratch_fixture repo state so the
        # incoming MERGE starts from empty. Non-fatal — if the repo isn't
        # indexed yet, delete_repository reports 0 deleted.
        self._call_tool("delete_repository", {"repo_id": self.repo_id})
        parsed, _, _ = self._call_tool("index_directory", {
            "path": str(path.resolve()),
            "repo_id": self.repo_id,
            "incremental": False,
            "clear_existing": True,
            "skip_embed": True,  # Bench #4 cares about structure, not vectors.
        })
        if not parsed or "job_id" not in parsed:
            return parsed or {}
        job_id = parsed["job_id"]
        deadline = time.time() + timeout_s
        last: dict = {}
        while time.time() < deadline:
            status, _, _ = self._call_tool("check_job_status", {"job_id": job_id})
            last = status or {}
            state = (last.get("state") or last.get("status") or "").lower()
            if state in {"completed", "complete", "failed", "error"}:
                return last
            time.sleep(0.5)
        return last

    def reindex_paths(self, paths: list[Path]) -> ReindexReport | NotSupported:
        """Trigger Memtrace to pick up edits by calling `index_directory`
        with `incremental=true`, then polling `check_job_status` for done.

        `detect_changes` is an impact analyser (it reports what the edit
        AFFECTS in the existing graph) rather than a re-indexer — so we
        use `index_directory` here, which is the documented path for
        picking up changed-file parses. `skip_embed=true` keeps this fast
        (Bench #4 measures structural freshness, not embedding refresh).

        Paths are accepted but not forwarded: Memtrace re-scans the repo
        root (cheap with `incremental=true`). The `paths` argument is
        still recorded in the report so the driver can compare per-edit
        file counts across adapters.
        """
        if self.p is None:
            raise RuntimeError("MemtraceAdapter: call setup() before reindex_paths()")
        root = getattr(self, "_corpus_root", None)
        if root is None:
            # No corpus root known — best we can do is detect_changes as a
            # signal, since index_directory needs a path. This path is only
            # hit in tests that bypass ensure_indexed.
            return ReindexReport(files_reindexed=0, wall_ms=0.0, incremental=True)
        t0 = time.time()
        parsed, _, _ = self._call_tool("index_directory", {
            "path": str(root),
            "repo_id": self.repo_id,
            "incremental": True,
            "clear_existing": False,
            "skip_embed": True,
        })
        job_id = (parsed or {}).get("job_id")
        if job_id:
            # Poll until the incremental reindex job is done (should be
            # fast — it only parses changed files).
            deadline = time.time() + 30.0
            while time.time() < deadline:
                status, _, _ = self._call_tool("check_job_status", {"job_id": job_id})
                state = ((status or {}).get("state")
                         or (status or {}).get("status") or "").lower()
                if state in {"completed", "complete", "failed", "error"}:
                    break
                time.sleep(0.05)
        wall_ms = (time.time() - t0) * 1000
        return ReindexReport(files_reindexed=len(paths), wall_ms=wall_ms, incremental=True)

    def _relpath_for_detect(self, p: Path) -> str:
        """Best-effort path normalisation for `detect_changes`.

        detect_changes stores paths as "<repo_id>/<rel>". We prefer an
        absolute-to-relative-to-corpus-root transform when the corpus root
        is known from ensure_indexed; otherwise fall back to `<repo_id>/<basename>`.
        """
        root = getattr(self, "_corpus_root", None)
        if root is not None:
            try:
                rel = Path(p).resolve().relative_to(Path(root).resolve())
                return f"{self.repo_id}/{rel.as_posix()}"
            except ValueError:
                pass
        return f"{self.repo_id}/{Path(p).name}"

    def time_to_queryable(self, name: str, deadline_ms: int) -> float:
        """Poll find_symbol until it returns a hit or `deadline_ms` elapses.

        Returns wall-time in ms when the symbol becomes queryable; returns
        `deadline_ms + 1` (a sentinel beyond the deadline) on timeout so
        Bench #4 p95 reflects the miss as a ceiling rather than silently
        dropping the sample.
        """
        if self.p is None:
            raise RuntimeError("MemtraceAdapter: call setup() before time_to_queryable()")
        t0 = time.time()
        poll_interval_s = 0.05
        while True:
            elapsed_ms = (time.time() - t0) * 1000
            if elapsed_ms > deadline_ms:
                return float(deadline_ms + 1)
            res = self.query_symbol(name, limit=5)
            if res.paths:
                return (time.time() - t0) * 1000
            time.sleep(poll_interval_s)
