"""GitNexus adapter — eval-server over HTTP POST /tool/query."""
from __future__ import annotations
import json
import re
import time
from urllib import request

from benchmarks.suite.contract import (
    Adapter, GraphResult, NotSupported, QueryResult, ReindexReport,
    SetupReport, SymbolRef,
)
from pathlib import Path


DEFAULT_URL = "http://localhost:4848/tool/query"


class GitNexusAdapter(Adapter):
    name = "gitnexus"
    description = "GitNexus eval-server (POST /tool/query)"
    version = "gitnexus@eval-server"  # pinned to concrete version in versions.toml

    ARROW = re.compile(r"→\s+([A-Za-z0-9_./\-]+\.(?:py|ts|tsx|js|rs|go|java))")
    COLON = re.compile(r"\s([A-Za-z0-9_./\-]+\.(?:py|ts|tsx|js|rs|go|java)):\d+")

    def __init__(self, url: str = DEFAULT_URL):
        self.url = url
        self._corpus_path: str | None = None
        # Multi-repo disambiguation: the eval-server accepts a `repo` parameter
        # when more than one repo is indexed locally (e.g., mempalace AND django).
        # Without it you get `Error: Multiple repositories indexed. Specify which
        # one with the "repo" parameter.`  Captured from corpus.name in setup.
        self._corpus_name: str | None = None
        self._server_up = False

    def setup(self, corpus) -> SetupReport:
        self._corpus_path = str(corpus.path)
        self._corpus_name = getattr(corpus, "name", None)
        t0 = time.time()
        self._server_up = self._ping()
        return SetupReport(indexed_files=0, wall_ms=(time.time() - t0) * 1000)

    def teardown(self) -> None:
        # eval-server is externally managed; nothing to clean up here.
        pass

    def query_symbol(self, name: str, limit: int) -> QueryResult:
        if not self._server_up:
            return QueryResult(raw_response_text="<server down>", latency_ms=0.0)
        t0 = time.time()
        paths: list[str] = []
        text = ""
        try:
            body = json.dumps({"query": name, "targetDir": self._corpus_path}).encode()
            req = request.Request(self.url, data=body,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
            with request.urlopen(req, timeout=10) as r:
                text = r.read().decode("utf-8", errors="ignore")
            for m in self.ARROW.finditer(text):
                rel = m.group(1)
                full = f"mempalace/{rel}" if not rel.startswith("mempalace/") else rel
                if full not in paths:
                    paths.append(full)
            for m in self.COLON.finditer(text):
                rel = m.group(1)
                full = f"mempalace/{rel}" if not rel.startswith("mempalace/") else rel
                if full not in paths:
                    paths.append(full)
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
    # GitNexus eval-server exposes a single /tool/query endpoint that returns
    # free-text flow-style descriptions. We issue natural-language probes
    # ("callers of X", "what does X call") and regex-extract file paths from
    # the response. This is a best-effort path — if eval-server returns no
    # matches, we surface an empty GraphResult rather than NotSupported
    # because the system *claims* graph capability; emptiness is its score,
    # not its opt-out.

    def callers_of(self, name: str) -> GraphResult:
        return self._graph_probe(f"callers of {name}")

    def callees_of(self, name: str) -> GraphResult:
        return self._graph_probe(f"what does {name} call")

    def impact_of(self, name: str) -> GraphResult:
        return self._graph_probe(f"impact of changing {name}")

    def find_dead_code(self) -> GraphResult | NotSupported:
        # eval-server has no documented dead-code query. Disclose honestly.
        return NotSupported(reason="gitnexus eval-server has no dead-code query")

    # Proper parsers for the eval-server flow-text format.
    #
    # Known sections in responses to "callers of X" / "what does X call" /
    # "impact of changing X":
    #
    # 1. "Found N execution flow(s):"
    #      Each flow is numbered. The flow START is the caller (or chain
    #      root); the end is the target. Format:
    #        1. FlowName → EndSymbol (4 steps, 1 symbols)
    #           undefined EndSymbol → mempalace/backends/chroma.py:179
    #      The flow-head name (e.g. `FlowName`) is a real caller-side symbol.
    #
    # 2. "Standalone definitions:"
    #      Flat list of symbols that reference the target. Format:
    #        Symbol <name> → <relative/path.py>
    #      These are direct callers (what pyright would call "incomingCalls").
    #
    # The earlier ARROW/COLON regex extracted only file paths. This parser
    # pulls SYMBOL NAMES too, which is what name-based scoring needs.
    _FLOW_HEAD_RE = re.compile(
        r"^\s*\d+\.\s+([A-Za-z_][A-Za-z0-9_]*)\s+→\s+([A-Za-z_][A-Za-z0-9_]*)",
        re.MULTILINE,
    )
    _STANDALONE_RE = re.compile(
        r"^\s*Symbol\s+([A-Za-z_][A-Za-z0-9_.]*)\s+→\s+([A-Za-z0-9_./\-]+\.(?:py|ts|tsx|js|rs|go|java))",
        re.MULTILINE,
    )

    def _graph_probe(self, nl_query: str) -> GraphResult:
        if not self._server_up:
            return GraphResult(nodes=[], latency_ms=0.0)
        t0 = time.time()
        nodes: list[SymbolRef] = []
        seen: set[tuple[str, str]] = set()
        try:
            body_data = {"query": nl_query, "targetDir": self._corpus_path}
            if self._corpus_name:
                body_data["repo"] = self._corpus_name
            body = json.dumps(body_data).encode()
            req = request.Request(self.url, data=body,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
            with request.urlopen(req, timeout=15) as r:
                text = r.read().decode("utf-8", errors="ignore")

            # (a) Flow heads — the left-hand symbol in "N. Head → End"
            #     are graph-predecessors of the query target; these are
            #     real callers per GitNexus's flow model.
            for m in self._FLOW_HEAD_RE.finditer(text):
                head_name = m.group(1)
                key = (head_name, "")
                if key not in seen:
                    seen.add(key)
                    nodes.append(SymbolRef(name=head_name, file_path="", line=None))

            # (b) Standalone definitions — direct caller list
            for m in self._STANDALONE_RE.finditer(text):
                sym_name = m.group(1)
                file_rel = m.group(2)
                key = (sym_name, file_rel)
                if key not in seen:
                    seen.add(key)
                    nodes.append(SymbolRef(name=sym_name, file_path=file_rel, line=None))

            # (c) Legacy path-only fallback — for any line like
            #     "... → file.py:lineno" not covered above, record the
            #     file without a name. This keeps any signal that the
            #     newer parsers miss (name-less flow tails etc.).
            for m in self.ARROW.finditer(text):
                rel = m.group(1)
                key = ("", rel)
                if key not in seen and not any(rel == p for _, p in seen if p):
                    seen.add(key)
                    nodes.append(SymbolRef(name="", file_path=rel, line=None))
        except Exception:
            pass
        return GraphResult(nodes=nodes, latency_ms=(time.time() - t0) * 1000)

    # ── Bench #4: incremental indexing ───────────────────────────────────────

    def reindex_paths(self, paths: list[Path]) -> ReindexReport | NotSupported:
        """eval-server is batch-only. We re-trigger analysis by issuing a
        no-op query that forces the server to notice filesystem state.
        If eval-server is not up, report NotSupported."""
        if not self._server_up:
            return NotSupported(reason="gitnexus eval-server not reachable")
        t0 = time.time()
        try:
            body = json.dumps({
                "query": "refresh index",
                "targetDir": self._corpus_path,
            }).encode()
            req = request.Request(self.url, data=body,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
            with request.urlopen(req, timeout=120) as r:
                r.read()
        except Exception:
            pass
        return ReindexReport(
            files_reindexed=len(paths),
            wall_ms=(time.time() - t0) * 1000,
            incremental=False,  # eval-server has no documented incremental path
        )

    def time_to_queryable(self, name: str, deadline_ms: int) -> float:
        if not self._server_up:
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

    def _ping(self) -> bool:
        try:
            body_data = {"query": "ping", "targetDir": self._corpus_path}
            # Multi-repo eval-server rejects queries without `repo` when >1
            # repo is indexed.  Send it so ping works in either mode.
            if self._corpus_name:
                body_data["repo"] = self._corpus_name
            body = json.dumps(body_data).encode()
            req = request.Request(self.url, data=body,
                                  headers={"Content-Type": "application/json"},
                                  method="POST")
            with request.urlopen(req, timeout=5) as r:
                r.read()
            return True
        except Exception:
            return False
