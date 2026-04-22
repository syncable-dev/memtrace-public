"""Bench #3 ground-truth generator — pyright LSP call-hierarchy.

Spawns `pyright-langserver --stdio`, opens the mempalace workspace,
and for each of N sampled symbols queries:

  textDocument/prepareCallHierarchy
    → callHierarchy/incomingCalls (callers)
    → callHierarchy/outgoingCalls (callees)
    → transitive incomingCalls closure (impact)

Outputs `benchmarks/suite/datasets/bench_3_graph.json` with one entry per
usable symbol:

  [
    {
      "id": "g1",
      "symbol": "function_name",
      "file": "mempalace/foo/bar.py",
      "line": 42,
      "callers": [{"name": "...", "file": "...", "line": ...}, ...],
      "callees": [...],
      "impact":  [...]
    },
    ...
  ]

Prerequisites:
  - pyright installed globally: `npm install -g pyright`
  - mempalace checkout at /Users/alexthh/Desktop/ZeroToDemo/mempalace

If pyright fails to resolve a symbol, we skip it gracefully. Target is
200 usable triples; we over-sample the symbol pool to absorb skips.

Run:
    python -m benchmarks.suite.datasets.generators.pyright_graph

This takes several minutes — pyright has to index mempalace on startup.
"""
from __future__ import annotations
import ast
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Queue, Empty
from typing import Any


DEFAULT_SEED = 2026_04_21
TARGET_TRIPLES = 200
OVERSAMPLE_FACTOR = 3  # sample 3× target to absorb pyright skips
LSP_TIMEOUT_S = 20


@dataclass
class SymbolRef:
    name: str
    file: str  # workspace-relative, POSIX
    line: int  # 1-indexed


# ── LSP plumbing ─────────────────────────────────────────────────────────────

class LSPClient:
    """Minimal JSON-RPC over stdio LSP client. Only the handful of
    methods we need: initialize, textDocument/didOpen,
    textDocument/prepareCallHierarchy, callHierarchy/incomingCalls,
    callHierarchy/outgoingCalls, shutdown."""

    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root.resolve()
        self.proc: subprocess.Popen | None = None
        self._id = 0
        self._responses: dict[int, Any] = {}
        self._lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        exe = shutil.which("pyright-langserver")
        if not exe:
            raise RuntimeError(
                "pyright-langserver not on PATH. Install with: npm install -g pyright"
            )
        self.proc = subprocess.Popen(
            [exe, "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        # initialize
        root_uri = f"file://{self.workspace_root}"
        self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "callHierarchy": {"dynamicRegistration": False},
                },
                "workspace": {"workspaceFolders": True},
            },
            "workspaceFolders": [
                {"uri": root_uri, "name": self.workspace_root.name},
            ],
        }, timeout=60)  # pyright takes a while to index
        self._notify("initialized", {})

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self._request("shutdown", None, timeout=5)
            self._notify("exit", None)
        except Exception:
            pass
        self._stop.set()
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
            self.proc.wait()
        self.proc = None

    def open_document(self, path: Path) -> None:
        uri = f"file://{path.resolve()}"
        text = path.read_text(encoding="utf-8", errors="ignore")
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri, "languageId": "python", "version": 1, "text": text,
            },
        })

    def prepare_call_hierarchy(self, path: Path, line: int, character: int):
        """line/character are 0-indexed per LSP spec."""
        uri = f"file://{path.resolve()}"
        return self._request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        })

    def incoming_calls(self, item: dict):
        return self._request("callHierarchy/incomingCalls", {"item": item})

    def outgoing_calls(self, item: dict):
        return self._request("callHierarchy/outgoingCalls", {"item": item})

    # — internals —

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _encode(self, payload: dict) -> bytes:
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        return header + body

    def _notify(self, method: str, params: Any) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(self._encode(msg))
        self.proc.stdin.flush()

    def _request(self, method: str, params: Any, timeout: float = LSP_TIMEOUT_S):
        rid = self._next_id()
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(self._encode(msg))
        self.proc.stdin.flush()
        # Poll response dict.
        t0 = time.time()
        while True:
            with self._lock:
                if rid in self._responses:
                    return self._responses.pop(rid)
            if time.time() - t0 > timeout:
                raise TimeoutError(f"LSP method '{method}' timed out after {timeout}s")
            time.sleep(0.01)

    def _reader_loop(self) -> None:
        assert self.proc and self.proc.stdout
        stdout = self.proc.stdout
        while not self._stop.is_set():
            # Read LSP frame header
            header = b""
            while b"\r\n\r\n" not in header:
                chunk = stdout.read(1)
                if not chunk:
                    return
                header += chunk
            # Parse Content-Length
            length = 0
            for line in header.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip())
                    break
            # Read body
            body = b""
            while len(body) < length:
                chunk = stdout.read(length - len(body))
                if not chunk:
                    return
                body += chunk
            try:
                msg = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self._lock:
                    self._responses[msg["id"]] = msg


# ── Symbol enumeration ───────────────────────────────────────────────────────

def enumerate_symbols(workspace: Path) -> list[SymbolRef]:
    """Walk the workspace, parse .py files, yield top-level + method
    def lines. Skips tests/__init__ for diversity."""
    syms: list[SymbolRef] = []
    skip_parts = {".git", ".venv", "venv", "node_modules", "__pycache__",
                  "build", "dist", ".mypy_cache", ".pytest_cache"}
    for py in workspace.rglob("*.py"):
        if any(part in skip_parts for part in py.parts):
            continue
        if py.name == "__init__.py":
            continue
        rel = py.relative_to(workspace).as_posix()
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                syms.append(SymbolRef(name=node.name, file=rel, line=node.lineno))
    return syms


