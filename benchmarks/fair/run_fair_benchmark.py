"""
Fair multi-tool code-search benchmark.

Design notes
============

1. The dataset is extracted from mempalace's Python source files via the
   standard `ast` module (see extract_ground_truth.py).  It does NOT come
   from any tool's index.  Every system gets the same queries.

2. Each tool is called through a thin adapter that normalises its
   response into a list of file-path strings ranked best-first.  The
   adapter is:
     - Memtrace        find_symbol(name, limit=10) → JSON `results[].file_path`
     - ChromaDB        query(q, n_results=10)      → metadata.source (deduped)
     - GitNexus        POST /tool/query            → JSON `filePath` (deduped)
     - CGC             cgc find <name>             → symbol names ONLY.
                                                     Our adapter then runs
                                                     grep -rln "<name>" repo
                                                     to turn the symbol hit
                                                     into file paths — this
                                                     is the standard
                                                     "external-post-process"
                                                     allowance other IR
                                                     benchmarks use for
                                                     tools without path
                                                     output.  Disclosed
                                                     in results.

3. Metrics are reported as:
     - Coverage         did the tool index/find the symbol at all?
                        (= any response rows → 1, else 0)
     - Acc@1/5/10       does `expected_file` appear at position 1 / in
                        top-K returned paths?
     - Conditional Acc  Acc@1 restricted to queries where Coverage=1
                        (separates "parser coverage" from "search rank")
     - Latency          wall clock per query
     - Tokens           response character count / 4

4. Acc@1 is an unweighted substring match: "is `expected_file` (= 'mempalace/…/x.py')
   a substring of any path the adapter returned?"  Path separators and
   leading/trailing dirs are not normalised — the adapters are responsible
   for returning paths that contain the mempalace/<relpath> substring.
"""

import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib import request, error

HERE          = Path(__file__).parent
DATASET_FILE  = HERE / "dataset.json"
RESULTS_FILE  = HERE / "results.json"
MEMTRACE_BIN  = "/Users/alexthh/Desktop/ZeroToDemo/Memtrace/target/release/memtrace"
MEMPALACE     = Path("/Users/alexthh/Desktop/ZeroToDemo/mempalace")
MEMPALACE_PARENT = MEMPALACE.parent
CGC_BIN       = str(HERE.parent / ".venv/bin/cgc")
GN_URL        = "http://localhost:4848/tool/query"
MAX_QUERIES   = int(os.environ.get("MAX_QUERIES", "1000"))
LIMIT         = 10

# ─── Adapter: Memtrace (MCP JSON-RPC over stdio) ────────────────────────────────

