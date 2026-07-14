#!/usr/bin/env python3
"""Run ContextBench's coding agent with live, hierarchical Memtrace tools."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import importlib.util
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


HERE = Path(__file__).resolve().parent
GENERATED_PATCH_PREFIXES = (
    "build/",
    "dist/",
    ".eggs/",
    ".pytest_cache/",
    ".tox/",
    "node_modules/",
    "appendonlydir/",
    ".nyc_output/",
    "coverage/",
)
GENERATED_PATCH_BASENAMES = {"appendonly.aof", "dump.rdb", ".coverage", "Dockerfile"}
AGENT_LOCALIZATION_POLICY = "hierarchy-listwise-v2"
AGENT_TOOL_CLIENT_PATH = "/usr/local/bin/memtrace-agent"
MAX_AGENT_TOOL_RESULTS = 50
MIN_RANKED_CONTEXTS = 2
MAX_RANKED_CONTEXTS = 12
PLACEHOLDER_QUERIES = {"issue", "issue concept", "specific behavior", "behavior"}
SCOPED_RECALL_FLOOR_SCORE = 0.25
MAX_RECALL_FLOOR_LINES = 80


@dataclass(frozen=True)
class BenchmarkSettings:
    subset: str
    split: str
    container_root: str
    use_multi_config: bool = False


def snapshot_age_days(repo_root: Path) -> int:
    result = subprocess.run(
        ["git", "show", "-s", "--format=%ct", "HEAD"],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"could not read snapshot commit time: {result.stderr.strip()}"
        )
    commit_time = int(result.stdout.strip())
    return max(0, int((time.time() - commit_time) // 86400))


def snapshot_anchored_days(repo_root: Path, lookback_days: int) -> int | None:
    """Translate a historical benchmark lookback to Memtrace's wall-clock window."""
    if lookback_days < 0:
        return None
    return snapshot_age_days(repo_root) + lookback_days


def compact_tool_value(value: Any, depth: int = 0) -> Any:
    """Keep tool evidence useful to the agent without dumping whole source files."""
    if depth >= 5:
        return "<nested value omitted>"
    if isinstance(value, str):
        return value if len(value) <= 600 else value[:600] + "..."
    if isinstance(value, list):
        return [
            compact_tool_value(item, depth + 1)
            for item in value[:MAX_AGENT_TOOL_RESULTS]
        ]
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, item in list(value.items())[:80]:
            if key in {"content", "source", "source_code", "text"} and isinstance(
                item, str
            ):
                continue
            compacted[str(key)] = compact_tool_value(item, depth + 1)
        return compacted
    return value


def compact_tool_result(action: str, result: Any) -> Any:
    if action == "search" and isinstance(result, dict):
        rows = []
        for row in result.get("results", [])[:MAX_AGENT_TOOL_RESULTS]:
            if not isinstance(row, dict):
                continue
            rows.append(
                {
                    key: row[key]
                    for key in (
                        "id",
                        "file_path",
                        "name",
                        "scope_path",
                        "kind",
                        "start_line",
                        "end_line",
                        "symbol_start_line",
                        "symbol_end_line",
                        "score",
                    )
                    if row.get(key) is not None
                }
            )
        return {
            "query": result.get("query"),
            "total": result.get("total", len(result.get("results", []))),
            "results": rows,
        }
    return compact_tool_value(result)


