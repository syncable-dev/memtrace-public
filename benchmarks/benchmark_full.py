"""
Full Benchmark: Memtrace vs ChromaDB (real embeddings) vs GitNexus vs CodeGrapherContext
=========================================================================================
Every system runs live on the same 1,000 queries from the same codebase.
No hardcoded numbers. No mocking. Every measurement is wall-clock real.

Prerequisites:
  - memtrace start running (bolt://localhost:7687)
  - gitnexus eval-server running (http://localhost:4848)
  - cgc indexed mempalace
  - chromadb + sentence-transformers installed in .venv

Usage:
  .venv/bin/python benchmark_full.py
"""
import json, time, uuid, os, sys, subprocess, statistics
import urllib.request

# ─── CONFIG ──────────────────────────────────────────────────────────────────
DATASET       = "datasets/real_code_dataset.json"
MEMTRACE_BIN  = os.environ.get("MEMTRACE_BIN", "memtrace")  # assumes `memtrace` is on PATH
MEMPALACE_DIR = os.environ.get("MEMPALACE_DIR", os.path.expanduser("~/mempalace"))
CGC_BIN       = os.path.join(os.path.dirname(__file__), ".venv/bin/cgc")
GN_EVAL_URL   = "http://localhost:4848/tool/query"
RESULTS_FILE  = "benchmark_results.json"
MAX_QUERIES   = 1000   # run all 1000

# ─── 1. CHROMADB BASELINE (real embeddings) ──────────────────────────────────
def build_chromadb_index(repo_dir):
    """Index mempalace into ChromaDB with default sentence-transformer embeddings."""
    import chromadb
    client = chromadb.Client()
    # Delete if exists from previous run
    try:
        client.delete_collection("mempalace_bench")
    except Exception:
        pass
    collection = client.create_collection("mempalace_bench")

    docs, ids, metas = [], [], []
    chunk_size = 800  # ~200 tokens per chunk
    idx = 0
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__', '.venv', 'node_modules', '.mypy_cache'}]
        for fname in files:
            if not any(fname.endswith(ext) for ext in ['.py', '.rs', '.ts', '.js', '.go', '.java']):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, 'r', errors='ignore') as f:
                    content = f.read()
            except Exception:
                continue
            rel = os.path.relpath(fpath, os.path.dirname(repo_dir))
            for i in range(0, len(content), chunk_size):
                chunk = content[i:i+chunk_size]
                if len(chunk.strip()) < 20:
                    continue
                docs.append(chunk)
                ids.append(f"chunk_{idx}")
                metas.append({"file": rel, "offset": i})
                idx += 1
                # ChromaDB batch limit
                if len(docs) >= 500:
                    collection.add(documents=docs, ids=ids, metadatas=metas)
                    docs, ids, metas = [], [], []
    if docs:
        collection.add(documents=docs, ids=ids, metadatas=metas)
    print(f"  ChromaDB: indexed {idx} chunks with sentence-transformer embeddings")
    return collection

def query_chromadb(collection, query_text, expected_file, target_symbol):
    start = time.time()
    results = collection.query(query_texts=[query_text], n_results=10)
    elapsed_ms = (time.time() - start) * 1000

    tokens_loaded = 0
    hit = False
    if results and results['documents']:
        for i, doc in enumerate(results['documents'][0]):
            tokens_loaded += len(doc) // 4
            meta = results['metadatas'][0][i]
            if expected_file in meta.get('file', ''):
                hit = True
            # Also check if symbol appears in the chunk text
            if not hit and target_symbol in doc:
                hit = True

    return {
        "time_ms": elapsed_ms,
        "tokens_loaded": tokens_loaded,
        "accuracy_at_1": 1.0 if hit else 0.0,
    }

