"""ChromaDB adapter — sentence-transformers all-MiniLM-L6-v2, 800-char chunks.

Ported from benchmarks/fair. Indexing moved from __init__ to setup() and
reports wall-time back to the runner."""
from __future__ import annotations
import os
import time
from pathlib import Path

from benchmarks.suite.contract import (
    Adapter, GraphResult, NotSupported, QueryResult, ReindexReport,
    SetupReport, SymbolRef,
)


SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", "dist",
             "build", ".pytest_cache", ".mypy_cache", "target"}


class ChromaDBAdapter(Adapter):
    name = "chromadb"
    description = "chromadb + sentence-transformers all-MiniLM-L6-v2, 800-char chunks"
    version = "chromadb==1.5.7; model=all-MiniLM-L6-v2"

    def __init__(self, collection_name: str = "suite_bench"):
        self.collection_name = collection_name
        self.client = None
        self.col = None
        self._indexed_chunks = 0
        self._corpus_parent: Path | None = None

    def setup(self, corpus) -> SetupReport:
        import chromadb
        self._corpus_parent = corpus.parent
        t0 = time.time()
        self.client = chromadb.Client()
        try:
            self.client.delete_collection(self.collection_name)
        except Exception:
            pass
        self.col = self.client.create_collection(self.collection_name)

        docs, ids, metas = [], [], []
        idx = 0
        indexed_files = 0
        for root, dirs, files in os.walk(corpus.path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = Path(root) / fname
                rel = fpath.relative_to(corpus.parent)
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                file_produced_chunk = False
                for i in range(0, len(content), 800):
                    chunk = content[i:i+800]
                    if len(chunk.strip()) < 20:
                        continue
                    docs.append(chunk)
                    ids.append(f"c{idx}")
                    metas.append({"source": str(rel)})
                    idx += 1
                    file_produced_chunk = True
                if file_produced_chunk:
                    indexed_files += 1
        B = 500
        try:
            for i in range(0, len(docs), B):
                self.col.add(documents=docs[i:i+B], ids=ids[i:i+B], metadatas=metas[i:i+B])
        except Exception:
            # Partial-index recovery: drop the half-populated collection so a
            # retry starts clean and we don't leak a malformed Chroma state.
            self.teardown()
            raise
        self._indexed_chunks = len(docs)
        return SetupReport(indexed_files=indexed_files, wall_ms=(time.time() - t0) * 1000)

    def teardown(self) -> None:
        if self.client and self.col:
            try:
                self.client.delete_collection(self.collection_name)
            except Exception:
                pass
        self.client = None
        self.col = None

    # ── Bench #3: graph queries (ChromaDB has no graph — the N/A IS the point) ──
    # These are the honest-loss disclosures. A vector DB cannot answer
    # structural graph queries, so returning NotSupported (rather than a
    # best-effort string match) is the correct behaviour.

    def callers_of(self, name: str) -> NotSupported:
        return NotSupported(reason="vector DB has no graph")

    def callees_of(self, name: str) -> NotSupported:
        return NotSupported(reason="vector DB has no graph")

    def impact_of(self, name: str) -> NotSupported:
        return NotSupported(reason="vector DB has no graph")

    def find_dead_code(self) -> NotSupported:
        return NotSupported(reason="vector DB has no graph")

    # ── Bench #4: incremental indexing ───────────────────────────────────────
    #
    # ChromaDB does support incremental upsert, but it has no concept of
    # "symbol"-level edits. We re-embed the chunks for the listed files
    # (drop the old chunks by source metadata, add the new ones) and poll
    # `query()` until the expected symbol string appears in the result set.

    def reindex_paths(self, paths: list[Path]) -> ReindexReport:
        if self.col is None:
            raise RuntimeError("ChromaDBAdapter: call setup() before reindex_paths()")
        t0 = time.time()
        files_touched = 0
        for p in paths:
            fpath = Path(p)
            if not fpath.exists() or not fpath.suffix == ".py":
                continue
            # Drop existing chunks with this source path. `setup` keys by
            # `fpath.relative_to(corpus.parent)`, so use the same transform
            # here — otherwise stale pre-edit chunks survive forever and
            # the staleness rate balloons.
            if self._corpus_parent is not None:
                try:
                    rel = fpath.resolve().relative_to(self._corpus_parent.resolve())
                except ValueError:
                    rel = fpath
            else:
                rel = fpath
            src_str = str(rel)
            try:
                self.col.delete(where={"source": src_str})
            except Exception:
                pass
            # Re-embed.
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            new_docs, new_ids, new_metas = [], [], []
            base = abs(hash(src_str))
            for i in range(0, len(content), 800):
                chunk = content[i:i+800]
                if len(chunk.strip()) < 20:
                    continue
                new_docs.append(chunk)
                new_ids.append(f"reidx-{base}-{i}")
                new_metas.append({"source": src_str})
            if new_docs:
                self.col.add(documents=new_docs, ids=new_ids, metadatas=new_metas)
                files_touched += 1
        return ReindexReport(
            files_reindexed=files_touched,
            wall_ms=(time.time() - t0) * 1000,
            incremental=True,
        )

    def time_to_queryable(self, name: str, deadline_ms: int) -> float:
        """Poll `query(name)` until the returned documents contain the symbol."""
        if self.col is None:
            raise RuntimeError("ChromaDBAdapter: call setup() before time_to_queryable()")
        t0 = time.time()
        poll_interval_s = 0.05
        while True:
            elapsed_ms = (time.time() - t0) * 1000
            if elapsed_ms > deadline_ms:
                return float(deadline_ms + 1)
            res = self.col.query(query_texts=[name], n_results=3)
            docs = res.get("documents", [[]])[0] if res.get("documents") else []
            if any(name in (d or "") for d in docs):
                return (time.time() - t0) * 1000
            time.sleep(poll_interval_s)

    def query_symbol(self, name: str, limit: int) -> QueryResult:
        if self.col is None:
            raise RuntimeError("ChromaDBAdapter: call setup() first")
        t0 = time.time()
        res = self.col.query(query_texts=[name], n_results=limit)
        latency_ms = (time.time() - t0) * 1000
        paths: list[str] = []
        metas = res.get("metadatas", [[]])[0] if res.get("metadatas") else []
        docs = res.get("documents", [[]])[0] if res.get("documents") else []
        for m in metas:
            src = m.get("source") if m else None
            if src and src not in paths:
                paths.append(src)
        tokens = sum(len(d or "") for d in docs) // 4
        raw = "\n---\n".join(d or "" for d in docs)
        return QueryResult(
            paths=paths,
            ranked_symbols=[SymbolRef(name=name, file_path=p, line=None) for p in paths],
            raw_response_text=raw,
            latency_ms=latency_ms,
            tokens_used=tokens,
        )