class AgentToolBridge:
    """Expose a task-isolated Memtrace MCP session to the benchmark container."""

    def __init__(
        self,
        repo_root: Path,
        env: dict[str, str],
        *,
        container_root: str = "/testbed",
        require_history: bool = True,
        trace_path: Path | None = None,
        max_ranked_lines: int = 200,
    ) -> None:
        self.repo_root = repo_root
        self.env = env
        self.container_root = container_root.rstrip("/")
        self.require_history = require_history
        self.trace_path = trace_path
        self.max_ranked_lines = max_ranked_lines
        self.snapshot_age_days = snapshot_age_days(repo_root)
        self.token = secrets.token_urlsafe(32)
        self.client = retrieval.McpClient(repo_root, env)
        repositories = self.client.call_tool("list_indexed_repositories", {})
        if not isinstance(repositories, list) or len(repositories) != 1:
            self.client.close()
            raise RuntimeError(
                f"agent bridge expected one isolated indexed repo, got: {repositories}"
            )
        self.repo_id = str(repositories[0]["repo_id"])
        self.trace: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://host.docker.internal:{self._server.server_port}/tool"

    def _handler(self):
        bridge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                if self.path != "/tool":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 1024 * 1024:
                        raise ValueError("invalid request length")
                    payload = json.loads(self.rfile.read(length))
                    if not secrets.compare_digest(
                        str(payload.pop("token", "")), bridge.token
                    ):
                        raise PermissionError("invalid bridge token")
                    response = {"ok": True, **bridge.dispatch(payload)}
                    status = 200
                except PermissionError as error:
                    response = {"ok": False, "error": str(error)}
                    status = 403
                except Exception as error:
                    response = {
                        "ok": False,
                        "error": f"{type(error).__name__}: {error}",
                    }
                    status = 400
                body = json.dumps(response, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    @staticmethod
    def _required_text(payload: dict[str, Any], key: str) -> str:
        value = str(payload.get(key) or "").strip()
        if not value:
            raise ValueError(f"{key} is required")
        return value

    @staticmethod
    def parse_ranked_candidate(value: str) -> tuple[str, int, int]:
        match = re.fullmatch(r"(.+):(\d+)-(\d+)", value.strip())
        if not match:
            raise ValueError(
                f"invalid candidate {value!r}; expected relative/path:start-end"
            )
        file_path, start_text, end_text = match.groups()
        start, end = int(start_text), int(end_text)
        if start < 1 or end < start:
            raise ValueError(f"invalid candidate line range: {value!r}")
        return file_path, start, end

    def _ground_ranked_candidates(
        self, raw_candidates: list[Any]
    ) -> list[dict[str, Any]]:
        if not MIN_RANKED_CONTEXTS <= len(raw_candidates) <= MAX_RANKED_CONTEXTS:
            raise ValueError(
                f"rank requires {MIN_RANKED_CONTEXTS}-{MAX_RANKED_CONTEXTS} candidates"
            )
        grounded_ranges: dict[str, list[tuple[int, int]]] = {}
        for record in self.trace:
            if record.get("action") not in {"search", "symbol"}:
                continue
            result = record.get("result")
            rows = result.get("results", []) if isinstance(result, dict) else []
            if isinstance(result, dict) and not rows:
                rows = [result]
            for row in rows:
                if not isinstance(row, dict) or not row.get("file_path"):
                    continue
                start = row.get("start_line", row.get("symbol_start_line"))
                end = row.get("end_line", row.get("symbol_end_line"))
                if start is None or end is None:
                    continue
                file_path = self.normalize_agent_path(str(row["file_path"]))
                grounded_ranges.setdefault(file_path, []).append((int(start), int(end)))

        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, int, int]] = set()
        total_lines = 0
        for raw_candidate in raw_candidates:
            raw_path, start, end = self.parse_ranked_candidate(str(raw_candidate))
            file_path = self.normalize_agent_path(raw_path)
            if not (self.repo_root / file_path).is_file():
                raise ValueError(f"ranked candidate file does not exist: {file_path}")
            key = (file_path, start, end)
            if key in seen:
                raise ValueError(f"duplicate ranked candidate: {raw_candidate}")
            seen.add(key)
            if not any(
                outer_start <= start <= end <= outer_end
                for outer_start, outer_end in grounded_ranges.get(file_path, [])
            ):
                raise ValueError(
                    f"ranked candidate was not grounded by Memtrace evidence: {raw_candidate}"
                )
            total_lines += end - start + 1
            if total_lines > self.max_ranked_lines:
                raise ValueError(
                    f"ranked context exceeds {self.max_ranked_lines}-line budget"
                )
            candidates.append({"file": file_path, "start": start, "end": end})
        return candidates

    def normalize_agent_path(self, raw_path: str) -> str:
        value = raw_path.replace("\\", "/").strip()
        if not value:
            return value
        direct = value.lstrip("./")
        if (self.repo_root / direct).is_file():
            return direct
        prefixes = [self.container_root, "/testbed", "/app"]
        for prefix in prefixes:
            marker = prefix.rstrip("/") + "/"
            if value.startswith(marker):
                relative = value[len(marker) :]
                if (self.repo_root / relative).is_file():
                    return relative
        parts = Path(value).parts
        for index in range(1, len(parts)):
            relative = Path(*parts[index:]).as_posix()
            if (self.repo_root / relative).is_file():
                return relative
        return value

    def _rewrite_result_paths(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._rewrite_result_paths(item) for item in value]
        if isinstance(value, dict):
            return {
                key: (
                    self.normalize_agent_path(str(item))
                    if key == "file_path" and isinstance(item, str)
                    else self._rewrite_result_paths(item)
                )
                for key, item in value.items()
            }
        return value

    def _write_trace(self) -> None:
        if self.trace_path is None:
            return
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.trace_path.with_name(
            f"{self.trace_path.name}.tmp-{os.getpid()}"
        )
        temporary.write_text(json.dumps(self.trace, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.trace_path)

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = self._required_text(payload, "action")
        arguments: dict[str, Any]
        if action == "search":
            query = self._required_text(payload, "query")
            if not str(payload.get("file_path") or "").strip():
                normalized_query = re.sub(r"\s+", " ", query.lower()).strip()
                significant_terms = re.findall(r"[a-z0-9_]+", normalized_query)
                if (
                    normalized_query in PLACEHOLDER_QUERIES
                    or len(significant_terms) < 4
                ):
                    raise ValueError(
                        "broad search query must describe the concrete issue; "
                        "placeholder or generic queries are rejected"
                    )
            arguments = {
                "repo_id": self.repo_id,
                "query": query,
                "limit": min(
                    MAX_AGENT_TOOL_RESULTS, max(1, int(payload.get("limit", 20)))
                ),
                "include_diagnostics": True,
                "view": "live",
            }
            if file_path := str(payload.get("file_path") or "").strip():
                arguments["file_path"] = self.normalize_agent_path(file_path)
            tool_name = "find_code"
        elif action == "shortlist":
            files = [
                self.normalize_agent_path(str(item).strip())
                for item in payload.get("files", [])
                if str(item).strip()
            ]
            if not 2 <= len(files) <= 8:
                raise ValueError("shortlist requires 2-8 file paths")
            arguments = {"files": list(dict.fromkeys(files))}
            tool_name = "shortlist"
        elif action == "symbol":
            arguments = {
                "repo_id": self.repo_id,
                "symbol": self._required_text(payload, "symbol"),
                "view": "live",
            }
            if file_path := str(payload.get("file_path") or "").strip():
                arguments["file_path"] = self.normalize_agent_path(file_path)
            tool_name = "get_symbol_context"
        elif action == "cochange":
            target = self._required_text(payload, "target")
            arguments = {
                "repo_id": self.repo_id,
                "target": self.normalize_agent_path(target),
                "limit": min(20, max(1, int(payload.get("limit", 10)))),
                "window_days": self.snapshot_age_days
                + max(1, int(payload.get("window_days", 365))),
            }
            tool_name = "get_cochange_context"
        elif action == "history":
            from_time = str(payload.get("from_time") or "365d ago")
            relative_days = re.fullmatch(r"\s*(\d+)d\s+ago\s*", from_time)
            if relative_days:
                from_time = (
                    f"{self.snapshot_age_days + int(relative_days.group(1))}d ago"
                )
            arguments = {
                "repo_id": self.repo_id,
                "from": from_time,
                "mode": "recent",
                "limit": min(50, max(1, int(payload.get("limit", 20)))),
            }
            if target := str(payload.get("target") or "").strip():
                arguments["target"] = self.normalize_agent_path(target)
            tool_name = "get_evolution"
        elif action == "rank":
            arguments = {
                "candidates": self._ground_ranked_candidates(
                    list(payload.get("candidates") or [])
                )
            }
            tool_name = "rank_context"
        elif action == "verify":
            arguments = {}
            tool_name = "verify_protocol"
        else:
            raise ValueError(f"unsupported action: {action}")

        started = time.monotonic()
        with self._lock:
            result = (
                {"accepted": arguments["files"]}
                if tool_name == "shortlist"
                else {"accepted": arguments["candidates"]}
                if tool_name == "rank_context"
                else validate_agent_tool_trace(
                    self.trace, require_history=self.require_history
                )
                if tool_name == "verify_protocol"
                else self.client.call_tool(tool_name, arguments)
            )
        compacted = self._rewrite_result_paths(compact_tool_result(action, result))
        canonical = json.dumps(compacted, sort_keys=True, separators=(",", ":"))
        record = {
            "sequence": len(self.trace) + 1,
            "action": action,
            "tool": tool_name,
            "arguments": arguments,
            "result_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "result": compacted,
            "seconds": round(time.monotonic() - started, 3),
            "ok": True,
        }
        self.trace.append(record)
        self._write_trace()
        return {"action": action, "repo_id": self.repo_id, "result": compacted}

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self.client.close()

    def __enter__(self) -> "AgentToolBridge":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def benchmark_settings(instance_id: str, repo: str) -> BenchmarkSettings:
    family = instance_id.split("__", 1)[0]
    if family == "Multi-SWE-Bench":
        repo_name = repo.rsplit("/", 1)[-1].replace("-", "_")
        return BenchmarkSettings(
            "multi-swe-bench", "train", f"/home/{repo_name}", use_multi_config=True
        )
    if family == "SWE-PolyBench":
        return BenchmarkSettings("AmazonScience/SWE-PolyBench", "test", "/testbed")
    if family == "SWE-Bench-Pro":
        return BenchmarkSettings("pro", "test", "/app")
    return BenchmarkSettings("verified", "test", "/testbed")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


retrieval = load_module("contextbench_memtrace_retrieval", HERE / "runner.py")


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def read_lines(repo_root: Path, file_path: str, start: int, end: int) -> str:
    path = repo_root / file_path
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[max(0, start - 1) : min(len(lines), end)])