class MemtraceAdapter:
    name = "memtrace"
    description = "Rust binary, MCP JSON-RPC over stdio, find_symbol tool"

    def __init__(self):
        self.p = subprocess.Popen(
            [MEMTRACE_BIN, "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "fair-bench", "version": "1.0"},
        })
        self._notify("notifications/initialized")

    def _rpc(self, method, params):
        rid = str(uuid.uuid4())
        self.p.stdin.write(json.dumps({"jsonrpc":"2.0","id":rid,"method":method,"params":params})+"\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line: return None
            try: msg = json.loads(line)
            except: continue
            if msg.get("id") == rid: return msg

    def _notify(self, method, params=None):
        self.p.stdin.write(json.dumps({"jsonrpc":"2.0","method":method,"params":params or {}})+"\n")
        self.p.stdin.flush()

    def query(self, symbol: str):
        t0 = time.time()
        resp = self._rpc("tools/call", {
            "name": "find_symbol",
            "arguments": {"name": symbol, "limit": LIMIT},
        })
        latency_ms = (time.time() - t0) * 1000
        text = ""
        paths = []
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
            except json.JSONDecodeError:
                pass
        return {"paths": paths, "latency_ms": latency_ms, "tokens": len(text)//4}

    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
            try: self.p.wait(timeout=5)
            except: self.p.kill()


# ─── Adapter: ChromaDB (in-process, sentence-transformers embeddings) ──────────

class ChromaDBAdapter:
    name = "chromadb"
    description = "chromadb 1.5 + sentence-transformers all-MiniLM-L6-v2, 800-char code chunks"

    def __init__(self):
        import chromadb
        self.client = chromadb.Client()
        try: self.client.delete_collection("fair_bench")
        except Exception: pass
        self.col = self.client.create_collection("fair_bench")
        self._index()

    def _index(self):
        print("  [chromadb] indexing mempalace (800-char chunks)...")
        docs, ids, metas = [], [], []
        idx = 0
        for root, dirs, files in os.walk(MEMPALACE):
            dirs[:] = [d for d in dirs if d not in {
                ".git","__pycache__",".venv","node_modules","dist","build",
                ".pytest_cache",".mypy_cache","target"}]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = Path(root) / fname
                rel   = fpath.relative_to(MEMPALACE_PARENT)
                try: content = fpath.read_text(encoding="utf-8", errors="ignore")
                except: continue
                for i in range(0, len(content), 800):
                    chunk = content[i:i+800]
                    if len(chunk.strip()) < 20:
                        continue
                    docs.append(chunk); ids.append(f"c{idx}")
                    metas.append({"source": str(rel)})
                    idx += 1
        # Batch-add in chunks of 500 to avoid the sentence-transformer OOM
        B = 500
        for i in range(0, len(docs), B):
            self.col.add(documents=docs[i:i+B], ids=ids[i:i+B], metadatas=metas[i:i+B])
        print(f"  [chromadb] indexed {len(docs)} chunks across mempalace")

    def query(self, symbol: str):
        t0 = time.time()
        res = self.col.query(query_texts=[symbol], n_results=LIMIT)
        latency_ms = (time.time() - t0) * 1000
        paths = []
        metas = res.get("metadatas", [[]])[0] if res.get("metadatas") else []
        docs  = res.get("documents", [[]])[0] if res.get("documents") else []
        for m in metas:
            src = m.get("source") if m else None
            if src and src not in paths:
                paths.append(src)
        tokens = sum(len(d or "") for d in docs) // 4
        return {"paths": paths, "latency_ms": latency_ms, "tokens": tokens}

    def close(self):
        pass


# ─── Adapter: GitNexus (eval-server HTTP) ──────────────────────────────────────

class GitNexusAdapter:
    name = "gitnexus"
    description = "GitNexus eval-server (POST /tool/query, JSON body)"

    def __init__(self):
        self.server_up = self._ping()

    def _ping(self):
        try:
            req = request.Request(
                GN_URL,
                data=json.dumps({"query":"ping","targetDir":str(MEMPALACE)}).encode(),
                headers={"Content-Type":"application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=3) as r:
                r.read()
            return True
        except Exception:
            return False

    def query(self, symbol: str):
        if not self.server_up:
            return {"paths": [], "latency_ms": 0.0, "tokens": 0, "unavailable": True}
        t0 = time.time()
        paths = []
        text = ""
        try:
            req = request.Request(
                GN_URL,
                data=json.dumps({"query":symbol,"targetDir":str(MEMPALACE)}).encode(),
                headers={"Content-Type":"application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=10) as r:
                text = r.read().decode("utf-8", errors="ignore")

            # GitNexus response is plain text.  Shapes we extract file paths from:
            #   "  Symbol <name> → <relative/path.py>"
            #   "  <something> <relative/path.py>:<lineno>"
            # We prepend "mempalace/" because GitNexus paths are repo-relative.
            import re
            arrow = re.compile(r"→\s+([A-Za-z0-9_./\-]+\.(?:py|ts|tsx|js|rs|go|java))")
            colon = re.compile(r"\s([A-Za-z0-9_./\-]+\.(?:py|ts|tsx|js|rs|go|java)):\d+")
            for m in arrow.finditer(text):
                rel = m.group(1)
                full = f"mempalace/{rel}" if not rel.startswith("mempalace/") else rel
                if full not in paths: paths.append(full)
            for m in colon.finditer(text):
                rel = m.group(1)
                full = f"mempalace/{rel}" if not rel.startswith("mempalace/") else rel
                if full not in paths: paths.append(full)
        except Exception as e:
            text = f"<error: {e}>"
        return {"paths": paths, "latency_ms": (time.time()-t0)*1000, "tokens": len(text)//4}

    def close(self):
        pass


# ─── Adapter: CodeGrapherContext (CLI + grep fallback) ─────────────────────────

class CGCAdapter:
    name = "cgc"
    description = "cgc find <name> CLI + grep fallback to recover file paths"

    def __init__(self):
        self.available = os.path.exists(CGC_BIN)
        # Cache grep results across queries to avoid re-greping per call
        self._grep_cache: dict[str, list[str]] = {}

    def _grep_files(self, symbol):
        if symbol in self._grep_cache:
            return self._grep_cache[symbol]
        try:
            out = subprocess.run(
                ["grep","-rln","-E",
                 f"(def {symbol}\\b|class {symbol}\\b|async def {symbol}\\b)",
                 str(MEMPALACE)],
                capture_output=True, text=True, timeout=5,
            )
            paths = []
            for line in out.stdout.splitlines():
                try:
                    rel = str(Path(line).relative_to(MEMPALACE_PARENT))
                    if rel not in paths: paths.append(rel)
                except ValueError:
                    pass
            self._grep_cache[symbol] = paths
            return paths
        except Exception:
            self._grep_cache[symbol] = []
            return []

    def query(self, symbol: str):
        if not self.available:
            return {"paths": [], "latency_ms": 0.0, "tokens": 0, "unavailable": True}
        t0 = time.time()
        text = ""
        paths = []
        try:
            # CGC's search CLI is `cgc find name <symbol>` — the `find`
            # subcommand groups multiple search modes (name/pattern/type/...).
            # Rich-console output goes to STDERR (not stdout).  NO_COLOR +
            # TERM=dumb disable ANSI, but the box-drawing table chars stay.
            out = subprocess.run(
                [CGC_BIN, "find", "name", symbol],
                capture_output=True, text=True, timeout=15,
                env={**os.environ, "CI":"1", "NO_COLOR":"1", "TERM":"dumb"},
                cwd=str(MEMPALACE),
            )
            text = out.stdout + "\n" + out.stderr
            # Rich-text table — `Location` column holds absolute paths that
            # wrap across lines:
            #   │ name │ Function │ /Users/.../mempalace         │
            #   │      │          │ /tests/foo/bar.py:38         │
            # Strip box-drawing, collapse whitespace, then regex the joined
            # absolute path back to a mempalace-relative form.
            import re
            cleaned = re.sub(r"[│┌┐└┘├┤┬┴┼╭╮╯╰─]", " ", text)
            cleaned = re.sub(r"\s+", " ", cleaned)
            abs_re = re.compile(r"/mempalace\s*(/[A-Za-z0-9_./\-]+?\.(?:py|ts|tsx|js|rs|go|java))")
            for m in abs_re.finditer(cleaned):
                rel = f"mempalace{m.group(1)}"
                if rel not in paths: paths.append(rel)
            # If the table wrapping defeated extraction but CGC clearly had
            # a hit, degrade to the shared grep fallback (disclosed in
            # adapter description).  This keeps the bench "coverage"
            # metric honest — a symbol CGC indexed doesn't become a miss
            # because of output formatting alone.
            if not paths and symbol.lower() in text.lower() and "Found" in text:
                paths = self._grep_files(symbol)
        except Exception as e:
            text = f"<error: {e}>"
        return {"paths": paths, "latency_ms": (time.time()-t0)*1000, "tokens": len(text)//4}

    def close(self):
        pass


# ─── Scoring ───────────────────────────────────────────────────────────────────

def score_one(expected_file: str, paths: list[str]) -> dict:
    """Return positional hit info — rank 1-based; None if miss."""
    for i, p in enumerate(paths, start=1):
        if expected_file in p or p in expected_file:
            return {"rank": i, "hit_in": "top_" + str(i)}
    return {"rank": None, "hit_in": None}


def summarise(name, desc, results):
    N = len(results)
    if N == 0: return None
    covered = [r for r in results if r["paths_count"] > 0]
    hit_at_1  = sum(1 for r in results if r["rank"] == 1)
    hit_at_5  = sum(1 for r in results if r["rank"] is not None and r["rank"] <= 5)
    hit_at_10 = sum(1 for r in results if r["rank"] is not None and r["rank"] <= 10)
    hits      = sum(1 for r in results if r["rank"] is not None)
    lats = sorted(r["latency_ms"] for r in results)
    p = lambda q: lats[min(int(len(lats)*q), len(lats)-1)]
    cond_acc1 = (sum(1 for r in covered if r["rank"] == 1) / len(covered) * 100) if covered else 0.0
    return {
        "adapter":      name,
        "description":  desc,
        "n_queries":    N,
        "coverage_pct": round(len(covered) / N * 100, 2),
        "acc_at_1_pct": round(hit_at_1 / N * 100, 2),
        "acc_at_5_pct": round(hit_at_5 / N * 100, 2),
        "acc_at_10_pct":round(hit_at_10 / N * 100, 2),
        "conditional_acc_at_1_pct": round(cond_acc1, 2),
        "avg_latency_ms":    round(statistics.mean(lats), 2),
        "median_latency_ms": round(statistics.median(lats), 2),
        "p95_latency_ms":    round(p(0.95), 2),
        "p99_latency_ms":    round(p(0.99), 2),
        "avg_tokens":   round(statistics.mean(r["tokens"] for r in results), 0),
    }


def run_adapter(adapter, dataset, max_n: int):
    results = []
    t0 = time.time()
    try:
        for i, c in enumerate(dataset[:max_n]):
            out = adapter.query(c["target_symbol"])
            if out.get("unavailable"):
                return None  # skip entirely, note in report
            scored = score_one(c["expected_file"], out["paths"])
            results.append({
                "id":            c["id"],
                "target":        c["target_symbol"],
                "expected_file": c["expected_file"],
                "paths_count":   len(out["paths"]),
                "top_paths":     out["paths"][:3],
                "rank":          scored["rank"],
                "latency_ms":    out["latency_ms"],
                "tokens":        out["tokens"],
            })
            if (i + 1) % 100 == 0:
                acc1 = sum(1 for r in results if r["rank"] == 1) / len(results) * 100
                cov  = sum(1 for r in results if r["paths_count"] > 0) / len(results) * 100
                print(f"  [{adapter.name}] {i+1}/{max_n}  acc@1={acc1:.1f}%  cov={cov:.1f}%")
    finally:
        adapter.close()
    wall = time.time() - t0
    return {"results": results, "wall_seconds": round(wall, 2)}


def main():
    with open(DATASET_FILE) as f:
        dataset = json.load(f)
    n = min(MAX_QUERIES, len(dataset))
    print(f"Fair benchmark — {n} queries, dataset from Python AST (tool-independent)\n")

    adapters = [
        MemtraceAdapter(),
        ChromaDBAdapter(),
        GitNexusAdapter(),
        CGCAdapter(),
    ]

    report = {"n_queries": n, "dataset": "mempalace Python AST ground truth (1000 queries)", "tools": {}}

    for ad in adapters:
        print(f"\n── {ad.name} ──  {ad.description}")
        result = run_adapter(ad, dataset, n)
        if result is None:
            print(f"  [{ad.name}] UNAVAILABLE — skipped")
            report["tools"][ad.name] = {"status": "unavailable"}
            continue
        summary = summarise(ad.name, ad.description, result["results"])
        summary["wall_seconds"] = result["wall_seconds"]
        report["tools"][ad.name] = summary
        print(f"  → {json.dumps(summary, indent=4)}")

    with open(RESULTS_FILE, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n✓ wrote {RESULTS_FILE}")


if __name__ == "__main__":
    sys.exit(main())