# ── Response marshalling ─────────────────────────────────────────────────────

def _uri_to_rel(uri: str, workspace_root: Path, corpus_prefix: str) -> str:
    """file:///abs/path → corpus/<rel>.  Returns "" if outside workspace."""
    if not uri.startswith("file://"):
        return ""
    abs_p = Path(uri[len("file://"):])
    try:
        rel = abs_p.resolve().relative_to(workspace_root)
    except ValueError:
        return ""
    return f"{corpus_prefix}/{rel.as_posix()}"


def _item_to_ref(item: dict, workspace_root: Path, corpus_prefix: str) -> dict:
    return {
        "name": item.get("name", ""),
        "file": _uri_to_rel(item.get("uri", ""), workspace_root, corpus_prefix),
        "line": (item.get("range") or {}).get("start", {}).get("line", -1) + 1,
    }


# ── Orchestration ────────────────────────────────────────────────────────────

def generate(
    workspace: Path,
    corpus_prefix: str = "mempalace",
    target_triples: int = TARGET_TRIPLES,
    seed: int = DEFAULT_SEED,
) -> list[dict]:
    workspace = workspace.resolve()
    if not workspace.exists():
        raise FileNotFoundError(f"workspace {workspace} does not exist")

    rng = random.Random(seed)
    all_syms = enumerate_symbols(workspace)
    rng.shuffle(all_syms)

    if len(all_syms) < target_triples:
        print(f"warning: only {len(all_syms)} symbols available "
              f"(target {target_triples}); dataset will be smaller",
              file=sys.stderr)

    client = LSPClient(workspace)
    client.start()
    results: list[dict] = []
    try:
        # Open files lazily — pyright needs didOpen for prepareCallHierarchy
        # to resolve reliably.
        opened: set[Path] = set()
        pool = all_syms[: target_triples * OVERSAMPLE_FACTOR]
        for idx, sym in enumerate(pool):
            if len(results) >= target_triples:
                break
            full = workspace / sym.file
            if not full.exists():
                continue
            if full not in opened:
                try:
                    client.open_document(full)
                    opened.add(full)
                    time.sleep(0.02)  # give pyright a beat
                except Exception:
                    continue
            try:
                # LSP positions are 0-indexed; AST lineno is 1-indexed.
                items = client.prepare_call_hierarchy(
                    full, sym.line - 1, 4,  # character 4 ≈ past "def "
                )
            except Exception:
                continue
            result = (items or {}).get("result") if isinstance(items, dict) else None
            if not result:
                continue
            item = result[0]
            try:
                incoming = client.incoming_calls(item)
                outgoing = client.outgoing_calls(item)
            except Exception:
                continue
            inc_res = (incoming or {}).get("result") or []
            out_res = (outgoing or {}).get("result") or []
            callers = [_item_to_ref(c.get("from", {}), workspace, corpus_prefix)
                       for c in inc_res]
            callees = [_item_to_ref(c.get("to", {}), workspace, corpus_prefix)
                       for c in out_res]
            # Impact = transitive incoming closure (depth 2). We fan out one
            # additional layer from each caller.
            impact_names: set[tuple[str, str, int]] = set()
            for c in inc_res:
                frm = c.get("from", {})
                impact_names.add((frm.get("name", ""), frm.get("uri", ""),
                                  (frm.get("range") or {}).get("start", {}).get("line", -1)))
                try:
                    sub = client.incoming_calls(frm)
                    sub_res = (sub or {}).get("result") or []
                    for s in sub_res:
                        sfrm = s.get("from", {})
                        impact_names.add((sfrm.get("name", ""), sfrm.get("uri", ""),
                                          (sfrm.get("range") or {}).get("start", {}).get("line", -1)))
                except Exception:
                    pass
            impact = [
                {"name": n, "file": _uri_to_rel(u, workspace, corpus_prefix),
                 "line": line + 1}
                for (n, u, line) in impact_names
                if n
            ]
            results.append({
                "id": f"g{len(results) + 1}",
                "symbol": sym.name,
                "file": f"{corpus_prefix}/{sym.file}",
                "line": sym.line,
                "callers": callers,
                "callees": callees,
                "impact":  impact,
            })
            if (idx + 1) % 25 == 0:
                print(f"  processed {idx + 1}/{len(pool)} probes; "
                      f"{len(results)}/{target_triples} usable",
                      file=sys.stderr)
    finally:
        client.stop()
    return results


def main() -> None:
    from benchmarks.suite.corpora.mempalace import MempalaceCorpus
    corpus = MempalaceCorpus()
    if not corpus.path.exists():
        raise SystemExit(f"mempalace not found at {corpus.path}")
    print(f"generating pyright ground truth for {corpus.path}", file=sys.stderr)
    t0 = time.time()
    entries = generate(corpus.path, corpus_prefix=corpus.name)
    elapsed = time.time() - t0
    out = Path(__file__).resolve().parents[1] / "bench_3_graph.json"
    out.write_text(json.dumps(entries, indent=2) + "\n")
    print(f"wrote {len(entries)} triples -> {out}  ({elapsed:.1f}s)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