def render_seed_context(
    repo_root: Path, candidates: list[dict[str, Any]], container_root: str = "/testbed"
) -> str:
    sections = []
    for candidate in candidates:
        file_path = str(candidate["file"])
        start = int(candidate["start"])
        end = int(candidate["end"])
        content = read_lines(repo_root, file_path, start, end)
        if not content:
            continue
        sections.append(
            f"File: {container_root.rstrip('/')}/{file_path}\n"
            f"Lines: {start}-{end}\n```\n{content}\n```"
        )
    return "\n\n".join(sections)


def write_docker_wrapper(output: Path, client_path: Path) -> None:
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("docker is required for the ContextBench agent lane")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'if [ "${1:-}" = run ]; then\n'
        "  shift\n"
        f"  exec {shlex.quote(docker)} run "
        "--add-host host.docker.internal:host-gateway "
        f'-v {shlex.quote(str(client_path.resolve()) + ":" + AGENT_TOOL_CLIENT_PATH + ":ro")} "$@"\n'
        "fi\n"
        f'exec {shlex.quote(docker)} "$@"\n',
        encoding="utf-8",
    )
    output.chmod(0o755)


def prepare_history(
    repo_root: Path, env: dict[str, str], history_days: int
) -> dict[str, Any]:
    if history_days <= 0:
        return {"enabled": False, "days": 0}
    client = retrieval.McpClient(repo_root, env)
    started = time.monotonic()
    try:
        repositories = client.call_tool("list_indexed_repositories", {})
        if not isinstance(repositories, list) or len(repositories) != 1:
            raise RuntimeError(
                f"history replay expected one isolated indexed repo, got: {repositories}"
            )
        repo_id = str(repositories[0]["repo_id"])
        effective_days = snapshot_anchored_days(repo_root, history_days)
        replay_arguments: dict[str, Any] = {"repo_id": repo_id}
        if effective_days is not None:
            replay_arguments["days"] = effective_days
        result = client.call_tool("replay_history", replay_arguments)
        job_id = result.get("job_id") if isinstance(result, dict) else None
        if job_id:
            deadline = time.monotonic() + 1800
            while time.monotonic() < deadline:
                status = client.call_tool("check_job_status", {"job_id": job_id})
                state = (
                    str(status.get("status") or "") if isinstance(status, dict) else ""
                )
                if state in {"completed", "failed"}:
                    result = status
                    break
                time.sleep(2)
            else:
                raise RuntimeError("history replay exceeded 1800 seconds")
        return {
            "enabled": True,
            "lookback_days": history_days,
            "effective_days": effective_days,
            "seconds": round(time.monotonic() - started, 3),
            "result": compact_tool_value(result),
        }
    except RuntimeError as error:
        return {
            "enabled": True,
            "lookback_days": history_days,
            "seconds": round(time.monotonic() - started, 3),
            "unavailable": str(error),
        }
    finally:
        client.close()