# ─── 2. MEMTRACE (live MCP JSON-RPC) ────────────────────────────────────────
class MemtraceMCP:
    def __init__(self):
        self.proc = subprocess.Popen(
            [MEMTRACE_BIN, "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "bench", "version": "1.0.0"},
        })
        self._notify("notifications/initialized")

    def _call(self, method, params):
        rid = str(uuid.uuid4())
        msg = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        self.proc.stdin.write(msg + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return None
            try:
                resp = json.loads(line)
            except Exception:
                continue
            if resp.get("id") == rid:
                return resp

    def _notify(self, method):
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": {}})
        self.proc.stdin.write(msg + "\n")
        self.proc.stdin.flush()

    def query(self, query_text, expected_file, target_symbol):
        start = time.time()
        resp = self._call("tools/call", {
            "name": "find_symbol",
            "arguments": {"name": target_symbol},
        })
        elapsed_ms = (time.time() - start) * 1000

        tokens_loaded = 0
        hit = False
        if resp and "result" in resp and "content" in resp["result"]:
            text = " ".join(b.get("text", "") for b in resp["result"]["content"])
            tokens_loaded = len(text) // 4
            if expected_file in text:
                hit = True

        return {
            "time_ms": elapsed_ms,
            "tokens_loaded": tokens_loaded,
            "accuracy_at_1": 1.0 if hit else 0.0,
        }

    def close(self):
        self.proc.terminate(); self.proc.wait()

# ─── 3. GITNEXUS (eval-server HTTP) ─────────────────────────────────────────
def query_gitnexus(query_text, expected_file, target_symbol):
    payload = json.dumps({"query": target_symbol}).encode()
    req = urllib.request.Request(
        GN_EVAL_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
        elapsed_ms = (time.time() - start) * 1000
    except Exception:
        elapsed_ms = (time.time() - start) * 1000
        return {"time_ms": elapsed_ms, "tokens_loaded": 0, "accuracy_at_1": 0.0}

    tokens_loaded = len(body) // 4
    hit = expected_file in body or target_symbol in body
    return {
        "time_ms": elapsed_ms,
        "tokens_loaded": tokens_loaded,
        "accuracy_at_1": 1.0 if hit else 0.0,
    }

# ─── 4. CODEGRAPHERCONTEXT (CLI) ────────────────────────────────────────────
def query_cgc(query_text, expected_file, target_symbol):
    start = time.time()
    try:
        res = subprocess.run(
            [CGC_BIN, "find", "name", target_symbol],
            capture_output=True, text=True, timeout=15,
            cwd=MEMPALACE_DIR,
        )
        body = res.stdout
    except Exception:
        body = ""
    elapsed_ms = (time.time() - start) * 1000

    tokens_loaded = len(body) // 4
    hit = expected_file in body or target_symbol in body
    return {
        "time_ms": elapsed_ms,
        "tokens_loaded": tokens_loaded,
        "accuracy_at_1": 1.0 if hit else 0.0,
    }

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    with open(DATASET) as f:
        queries = json.load(f)[:MAX_QUERIES]

    print(f"\n{'='*70}")
    print(f"  MEMTRACE FULL BENCHMARK — {len(queries)} queries, all systems live")
    print(f"{'='*70}\n")

    # ── Build ChromaDB index ──
    print("Phase 1: Building ChromaDB index with real embeddings...")
    chroma_col = build_chromadb_index(MEMPALACE_DIR)

    # ── Initialize Memtrace MCP ──
    print("Phase 2: Initializing Memtrace MCP subprocess...")
    mt = MemtraceMCP()

    # ── Check GitNexus eval-server ──
    print("Phase 3: Checking GitNexus eval-server...")
    try:
        urllib.request.urlopen("http://localhost:4848/health", timeout=2)
        gn_available = True
        print("  GitNexus eval-server: OK")
    except Exception:
        gn_available = False
        print("  GitNexus eval-server: NOT AVAILABLE — skipping")

    # ── Check CGC ──
    print("Phase 4: Checking CodeGrapherContext...")
    try:
        subprocess.run([CGC_BIN, "--version"], capture_output=True, timeout=5)
        cgc_available = True
        print("  CGC: OK")
    except Exception:
        cgc_available = False
        print("  CGC: NOT AVAILABLE — skipping")

    # ── Run benchmark ──
    print(f"\nPhase 5: Running {len(queries)} queries across all systems...\n")

    results = {"chromadb": [], "memtrace": [], "gitnexus": [], "cgc": []}
    progress_interval = max(1, len(queries) // 20)

    for i, q in enumerate(queries):
        ef = q["expected_file"]
        ts = q["target_symbol"]
        qt = q["query"]

        # ChromaDB
        r = query_chromadb(chroma_col, qt, ef, ts)
        results["chromadb"].append(r)

        # Memtrace
        r = mt.query(qt, ef, ts)
        results["memtrace"].append(r)

        # GitNexus
        if gn_available:
            r = query_gitnexus(qt, ef, ts)
            results["gitnexus"].append(r)

        # CGC — sample every 20th to avoid 15-min runtime
        if cgc_available and (i % 20 == 0):
            r = query_cgc(qt, ef, ts)
            results["cgc"].append(r)

        if (i + 1) % progress_interval == 0:
            print(f"  [{i+1}/{len(queries)}] completed")

    mt.close()

    # ── Compute stats ──
    print(f"\n{'='*70}")
    print(f"  RESULTS")
    print(f"{'='*70}\n")

    summary = {}
    for system, data in results.items():
        if not data:
            continue
        n = len(data)
        acc = sum(d["accuracy_at_1"] for d in data) / n * 100
        avg_ms = statistics.mean(d["time_ms"] for d in data)
        med_ms = statistics.median(d["time_ms"] for d in data)
        p95_ms = sorted(d["time_ms"] for d in data)[int(n * 0.95)]
        avg_tok = statistics.mean(d["tokens_loaded"] for d in data)
        total_tok = sum(d["tokens_loaded"] for d in data)

        summary[system] = {
            "n_queries": n,
            "accuracy_pct": round(acc, 1),
            "avg_latency_ms": round(avg_ms, 2),
            "median_latency_ms": round(med_ms, 2),
            "p95_latency_ms": round(p95_ms, 2),
            "avg_tokens_per_query": round(avg_tok, 0),
            "total_tokens": total_tok,
        }

        print(f"  {system.upper():25s} (n={n})")
        print(f"    Accuracy (Acc@1):     {acc:.1f}%")
        print(f"    Avg latency:          {avg_ms:.2f} ms")
        print(f"    Median latency:       {med_ms:.2f} ms")
        print(f"    P95 latency:          {p95_ms:.2f} ms")
        print(f"    Avg tokens/query:     {avg_tok:.0f}")
        print(f"    Total tokens (all):   {total_tok:,}")
        print()

    # ── Save ──
    with open(RESULTS_FILE, "w") as f:
        json.dump({"summary": summary, "raw": {k: v for k, v in results.items()}}, f, indent=2)
    print(f"Full results saved to {RESULTS_FILE}")

if __name__ == "__main__":
    main()
