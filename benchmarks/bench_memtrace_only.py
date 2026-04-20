"""
Memtrace-only benchmark on the ArcadeDB backend.

Runs every dataset query through the real MCP binary (find_symbol tool)
and reports accuracy@1, latency percentiles, and token volume.  Mirrors
the measurement surface of benchmark_full.py without touching the
competitor runners — those are unchanged by the backend swap, so running
just the Memtrace portion is the fastest way to reproduce the v0.2.0
numbers after a clean index.

Prerequisites:
  1. ArcadeDB is up: `memtrace start` (auto-manages Docker)
  2. Target codebase is indexed: `memtrace index /path/to/mempalace`
  3. Dataset exists: `.venv/bin/python datasets/generate_dataset.py`
  4. Release binary built: `cargo build --release -p memtrace-mcp`

Usage:
  .venv/bin/python bench_memtrace_only.py
"""
import json
import os
import statistics
import subprocess
import sys
import time
import uuid

DATASET      = os.path.join(os.path.dirname(__file__), "datasets/real_code_dataset.json")
MEMTRACE_BIN = os.environ.get(
    "MEMTRACE_BIN",
    os.path.join(os.path.dirname(__file__), "..", "target", "release", "memtrace"),
)
OUTPUT       = os.path.join(os.path.dirname(__file__), "bench_memtrace_arcadedb.json")
MAX_QUERIES  = int(os.environ.get("MAX_QUERIES", "1000"))


class Memtrace:
    """Minimal MCP JSON-RPC client over stdio."""

    def __init__(self) -> None:
        self.p = subprocess.Popen(
            [MEMTRACE_BIN, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "bench", "version": "1.0"},
        })
        self._notify("notifications/initialized")

    def _call(self, method, params):
        rid = str(uuid.uuid4())
        self.p.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": rid, "method": method, "params": params,
        }) + "\n")
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

    def _notify(self, method, params=None):
        self.p.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params or {},
        }) + "\n")
        self.p.stdin.flush()

    def find(self, symbol):
        t0 = time.time()
        resp = self._call("tools/call", {
            "name": "find_symbol",
            "arguments": {"name": symbol, "limit": 10},
        })
        latency_ms = (time.time() - t0) * 1000
        text = ""
        if resp and "result" in resp:
            for c in resp["result"].get("content", []):
                if c.get("type") == "text":
                    text += c.get("text", "")
        return text, latency_ms

    def close(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.p.kill()


def main() -> int:
    if not os.path.exists(DATASET):
        print(f"dataset missing — run `python datasets/generate_dataset.py` first ({DATASET})", file=sys.stderr)
        return 1
    if not os.path.exists(MEMTRACE_BIN):
        print(f"memtrace binary missing — run `cargo build --release -p memtrace-mcp` ({MEMTRACE_BIN})", file=sys.stderr)
        return 1

    with open(DATASET) as f:
        cases = json.load(f)

    n = min(MAX_QUERIES, len(cases))
    print(f"Running {n} queries against Memtrace (ArcadeDB backend)...")

    mt = Memtrace()
    results = []
    try:
        for i, c in enumerate(cases[:n]):
            text, latency = mt.find(c["target_symbol"])
            hit = c["expected_file"] in text
            tokens = len(text) // 4
            results.append({"latency_ms": latency, "hit": hit, "tokens": tokens})
            if (i + 1) % 100 == 0:
                so_far = sum(1 for r in results if r["hit"]) / len(results) * 100
                print(f"  {i+1}/{n}   acc so far: {so_far:.1f}%")
    finally:
        mt.close()

    if not results:
        print("no results", file=sys.stderr)
        return 1

    acc = sum(1 for r in results if r["hit"]) / len(results) * 100
    latencies = sorted(r["latency_ms"] for r in results)
    tokens = [r["tokens"] for r in results]
    p95_idx = int(len(latencies) * 0.95)

    summary = {
        "backend": "ArcadeDB (Neo4j-Bolt plugin + HTTP opencypher)",
        "n_queries": len(results),
        "accuracy_pct": round(acc, 2),
        "avg_latency_ms": round(statistics.mean(latencies), 2),
        "median_latency_ms": round(statistics.median(latencies), 2),
        "p95_latency_ms": round(latencies[p95_idx], 2),
        "avg_tokens_per_query": round(statistics.mean(tokens), 0),
        "total_tokens": sum(tokens),
    }
    print("\n" + json.dumps(summary, indent=2))

    with open(OUTPUT, "w") as f:
        json.dump({"summary": summary, "raw": results}, f, indent=2)
    print(f"\nSaved raw results to {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