def validate_agent_tool_trace(
    trace: list[dict[str, Any]], *, require_history: bool = True
) -> dict[str, Any]:
    """Require the declared hierarchy instead of accepting a seed-only agent run."""
    broad_search = next(
        (
            index
            for index, item in enumerate(trace)
            if item.get("action") == "search"
            and not item.get("arguments", {}).get("file_path")
        ),
        None,
    )
    shortlist = next(
        (
            index
            for index, item in enumerate(trace)
            if item.get("action") == "shortlist"
            and broad_search is not None
            and index > broad_search
        ),
        None,
    )
    shortlist_files = (
        set(trace[shortlist].get("arguments", {}).get("files", []))
        if shortlist is not None
        else set()
    )
    scoped_searches = {
        str(item.get("arguments", {}).get("file_path")): index
        for index, item in enumerate(trace)
        if item.get("action") == "search"
        and item.get("arguments", {}).get("file_path")
        and shortlist is not None
        and index > shortlist
    }
    scoped_complete = bool(shortlist_files) and shortlist_files.issubset(
        scoped_searches
    )
    file_search = max(scoped_searches.values()) if scoped_complete else None
    symbol_calls = [
        (index, str(item.get("arguments", {}).get("file_path") or ""))
        for index, item in enumerate(trace)
        if item.get("action") == "symbol"
        and file_search is not None
        and index > file_search
    ]
    symbol_files = {file_path for _, file_path in symbol_calls if file_path}
    symbol = (
        max(index for index, _ in symbol_calls)
        if len(symbol_files.intersection(shortlist_files)) >= 2
        else None
    )
    history = next(
        (
            index
            for index, item in enumerate(trace)
            if item.get("action") in {"cochange", "history"}
            and symbol is not None
            and index > symbol
        ),
        None,
    )
    rank = next(
        (
            index
            for index, item in enumerate(trace)
            if item.get("action") == "rank"
            and symbol is not None
            and index > (history if history is not None else symbol)
            and (history is not None or not require_history)
            and MIN_RANKED_CONTEXTS
            <= len(item.get("arguments", {}).get("candidates", []))
            <= MAX_RANKED_CONTEXTS
        ),
        None,
    )
    missing = []
    for label, value in (
        ("broad search", broad_search),
        ("listwise shortlist", shortlist),
        ("file-scoped search for every shortlisted file", file_search),
        ("symbol graph for two shortlisted files", symbol),
    ):
        if value is None:
            missing.append(label)
    if require_history and history is None:
        missing.append("co-change/history")
    if rank is None:
        missing.append("ranked file-symbol-line context")
    if missing:
        raise RuntimeError(
            "agent did not complete the Memtrace localization protocol: "
            + ", ".join(missing)
        )
    return {
        "policy": AGENT_LOCALIZATION_POLICY,
        "calls": len(trace),
        "broad_search_sequence": broad_search + 1,
        "shortlist_sequence": shortlist + 1,
        "file_search_sequence": file_search + 1,
        "symbol_sequence": symbol + 1,
        "history_sequence": history + 1 if history is not None else None,
        "rank_sequence": rank + 1,
        "ranked_candidates": trace[rank]["arguments"]["candidates"],
    }


def write_agent_config(
    base_config: Path,
    output: Path,
    seed_context: str,
    model: str,
    container_root: str = "/testbed",
    bridge_url: str | None = None,
    bridge_token: str | None = None,
    docker_executable: Path | None = None,
    line_budget: int = 200,
) -> None:
    config = yaml.safe_load(base_config.read_text(encoding="utf-8"))
    seed_block = (
        "\n<memtrace_seed_context>\n"
        "Memtrace retrieved the following likely change surface before this run. "
        f"Use it as starting evidence, verify it against {container_root}, and continue exploring if needed. "
        "It is not a proposed patch and may be incomplete.\n\n"
        f"{seed_context}\n"
        "</memtrace_seed_context>\n"
    )
    tool_block = ""
    if bridge_url and bridge_token and docker_executable:
        tool_block = f"""
<memtrace_agent_protocol policy="{AGENT_LOCALIZATION_POLICY}">
Memtrace remains available throughout this task through the deterministic
`memtrace-agent` command. The seed above is only a starting hint.

Before editing, complete this hierarchy in order:
1. Broad file discovery with a concrete issue query containing the actual symptom,
   component, and expected behavior. Never copy a placeholder such as `issue concept`:
   `memtrace-agent search --query 'actual symptom component expected behavior' --limit 30`
2. Compare the complete result slate listwise. Submit 2-8 production files with
   `memtrace-agent shortlist --file path/one --file path/two`
3. Localize symbols inside EVERY shortlisted file with an issue-specific query:
   `memtrace-agent search --query 'specific behavior' --file path/one --limit 20`
4. Traverse the top competing symbols in AT LEAST TWO different shortlisted files:
   `memtrace-agent symbol --name SymbolName --file path/one`
5. Check commit co-change memory after finding an anchor with
   `memtrace-agent cochange --target path/one --days 365`
   Use `memtrace-agent history --target SymbolName --from '365d ago'` when the
   evolution of the behavior is relevant.
6. Read exact returned line ranges from {container_root} using numbered source
   views. Reproduce the failure or run the narrowest relevant test before editing.
7. Rank 2-12 exact file -> symbol -> line contexts, best first, within the
   {line_budget}-line budget. Every range must be inside prior Memtrace evidence:
   `memtrace-agent rank --candidate path/one.py:10-30 --candidate path/two.py:40-70`
   Re-run rank if later evidence changes the causal context, then test the patch.

Use the relative file paths returned by Memtrace. The only valid final submission
command begins with `memtrace-agent verify`; if any hierarchy stage is missing,
verification will fail and tell you what to complete.

Memtrace search, reranking, graph traversal, and history are deterministic.
You are responsible for comparing candidates, refining the queries, making the
patch, and ranking the smallest exact production line ranges that governed it.
</memtrace_agent_protocol>
"""
        environment = config.setdefault("environment", {})
        environment["executable"] = str(docker_executable)
        environment_env = environment.setdefault("env", {})
        environment_env["MEMTRACE_AGENT_URL"] = bridge_url
        environment_env["MEMTRACE_AGENT_TOKEN"] = bridge_token
    template = str(config["agent"]["instance_template"])
    marker = "</pr_description>"
    if marker in template:
        template = template.replace(marker, marker + seed_block + tool_block, 1)
    else:
        template = seed_block + tool_block + template
    config["agent"]["instance_template"] = template
    config["agent"]["context_request_template"] += (
        "\nFor the final PATCH_CONTEXT, preserve the exact ranked production definitions "
        "that governed the patch. Include every production function or class you changed, "
        "called through, or depended on; do not collapse the causal path to only the edited "
        "function. Do not copy broad exploration windows, whole files, tests, or unrelated neighbors.\n"
    )
    submission = (
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && git add -A && git diff --cached"
    )
    # ContextAwareAgent recognizes submission only when the marker is the first
    # output line. Silence a successful receipt so the marker keeps that shape;
    # on failure the non-zero exit still prevents the marker and staged diff.
    guarded_submission = f"memtrace-agent verify >/dev/null && {submission}"
    config["agent"]["instance_template"] = config["agent"]["instance_template"].replace(
        submission, guarded_submission
    )
    config["agent"]["context_request_template"] = config["agent"][
        "context_request_template"
    ].replace(submission, guarded_submission)
    for template_name in ("instance_template", "context_request_template"):
        if guarded_submission not in config["agent"][template_name]:
            config["agent"][template_name] += (
                "\nThe only valid submission command is:\n"
                f"```bash\n{guarded_submission}\n```\n"
            )
    config.setdefault("model", {})["model_name"] = model
    config["model"]["model_kwargs"] = {"drop_params": True, "temperature": 0.0}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def run_miniswe(
    python: Path,
    contextbench_root: Path,
    config: Path,
    original_instance_id: str,
    output_dir: Path,
    model: str,
    timeout: int,
    settings: BenchmarkSettings,
) -> subprocess.CompletedProcess[str]:
    mini_src = (
        contextbench_root
        / "agent-frameworks/mini-swe-agent/multi-poly-pro-verified/mini-swe-agent/src"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{mini_src}:{env.get('PYTHONPATH', '')}"
    command = [
        str(python),
        "-m",
        "minisweagent.run.extra.swebench_context_aware",
        "--subset",
        settings.subset,
        "--split",
        settings.split,
        "--config",
        str(config),
        "--filter",
        f"^{re.escape(original_instance_id)}$",
        "--output",
        str(output_dir),
        "--workers",
        "1",
        "--model",
        model,
    ]
    return subprocess.run(
        command,
        cwd=mini_src,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def load_agent_result(
    contextbench_root: Path, output_dir: Path, original_instance_id: str
) -> tuple[dict[str, Any], str, dict[str, Any], Path]:
    trajectory_path = (
        output_dir / original_instance_id / f"{original_instance_id}.traj.json"
    )
    if not trajectory_path.is_file():
        raise RuntimeError(f"mini-SWE-agent trajectory missing: {trajectory_path}")
    extractor = load_module(
        "contextbench_miniswe_extractor",
        contextbench_root / "contextbench/agents/minisweagent/extract.py",
    )
    trajectory = extractor.extract_trajectory(str(trajectory_path))
    raw_trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    predictions = json.loads((output_dir / "preds.json").read_text(encoding="utf-8"))
    patch = str(predictions.get(original_instance_id, {}).get("model_patch") or "")
    return trajectory, patch, raw_trajectory, trajectory_path


def merge_prediction(
    instance_id: str,
    retrieval_prediction: dict[str, Any],
    agent_trajectory: dict[str, Any],
    patch: str,
    agent_tool_trace: list[dict[str, Any]] | None = None,
    line_budget: int = 200,
) -> dict[str, Any]:
    retrieval_steps = retrieval_prediction.get("traj_data", {}).get("pred_steps", [])
    agent_steps = agent_trajectory.get("pred_steps", [])
    projection = project_agent_context(agent_tool_trace or [], line_budget)
    ranked_context = projection["candidates"]
    if ranked_context:
        pred_files = list(dict.fromkeys(str(item["file"]) for item in ranked_context))
        pred_spans: dict[str, list[dict[str, Any]]] = {}
        for item in ranked_context:
            pred_spans.setdefault(str(item["file"]), []).append(
                {"type": "line", "start": int(item["start"]), "end": int(item["end"])}
            )
        pred_symbols: dict[str, Any] = {}
    else:
        pred_files = agent_trajectory.get("pred_files", [])
        pred_spans = agent_trajectory.get("pred_spans", {})
        pred_symbols = agent_trajectory.get("pred_symbols", {})
    return {
        "instance_id": instance_id,
        "traj_data": {
            "pred_steps": [*retrieval_steps, *agent_steps],
            "pred_files": pred_files,
            "pred_spans": pred_spans,
            "pred_symbols": pred_symbols,
        },
        "model_patch": patch,
    }


def project_agent_context(
    trace: list[dict[str, Any]], line_budget: int
) -> dict[str, Any]:
    """Keep the agent's slate plus high-confidence exact symbols it shortlisted."""
    ranked: list[dict[str, Any]] = []
    shortlist_files: list[str] = []
    shortlist_sequence = -1
    for index, record in enumerate(trace):
        if record.get("action") == "shortlist":
            shortlist_files = list(record.get("arguments", {}).get("files", []))
            shortlist_sequence = index
        elif record.get("action") == "rank":
            ranked = [
                dict(item) for item in record.get("arguments", {}).get("candidates", [])
            ]

    intervals: dict[str, list[tuple[int, int]]] = {}
    file_order: list[str] = []

    def merged_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
        merged: list[list[int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [(start, end) for start, end in merged]

    def unique_line_count() -> int:
        return sum(
            end - start + 1
            for spans in intervals.values()
            for start, end in merged_spans(spans)
        )

    def add_candidate(candidate: dict[str, Any]) -> bool:
        file_path = str(candidate["file"])
        start, end = int(candidate["start"]), int(candidate["end"])
        if file_path not in intervals:
            intervals[file_path] = []
            file_order.append(file_path)
        intervals[file_path].append((start, end))
        if unique_line_count() > line_budget:
            intervals[file_path].pop()
            if not intervals[file_path]:
                del intervals[file_path]
                file_order.remove(file_path)
            return False
        return True

    for candidate in ranked:
        add_candidate(candidate)

    floor_added: list[dict[str, Any]] = []
    for file_path in shortlist_files:
        best: dict[str, Any] | None = None
        for index, record in enumerate(trace):
            if index <= shortlist_sequence or record.get("action") != "search":
                continue
            if record.get("arguments", {}).get("file_path") != file_path:
                continue
            result = record.get("result", {})
            for row in result.get("results", []) if isinstance(result, dict) else []:
                if not isinstance(row, dict) or row.get("kind") == "File":
                    continue
                start, end = row.get("symbol_start_line"), row.get("symbol_end_line")
                score = float(row.get("score", 0.0) or 0.0)
                if start is None or end is None:
                    continue
                candidate = {
                    "file": file_path,
                    "start": int(start),
                    "end": int(end),
                    "score": score,
                    "name": row.get("scope_path") or row.get("name"),
                }
                if best is None or score > float(best["score"]):
                    best = candidate
        if best is None:
            continue
        width = int(best["end"]) - int(best["start"]) + 1
        if (
            float(best["score"]) < SCOPED_RECALL_FLOOR_SCORE
            or width > MAX_RECALL_FLOOR_LINES
        ):
            continue
        if add_candidate(best):
            floor_added.append(best)

    candidates = [
        {"file": file_path, "start": start, "end": end}
        for file_path in file_order
        for start, end in merged_spans(intervals[file_path])
    ]
    return {
        "policy": "rank-plus-scoped-recall-floor-v1",
        "candidates": candidates,
        "ranked_candidates": ranked,
        "recall_floor_added": floor_added,
        "unique_lines": unique_line_count(),
        "line_budget": line_budget,
    }


def sanitize_patch(patch: str) -> tuple[str, list[str]]:
    chunks = re.split(r"(?=^diff --git a/)", patch, flags=re.MULTILINE)
    kept: list[str] = []
    dropped: list[str] = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        match = re.match(r"diff --git a/(.+?) b/(.+?)\n", chunk)
        if match and (
            any(
                match.group(2).startswith(prefix) for prefix in GENERATED_PATCH_PREFIXES
            )
            or Path(match.group(2)).name in GENERATED_PATCH_BASENAMES
        ):
            dropped.append(match.group(2))
            continue
        kept.append(chunk)
    cleaned = "".join(kept)
    if cleaned and not cleaned.endswith("\n"):
        cleaned += "\n"
    return cleaned, dropped


def load_completed(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {
        str(json.loads(line).get("instance_id"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, separators=(",", ":")) + "\n")


def docker_image_ids() -> set[str]:
    result = subprocess.run(
        ["docker", "image", "ls", "--quiet"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def remove_new_docker_images(before: set[str]) -> list[str]:
    new_images = sorted(docker_image_ids() - before)
    if new_images:
        subprocess.run(
            ["docker", "image", "rm", "--force", *new_images],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return new_images


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--contextbench-root", type=Path, required=True)
    parser.add_argument("--agent-python", type=Path, required=True)
    parser.add_argument("--base-agent-config", type=Path, required=True)
    parser.add_argument("--rerank-model-dir", type=Path, required=True)
    parser.add_argument("--query-plan-file", type=Path, required=True)
    parser.add_argument("--selector-model", default="gpt-5")
    parser.add_argument("--agent-model", default="openai/gpt-5")
    parser.add_argument(
        "--line-budget", type=int, default=retrieval.DEFAULT_LINE_BUDGET
    )
    parser.add_argument("--instance-id")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument(
        "--history-days",
        type=int,
        default=365,
        help="Replay only history reachable behind the task base commit; 0 disables history.",
    )
    parser.add_argument("--keep-index", action="store_true")
    parser.add_argument(
        "--graph-cache-dir",
        type=Path,
        help="Persistent content-addressed MemDB cache; completed entries reuse embeddings.",
    )
    parser.add_argument(
        "--cache-namespace",
        default="contextbench-v1",
        help="Bump when the Memtrace graph or embedding schema changes.",
    )
    parser.add_argument(
        "--remove-new-images",
        action="store_true",
        help="Remove Docker images first pulled during each task after archiving its output.",
    )
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()

    retrieval.load_env_file(Path(".env"))
    retrieval.validate_rerank_model(args.rerank_model_dir)
    rows = retrieval.load_rows(args.dataset, args.instance_id, args.limit)
    if not rows:
        raise SystemExit("no matching ContextBench rows")

    predictions_path = args.output_dir / "predictions.jsonl"
    swebench_predictions_path = args.output_dir / "swebench-predictions.jsonl"
    failures_path = args.output_dir / "failures.jsonl"
    audit_dir = args.output_dir / "audit"
    completed = load_completed(predictions_path)
    query_plans = {}
    if args.query_plan_file.is_file():
        query_plans = json.loads(args.query_plan_file.read_text(encoding="utf-8"))

    for index, row in enumerate(rows, 1):
        instance_id = str(row["instance_id"])
        original_instance_id = str(row["original_inst_id"])
        if instance_id in completed:
            print(f"[{index}/{len(rows)}] skip {instance_id}", file=sys.stderr)
            continue
        print(
            f"[{index}/{len(rows)}] {instance_id} ({original_instance_id})",
            file=sys.stderr,
        )
        slug = slugify(instance_id)
        instance_work = args.work_dir / slug
        repo_root = instance_work / "repo"
        started = time.monotonic()
        images_before = docker_image_ids() if args.remove_new_images else set()
        try:
            retrieval.ensure_checkout(
                str(row["repo_url"]), str(row["base_commit"]), repo_root
            )
            cache_entry = None
            cache_complete = False
            if args.graph_cache_dir:
                cache_manifest = retrieval.graph_cache_manifest(
                    instance_id,
                    str(row["repo_url"]),
                    str(row["base_commit"]),
                    args.cache_namespace,
                    repo_root,
                )
                cache_manifest["agent_localization_policy"] = AGENT_LOCALIZATION_POLICY
                cache_manifest["agent_history_days"] = str(args.history_days)
                cache_entry = args.graph_cache_dir / retrieval.graph_cache_key(
                    str(row["repo_url"]),
                    str(row["base_commit"]),
                    args.cache_namespace,
                )
                cache_complete = retrieval.graph_cache_is_reusable(
                    cache_entry, cache_manifest
                )
                if not cache_complete:
                    shutil.rmtree(cache_entry, ignore_errors=True)
                    cache_entry.mkdir(parents=True, exist_ok=True)
            else:
                shutil.rmtree(instance_work / "memdb", ignore_errors=True)
                shutil.rmtree(instance_work / "state", ignore_errors=True)
            env = retrieval.memtrace_env(
                repo_root, instance_work, args.rerank_model_dir, cache_entry
            )
            retrieval_prediction, audit = retrieval.retrieve_instance(
                row,
                repo_root,
                env,
                args.line_budget,
                args.selector_model,
                query_plans.get(instance_id),
                reuse_index=cache_complete,
            )
            history_audit = (
                {
                    "enabled": args.history_days > 0,
                    "days": args.history_days,
                    "cache_hit": True,
                }
                if cache_complete
                else prepare_history(repo_root, env, args.history_days)
            )
            if cache_entry and not cache_complete:
                retrieval.write_graph_cache_manifest(cache_entry, cache_manifest)
            if instance_id not in query_plans:
                query_plans[instance_id] = audit["search_queries"]
                args.query_plan_file.parent.mkdir(parents=True, exist_ok=True)
                args.query_plan_file.write_text(
                    json.dumps(query_plans, indent=2) + "\n", encoding="utf-8"
                )

            settings = benchmark_settings(instance_id, str(row["repo"]))
            seed_context = render_seed_context(
                repo_root, audit["final_candidates"], settings.container_root
            )
            if not seed_context:
                raise RuntimeError("Memtrace selected no readable seed context")
            config_path = instance_work / "agent-config.yaml"
            base_config = args.base_agent_config
            if settings.use_multi_config:
                multi_config = args.base_agent_config.with_name("swebench_multi.yaml")
                if not multi_config.is_file():
                    raise RuntimeError(
                        f"Multi-SWE-agent config missing: {multi_config}"
                    )
                base_config = multi_config
            docker_wrapper = instance_work / "docker-with-memtrace-bridge"
            write_docker_wrapper(docker_wrapper, HERE / "agent_tool_client.py")
            agent_output = instance_work / "agent-output"
            # The outer predictions file is the resume authority. A rejected
            # protocol run may still leave MiniSWE's internal preds.json behind;
            # remove that task-local output so MiniSWE cannot silently skip the
            # retry and masquerade as a fresh empty trajectory.
            shutil.rmtree(agent_output, ignore_errors=True)
            with AgentToolBridge(
                repo_root,
                env,
                container_root=settings.container_root,
                require_history=args.history_days > 0,
                trace_path=instance_work / "agent-tool-trace.json",
                max_ranked_lines=args.line_budget,
            ) as bridge:
                write_agent_config(
                    base_config,
                    config_path,
                    seed_context,
                    args.agent_model,
                    settings.container_root,
                    bridge_url=bridge.url,
                    bridge_token=bridge.token,
                    docker_executable=docker_wrapper,
                    line_budget=args.line_budget,
                )
                agent_run = run_miniswe(
                    args.agent_python,
                    args.contextbench_root,
                    config_path,
                    original_instance_id,
                    agent_output,
                    args.agent_model,
                    args.timeout,
                    settings,
                )
                agent_tool_trace = list(bridge.trace)
            (instance_work / "agent.log").write_text(agent_run.stdout, encoding="utf-8")
            if agent_run.returncode != 0:
                raise RuntimeError(f"mini-SWE-agent exited {agent_run.returncode}")
            agent_protocol = validate_agent_tool_trace(
                agent_tool_trace, require_history=args.history_days > 0
            )

            agent_trajectory, raw_patch, raw_trajectory, trajectory_path = (
                load_agent_result(
                    args.contextbench_root, agent_output, original_instance_id
                )
            )
            patch, dropped_patch_files = sanitize_patch(raw_patch)
            if not patch.strip():
                raise RuntimeError(
                    "mini-SWE-agent produced no source patch after sanitization"
                )
            merged = merge_prediction(
                instance_id,
                retrieval_prediction,
                agent_trajectory,
                patch,
                agent_tool_trace,
                args.line_budget,
            )
            append_jsonl(predictions_path, merged)
            append_jsonl(
                swebench_predictions_path,
                {
                    "instance_id": original_instance_id,
                    "model_name_or_path": "memtrace+gpt-5",
                    "model_patch": patch,
                },
            )
            model_stats = raw_trajectory.get("info", {}).get("model_stats", {})
            audit["agent"] = {
                "model": args.agent_model,
                "cost_usd": float(model_stats.get("instance_cost", 0.0) or 0.0),
                "api_calls": int(model_stats.get("api_calls", 0) or 0),
                "exit_status": raw_trajectory.get("info", {}).get("exit_status"),
                "trajectory_path": str(trajectory_path),
                "patch_bytes": len(patch.encode("utf-8")),
                "raw_patch_bytes": len(raw_patch.encode("utf-8")),
                "dropped_generated_files": dropped_patch_files,
                "retrieval_steps": len(retrieval_prediction["traj_data"]["pred_steps"]),
                "agent_steps": len(agent_trajectory.get("pred_steps", [])),
                "localization_protocol": agent_protocol,
                "final_context_projection": project_agent_context(
                    agent_tool_trace, args.line_budget
                ),
                "memtrace_tool_trace": agent_tool_trace,
                "history_replay": history_audit,
            }
            audit["end_to_end_seconds"] = time.monotonic() - started
            audit_dir.mkdir(parents=True, exist_ok=True)
            (audit_dir / f"{slug}.json").write_text(
                json.dumps(audit, indent=2) + "\n", encoding="utf-8"
            )
        except Exception as error:
            append_jsonl(
                failures_path,
                {
                    "instance_id": instance_id,
                    "original_inst_id": original_instance_id,
                    "error": type(error).__name__,
                    "detail": str(error),
                    "elapsed_seconds": time.monotonic() - started,
                },
            )
            print(f"ERROR {instance_id}: {error}", file=sys.stderr)
            if args.fail_fast:
                raise
        finally:
            if args.remove_new_images:
                removed_images = remove_new_docker_images(images_before)
                if removed_images:
                    print(
                        f"CLEANUP {instance_id}: removed {len(removed_images)} newly pulled image(s)",
                        file=sys.stderr,
                    )
            if not args.keep_index and not args.graph_cache_dir:
                shutil.rmtree(instance_work / "memdb", ignore_errors=True)
                shutil.rmtree(instance_work / "state", ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
