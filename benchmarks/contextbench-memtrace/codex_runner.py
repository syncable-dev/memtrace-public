#!/usr/bin/env python3
"""Run a real Codex coding agent against an isolated, cached Memtrace graph.

The graph/embedding template is keyed by repository URL, base commit, and the
explicit cache namespace.  Its checkout is immutable and stable across
benchmark iterations; Codex edits a separate per-run checkout.  This matters
because Memtrace repository records intentionally bind to the path they were
indexed from.  A per-key file lock prevents two agents from opening the same
embedded MemDB concurrently.

Codex runs with an isolated CODEX_HOME containing only Memtrace's shipped
skills and a required task-specific Memtrace MCP server.  JSONL events are
converted into ContextBench's public trajectory format without consulting
gold context or a reference patch.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import runner as retrieval


CACHE_SCHEMA_VERSION = 2
CACHE_KIND = "codex-memtrace-template"
CODEX_POLICY = "codex-memtrace-skills-v1"
FINAL_CONTEXT_POLICY = "codex-hierarchical-recall-floor-v1"
PROJECTION_POLICIES = {
    "precision-80": {"primary_lines": 80, "cross_files": 0, "cross_lines": 0},
    "precision-90": {"primary_lines": 90, "cross_files": 0, "cross_lines": 0},
    "precision-100": {"primary_lines": 100, "cross_files": 0, "cross_lines": 0},
    "compact-multi": {"primary_lines": 80, "cross_files": 2, "cross_lines": 20},
    "compact-balanced": {
        "primary_lines": 100,
        "cross_files": 1,
        "cross_lines": 20,
    },
    "compact-anchor": {"primary_lines": 120, "cross_files": 0, "cross_lines": 0},
    "balanced": {"primary_lines": 120, "cross_files": 2, "cross_lines": 40},
    "anchor-heavy": {"primary_lines": 160, "cross_files": 1, "cross_lines": 40},
    "anchor-only": {"primary_lines": 200, "cross_files": 0, "cross_lines": 0},
}
DEFAULT_PROJECTION_VARIANT = "precision-90"
MAX_TRAJECTORY_WINDOW = 120
SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".swift",
    ".ts",
    ".tsx",
}


class CacheValidationError(RuntimeError):
    """The opened graph is readable but does not match its sealed identity."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def directory_identity(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = child.relative_to(path).as_posix()
        size = child.stat().st_size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256_file(child).encode("ascii"))
        digest.update(b"\0")
        files += 1
        total_bytes += size
    return {"files": files, "bytes": total_bytes, "sha256": digest.hexdigest()}


def git_output(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def checkout_is_exact(repo: Path, commit: str) -> bool:
    if not (repo / ".git").exists():
        return False
    try:
        return git_output(repo, "rev-parse", "HEAD") == commit
    except subprocess.CalledProcessError:
        return False


def ensure_clean_checkout(
    repo_url: str, base_commit: str, repo_root: Path, reference: Path | None = None
) -> None:
    if not (repo_root / ".git").exists():
        repo_root.parent.mkdir(parents=True, exist_ok=True)
        command = ["git", "clone", "--quiet"]
        if reference and (reference / ".git").exists():
            command += ["--reference-if-able", str(reference)]
        command += [repo_url, str(repo_root)]
        subprocess.run(command, check=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "fetch", "--quiet", "origin", base_commit],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "checkout", "--quiet", "--detach", base_commit],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "reset", "--quiet", "--hard", base_commit],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "clean", "-q", "-d", "-f", "-x"],
        check=True,
    )


def cache_manifest_path(cache_entry: Path) -> Path:
    return cache_entry / "complete.json"


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def expected_cache_identity(
    repo_url: str,
    base_commit: str,
    namespace: str,
    memtrace_binary: Path,
    rerank_model_dir: Path,
    history_days: int,
) -> dict[str, Any]:
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": CACHE_KIND,
        "repo_url": repo_url.rstrip("/"),
        "base_commit": base_commit,
        "namespace": namespace,
        "memtrace_binary_sha256": sha256_file(memtrace_binary),
        "rerank_model": directory_identity(rerank_model_dir),
        "history_days": history_days,
    }


def legacy_cache_matches(
    manifest: dict[str, Any], repo_url: str, base_commit: str, namespace: str
) -> bool:
    return all(
        (
            manifest.get("repo_url") == repo_url.rstrip("/"),
            manifest.get("base_commit") == base_commit,
            manifest.get("namespace") == namespace,
        )
    )


def cache_repo_root(cache_entry: Path, manifest: dict[str, Any]) -> Path | None:
    for key in ("index_repo_root", "workspace_root"):
        value = manifest.get(key)
        if isinstance(value, str) and value:
            candidate = Path(value)
            if candidate.is_dir():
                return candidate
    candidate = cache_entry / "repo"
    return candidate if candidate.is_dir() else None


def memtrace_environment(
    repo_root: Path,
    cache_entry: Path,
    rerank_model_dir: Path,
    memtrace_binary: Path,
    walker_include_dirs: Iterable[str] = (),
) -> dict[str, str]:
    env = retrieval.memtrace_env(
        repo_root,
        cache_entry,
        rerank_model_dir,
        cache_entry,
        walker_include_dirs=walker_include_dirs,
    )
    # ContextBench runs the exact local product build without a customer
    # license. Keep this in the runner rather than relying on a parent shell so
    # direct smoke tests and fleet runs exercise the same authenticated mode.
    env["MEMTRACE_DEV"] = "1"
    env["MEMTRACE_TELEMETRY"] = "off"
    env["PATH"] = f"{memtrace_binary.parent}:{env.get('PATH', '')}"
    return env


def summarize_index_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"raw_type": type(result).__name__}
    summary: dict[str, Any] = {}
    for key in (
        "repo_id",
        "nodes_created",
        "edges_created",
        "embeddings_created",
        "commit_sha",
        "status",
    ):
        if key in result:
            summary[key] = result[key]
    if isinstance(result.get("embedding"), dict):
        summary["embedding"] = result["embedding"]
    return summary


def validate_embedding_write(index_result: Any) -> None:
    if not isinstance(index_result, dict):
        raise RuntimeError(f"index returned no structured result: {index_result!r}")
    embedding = index_result.get("embedding")
    if isinstance(embedding, dict):
        if int(embedding.get("written", 0) or 0) <= 0:
            raise RuntimeError(f"index completed without embedding writes: {index_result}")
    elif int(index_result.get("embeddings_created", 0) or 0) <= 0:
        raise RuntimeError(f"index completed without embeddings: {index_result}")


def open_memtrace_client(
    repo_root: Path, env: dict[str, str], attempts: int = 3
) -> retrieval.McpClient:
    last_error: RuntimeError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return retrieval.McpClient(repo_root, env)
        except RuntimeError as error:
            last_error = error
            if attempt == attempts:
                break
            print(
                f"Memtrace MCP startup attempt {attempt}/{attempts} failed: {error}",
                file=sys.stderr,
            )
            time.sleep(attempt)
    assert last_error is not None
    raise last_error


def verify_index(
    repo_root: Path,
    cache_entry: Path,
    rerank_model_dir: Path,
    memtrace_binary: Path,
    base_commit: str,
) -> dict[str, Any]:
    env = memtrace_environment(
        repo_root, cache_entry, rerank_model_dir, memtrace_binary
    )
    client = open_memtrace_client(repo_root, env)
    try:
        repositories = client.call_tool("list_indexed_repositories", {})
    finally:
        client.close()
    if not isinstance(repositories, list) or len(repositories) != 1:
        raise CacheValidationError(
            f"cache expected one indexed repository, got {repositories}"
        )
    repository = repositories[0]
    if int(repository.get("nodes", 0) or 0) <= 0:
        raise CacheValidationError(f"cache repository has no nodes: {repository}")
    commit = str(repository.get("commit_sha") or "")
    if commit and commit != base_commit:
        raise CacheValidationError(
            f"cache commit mismatch: expected {base_commit}, got {commit}"
        )
    return repository


def prepare_history(
    repo_root: Path,
    cache_entry: Path,
    rerank_model_dir: Path,
    memtrace_binary: Path,
    history_days: int,
) -> dict[str, Any]:
    if history_days <= 0:
        return {"enabled": False, "days": 0}
    env = memtrace_environment(
        repo_root, cache_entry, rerank_model_dir, memtrace_binary
    )
    client = open_memtrace_client(repo_root, env)
    started = time.monotonic()
    try:
        repositories = client.call_tool("list_indexed_repositories", {})
        if not isinstance(repositories, list) or len(repositories) != 1:
            raise RuntimeError(f"history expected one repository, got {repositories}")
        repo_id = str(repositories[0]["repo_id"])
        snapshot_time = int(git_output(repo_root, "show", "-s", "--format=%ct", "HEAD"))
        snapshot_age_days = max(0, int((time.time() - snapshot_time) // 86400))
        result = client.call_tool(
            "replay_history",
            {"repo_id": repo_id, "days": snapshot_age_days + history_days},
        )
        job_id = result.get("job_id") if isinstance(result, dict) else None
        if job_id:
            deadline = time.monotonic() + 1800
            while time.monotonic() < deadline:
                status = client.call_tool("check_job_status", {"job_id": job_id})
                state = str(status.get("status") or "") if isinstance(status, dict) else ""
                if state in {"completed", "failed"}:
                    result = status
                    break
                time.sleep(2)
            else:
                raise RuntimeError("history replay exceeded 1800 seconds")
        return {
            "enabled": True,
            "lookback_days": history_days,
            "seconds": round(time.monotonic() - started, 3),
            "result": result,
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


def prepare_cache(
    row: dict[str, Any],
    graph_cache_dir: Path,
    namespace: str,
    memtrace_binary: Path,
    rerank_model_dir: Path,
    history_days: int,
    progress: Callable[[str], None] | None = None,
) -> tuple[Path, Path, dict[str, Any], Any]:
    repo_url = str(row["repo_url"])
    base_commit = str(row["base_commit"])
    key = retrieval.graph_cache_key(repo_url, base_commit, namespace)
    cache_entry = graph_cache_dir / key
    lock_dir = graph_cache_dir / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_handle = (lock_dir / f"{key}.lock").open("a+")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

    expected = expected_cache_identity(
        repo_url,
        base_commit,
        namespace,
        memtrace_binary,
        rerank_model_dir,
        history_days,
    )
    manifest = read_json(cache_manifest_path(cache_entry))
    repo_root = cache_repo_root(cache_entry, manifest)
    cache_hit = False
    legacy_hit = False
    repository: dict[str, Any] | None = None
    if repo_root and checkout_is_exact(repo_root, base_commit):
        if progress is not None:
            progress("checkout_ready")
        current_match = all(manifest.get(key) == value for key, value in expected.items())
        legacy_hit = legacy_cache_matches(manifest, repo_url, base_commit, namespace)
        if current_match or legacy_hit:
            try:
                repository = verify_index(
                    repo_root,
                    cache_entry,
                    rerank_model_dir,
                    memtrace_binary,
                    base_commit,
                )
                cache_hit = True
            except CacheValidationError:
                cache_hit = False

    index_result: Any = {"cache_hit": True, "embeddings_reused": True}
    if not cache_hit:
        shutil.rmtree(cache_entry, ignore_errors=True)
        cache_entry.mkdir(parents=True, exist_ok=True)
        repo_root = cache_entry / "repo"
        ensure_clean_checkout(repo_url, base_commit, repo_root)
        if progress is not None:
            progress("checkout_ready")
        reincluded = retrieval.write_tracked_dir_reincludes(repo_root)
        env = memtrace_environment(
            repo_root,
            cache_entry,
            rerank_model_dir,
            memtrace_binary,
            reincluded,
        )
        client = open_memtrace_client(repo_root, env)
        try:
            index_result = client.call_tool(
                "index_directory",
                {
                    "path": str(repo_root),
                    "clear_existing": True,
                    "defer_replay": True,
                    "skip_embed": False,
                },
            )
        finally:
            client.close()
        validate_embedding_write(index_result)
        repository = verify_index(
            repo_root,
            cache_entry,
            rerank_model_dir,
            memtrace_binary,
            base_commit,
        )
    assert repo_root is not None and repository is not None

    history = manifest.get("history") if cache_hit else None
    if not isinstance(history, dict) or history.get("unavailable"):
        history = prepare_history(
            repo_root,
            cache_entry,
            rerank_model_dir,
            memtrace_binary,
            history_days,
        )
    complete = {
        **expected,
        "index_repo_root": str(repo_root.resolve()),
        "repository": repository,
        "index": summarize_index_result(index_result),
        "history": history,
        "legacy_cache_upgraded": legacy_hit,
        "completed_at_unix": time.time(),
    }
    atomic_write_json(cache_manifest_path(cache_entry), complete)
    cache_audit = {
        "key": key,
        "entry": str(cache_entry),
        "cache_hit": cache_hit,
        "legacy_cache_upgraded": legacy_hit,
        "index": summarize_index_result(index_result),
        "repository": repository,
        "history": history,
    }
    # The caller retains the exclusive handle for the whole Codex turn.  This
    # prevents duplicate instances at one repo+commit from sharing a writable
    # embedded MemDB.  ContextBench's public datasets currently have unique
    # repo+commit keys, so unrelated tasks still run fully concurrently.
    return cache_entry, repo_root, cache_audit, lock_handle


def toml_string(value: str) -> str:
    return json.dumps(value)


def configure_codex_home(
    codex_home: Path,
    skills_dir: Path,
    memtrace_binary: Path,
    mcp_env: dict[str, str],
    index_repo_root: Path,
    model: str,
) -> dict[str, Any]:
    shutil.rmtree(codex_home, ignore_errors=True)
    installed = codex_home / "skills"
    installed.mkdir(parents=True, exist_ok=True)
    skill_names: list[str] = []
    for source in sorted(skills_dir.iterdir()):
        if not source.is_dir() or not (source / "SKILL.md").is_file():
            continue
        shutil.copytree(source, installed / source.name)
        skill_names.append(source.name)
    if "memtrace-first" not in skill_names or "memtrace-search" not in skill_names:
        raise RuntimeError(
            f"Memtrace skill bundle is incomplete: expected memtrace-first and memtrace-search in {skills_dir}"
        )

    allowed_env = {
        key: value
        for key, value in mcp_env.items()
        if key.startswith("MEMTRACE_") or key.startswith("MEMCORTEX_") or key == "CI"
    }
    lines = [
        f"model = {toml_string(model)}",
        'model_reasoning_effort = "medium"',
        'approval_policy = "never"',
        'sandbox_mode = "danger-full-access"',
        "",
        "[shell_environment_policy]",
        'inherit = "all"',
        "ignore_default_excludes = false",
        'exclude = ["OPENAI_*", "CODEX_*", "AWS_*"]',
        "",
        "[mcp_servers.memtrace]",
        f"command = {toml_string(str(memtrace_binary))}",
        'args = ["mcp"]',
        f"cwd = {toml_string(str(index_repo_root))}",
        "required = true",
        "startup_timeout_sec = 90",
        "tool_timeout_sec = 180",
        'default_tools_approval_mode = "approve"',
        "",
        "[mcp_servers.memtrace.env]",
    ]
    lines.extend(
        f"{key} = {toml_string(value)}" for key, value in sorted(allowed_env.items())
    )
    (codex_home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"skills": skill_names, "config": str(codex_home / "config.toml")}


def write_output_schema(path: Path) -> None:
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "tests": {"type": "array", "items": {"type": "string"}},
            "contexts": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "start": {"type": "integer", "minimum": 1},
                        "end": {"type": "integer", "minimum": 1},
                    },
                    "required": ["file", "start", "end"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["summary", "tests", "contexts"],
        "additionalProperties": False,
    }
    atomic_write_json(path, schema)


def render_prompt(row: dict[str, Any], line_budget: int) -> str:
    issue = str(row["problem_statement"])
    return f"""Use $memtrace-first before source-code discovery, then solve this task as a coding agent.

Memtrace is already fully indexed and embedded for the exact base commit. Use its MCP tools as your primary code memory and search surface. You may formulate and refine your own searches just as you would in a normal Codex task. Inspect exact source, make the fix, and run relevant tests. Do not search for or use ContextBench gold context, a reference patch, or benchmark answer artifacts.

Keep discovery efficient: use ranked search limits of at most 10, source windows of at most 120 lines, and normally no more than 24 Memtrace calls before implementing and testing. Prefer one precise follow-up query over repeatedly reopening the same file. This is a budget, not permission to stop before you have enough evidence for a correct fix.

Your structured final `contexts` must rank only the smallest exact repository file -> symbol -> line ranges that directly governed your fix. The harness adds a deterministic recall floor from your observed Memtrace trajectory, so do not pad this list. Put only the repository-relative file path in `file` (never append `:line`). Keep their union within {line_budget} source lines. Include a test, configuration, fixture, or authored documentation range when it directly defines or verifies the behavior being fixed; do not include broad exploration windows.

Task:
{issue}
"""


def normalize_repo_path(path: str, repo_root: Path) -> str:
    value = path.strip().strip("'\"").replace("\\", "/")
    if not value:
        return ""
    root = str(repo_root.resolve()).replace("\\", "/")
    if value.startswith(root + "/"):
        value = value[len(root) + 1 :]
    while value.startswith("./"):
        value = value[2:]
    value = value.lstrip("/")
    # Structured output sometimes still renders a location as `path:line`.
    # Strip that presentation suffix before resolving against the checkout.
    value = re.sub(r":\d+(?:-\d+)?$", "", value)
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if ".." in parts:
        return ""
    for index in range(len(parts)):
        candidate = Path(*parts[index:]).as_posix()
        target = (repo_root / candidate).resolve()
        try:
            target.relative_to(repo_root.resolve())
        except ValueError:
            continue
        if target.is_file():
            return candidate
    return ""


def result_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return
        yield from result_objects(decoded)
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            yield from result_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from result_objects(child)


def spans_from_object(value: Any, repo_root: Path) -> dict[str, list[dict[str, int]]]:
    spans: dict[str, list[dict[str, int]]] = {}
    for item in result_objects(value):
        raw_path = item.get("file_path") or item.get("file") or item.get("path")
        if not isinstance(raw_path, str):
            continue
        file_path = normalize_repo_path(raw_path, repo_root)
        if not file_path:
            continue
        start = item.get("symbol_start_line", item.get("start_line", item.get("start")))
        end = item.get("symbol_end_line", item.get("end_line", item.get("end")))
        if isinstance(start, int) and isinstance(end, int) and start > 0 and end >= start:
            spans.setdefault(file_path, []).append(
                {"type": "line", "start": start, "end": end}
            )
        else:
            spans.setdefault(file_path, [])
    return spans


def command_step(item: dict[str, Any], repo_root: Path) -> dict[str, Any] | None:
    command = str(item.get("command") or "")
    output = str(item.get("aggregated_output") or item.get("output") or "")
    spans: dict[str, list[dict[str, int]]] = {}

    patterns = (
        re.compile(r"sed\s+-n\s+['\"]?(\d+),(\d+)p['\"]?\s+([^\s;&|>]+)"),
        re.compile(r"nl\s+(?:-[A-Za-z]+\s+)*([^\s|]+).*?sed\s+-n\s+['\"]?(\d+),(\d+)p"),
    )
    for match in patterns[0].finditer(command):
        start, end, raw_path = int(match.group(1)), int(match.group(2)), match.group(3)
        file_path = normalize_repo_path(raw_path, repo_root)
        if file_path:
            spans.setdefault(file_path, []).append(
                {"type": "line", "start": start, "end": end}
            )
    for match in patterns[1].finditer(command):
        raw_path, start, end = match.group(1), int(match.group(2)), int(match.group(3))
        file_path = normalize_repo_path(raw_path, repo_root)
        if file_path:
            spans.setdefault(file_path, []).append(
                {"type": "line", "start": start, "end": end}
            )
    for match in re.finditer(r"\bhead\s+-n\s+(\d+)\s+([^\s;&|>]+)", command):
        end, raw_path = int(match.group(1)), match.group(2)
        file_path = normalize_repo_path(raw_path, repo_root)
        if file_path:
            spans.setdefault(file_path, []).append(
                {"type": "line", "start": 1, "end": end}
            )
    for match in re.finditer(r"\bcat\s+([^\s;&|>]+)", command):
        file_path = normalize_repo_path(match.group(1), repo_root)
        if file_path and Path(file_path).suffix in SOURCE_SUFFIXES:
            spans.setdefault(file_path, [])
    for raw in output.splitlines():
        match = re.match(r"([^:\n]+\.[A-Za-z0-9]+):(\d+):", raw)
        if not match:
            continue
        file_path = normalize_repo_path(match.group(1), repo_root)
        if file_path:
            line = int(match.group(2))
            spans.setdefault(file_path, []).append(
                {"type": "line", "start": line, "end": line}
            )
    if not spans:
        return None
    return {"files": list(spans), "spans": spans, "symbols": {}}


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def trajectory_from_events(
    events: list[dict[str, Any]], repo_root: Path
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    mcp_calls = 0
    usage: dict[str, Any] = {}
    for event in events:
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if "mcp" in item_type.lower():
            mcp_calls += 1
            spans = spans_from_object(item, repo_root)
            if spans:
                steps.append({"files": list(spans), "spans": spans, "symbols": {}})
        elif item_type == "command_execution":
            step = command_step(item, repo_root)
            if step:
                steps.append(step)
    return steps, mcp_calls, usage


def diff_contexts(patch: str) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    current = ""
    for raw in patch.splitlines():
        match = re.match(r"diff --git a/(.+) b/(.+)", raw)
        if match:
            current = match.group(1)
            continue
        hunk = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw)
        if not current or not hunk:
            continue
        old_start = int(hunk.group(1))
        old_count = int(hunk.group(2) or 1)
        start = max(1, old_start)
        end = start + max(1, old_count) - 1
        contexts.append({"file": current, "start": start, "end": end})
    return contexts


def bounded_contexts(
    candidates: Iterable[dict[str, Any]], repo_root: Path, line_budget: int
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    intervals: dict[str, list[tuple[int, int]]] = {}

    def merged_count() -> int:
        count = 0
        for spans in intervals.values():
            end = 0
            for start, stop in sorted(spans):
                if start > end + 1:
                    count += stop - start + 1
                elif stop > end:
                    count += stop - end
                end = max(end, stop)
        return count

    for item in candidates:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("file")
        start, end = item.get("start"), item.get("end")
        if not isinstance(raw_path, str) or not isinstance(start, int) or not isinstance(end, int):
            continue
        file_path = normalize_repo_path(raw_path, repo_root)
        if not file_path or start < 1 or end < start:
            continue
        line_total = len(
            (repo_root / file_path).read_text(encoding="utf-8", errors="replace").splitlines()
        )
        end = min(end, max(1, line_total))
        if end < start:
            continue
        intervals.setdefault(file_path, []).append((start, end))
        if merged_count() > line_budget:
            intervals[file_path].pop()
            if not intervals[file_path]:
                del intervals[file_path]
            continue
        candidate = {"file": file_path, "start": start, "end": end}
        if candidate not in selected:
            selected.append(candidate)
    return selected


def projection_path_allowed(path: str) -> bool:
    lowered = path.lower()
    parts = set(Path(lowered).parts)
    if parts.intersection(
        {"node_modules", "vendor", "vendored", "third_party"}
    ):
        return False
    return Path(lowered).suffix in SOURCE_SUFFIXES.union(
        {".cfg", ".ini", ".json", ".md", ".rst", ".toml", ".yaml", ".yml"}
    )


def clip_context(
    context: dict[str, Any],
    limit: int,
    anchors: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    start, end = int(context["start"]), int(context["end"])
    if end - start + 1 <= limit:
        return dict(context)
    focus = (start + end) // 2
    same_file = [
        item
        for item in anchors
        if item.get("file") == context.get("file")
        and int(item.get("end", 0)) >= start
        and int(item.get("start", end + 1)) <= end
    ]
    if same_file:
        anchor = same_file[0]
        focus = (int(anchor["start"]) + int(anchor["end"])) // 2
    clipped_start = max(start, focus - limit // 2)
    clipped_end = clipped_start + limit - 1
    if clipped_end > end:
        clipped_end = end
        clipped_start = end - limit + 1
    return {"file": context["file"], "start": clipped_start, "end": clipped_end}


def project_hierarchical_recall(
    structured: Iterable[dict[str, Any]],
    steps: Iterable[dict[str, Any]],
    patch: str,
    line_budget: int,
    variant: str = DEFAULT_PROJECTION_VARIANT,
) -> list[dict[str, Any]]:
    """Keep Codex's exact picks, then spend unused budget on observed context.

    This is deliberately task-agnostic and model-free.  The hierarchy is:
    edited/final files first, then independently-supported production files,
    then the most repeated and most recent line windows within each file.
    """
    if variant not in PROJECTION_POLICIES:
        raise ValueError(f"unknown projection variant: {variant}")
    policy = PROJECTION_POLICIES[variant]
    seeds = [
        {"file": str(item["file"]), "start": int(item["start"]), "end": int(item["end"])}
        for item in [*structured, *diff_contexts(patch)]
        if isinstance(item, dict)
        and isinstance(item.get("file"), str)
        and isinstance(item.get("start"), int)
        and isinstance(item.get("end"), int)
        and int(item["start"]) > 0
        and int(item["end"]) >= int(item["start"])
        and projection_path_allowed(str(item["file"]))
    ]
    anchor_files = {item["file"] for item in seeds}
    step_list = list(steps)
    file_steps: dict[str, set[int]] = {}
    occurrences: dict[tuple[str, int, int], set[int]] = {}
    ranked_by_file: dict[str, list[dict[str, Any]]] = {}

    for step_index, step in enumerate(step_list):
        spans = step.get("spans") if isinstance(step, dict) else None
        if not isinstance(spans, dict):
            continue
        for file, values in spans.items():
            if not isinstance(file, str) or not projection_path_allowed(file):
                continue
            if not isinstance(values, list):
                continue
            for span in values:
                if not isinstance(span, dict):
                    continue
                start, end = span.get("start"), span.get("end")
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or not isinstance(end, int)
                    or isinstance(end, bool)
                    or start < 1
                    or end < start
                ):
                    continue
                candidate = clip_context(
                    {"file": file, "start": start, "end": end},
                    MAX_TRAJECTORY_WINDOW,
                    seeds,
                )
                key = (file, candidate["start"], candidate["end"])
                occurrences.setdefault(key, set()).add(step_index)
                file_steps.setdefault(file, set()).add(step_index)
        retrieval_evidence = step.get("retrieval") if isinstance(step, dict) else None
        if isinstance(retrieval_evidence, list):
            for evidence in retrieval_evidence:
                if not isinstance(evidence, dict):
                    continue
                file = evidence.get("file")
                rank = evidence.get("rank")
                if (
                    not isinstance(file, str)
                    or not isinstance(rank, int)
                    or isinstance(rank, bool)
                    or rank < 1
                ):
                    continue
                ranked_by_file.setdefault(file, []).append(
                    {**evidence, "step": step_index}
                )

    def overlaps_seed(file: str, start: int, end: int) -> bool:
        return any(
            item["file"] == file
            and item["end"] >= start - 1
            and item["start"] <= end + 1
            for item in seeds
        )

    candidates = [
        {"file": file, "start": start, "end": end}
        for file, start, end in occurrences
    ]

    def candidate_rank(item: dict[str, Any]) -> tuple[Any, ...]:
        key = (item["file"], item["start"], item["end"])
        width = item["end"] - item["start"] + 1
        span_steps = len(occurrences[key])
        file_step_count = len(file_steps.get(item["file"], set()))
        recency = max(occurrences[key], default=-1)
        common = (
            item["file"] not in anchor_files,
            not overlaps_seed(item["file"], item["start"], item["end"]),
        )
        mode = str(policy.get("candidate_rank", "consensus"))
        if mode == "memtrace-rank":
            ranked = ranked_by_file.get(item["file"], [])
            overlapping = [
                evidence
                for evidence in ranked
                if isinstance(evidence.get("start"), int)
                and isinstance(evidence.get("end"), int)
                and int(evidence["end"]) >= item["start"]
                and int(evidence["start"]) <= item["end"]
            ]
            exact = [
                evidence
                for evidence in overlapping
                if str(evidence.get("kind") or "").lower() != "file"
            ]
            evidence = (
                not bool(exact),
                min((int(value["rank"]) for value in exact), default=10**9),
                min((int(value["rank"]) for value in ranked), default=10**9),
                -len({int(value["step"]) for value in exact}),
                width,
                -span_steps,
                -file_step_count,
                -recency,
            )
        elif mode == "narrow":
            evidence = (width, -span_steps, -file_step_count, -recency)
        elif mode == "repeated-narrow":
            evidence = (-span_steps, width, -file_step_count, -recency)
        elif mode == "recent-narrow":
            evidence = (-recency, width, -span_steps, -file_step_count)
        elif mode == "density":
            evidence = (
                -(span_steps * 10_000 // width),
                width,
                -file_step_count,
                -recency,
            )
        else:
            evidence = (-file_step_count, -span_steps, -recency, width)
        return (*common, *evidence, item["file"], item["start"])

    candidates.sort(key=candidate_rank)

    selected: list[dict[str, Any]] = []
    covered: set[tuple[str, int]] = set()

    def append(item: dict[str, Any], cap: int) -> bool:
        nonlocal covered
        if cap <= len(covered):
            return False
        remaining = min(line_budget, cap) - len(covered)
        limit = max(1, remaining)
        item_lines = int(item["end"]) - int(item["start"]) + 1
        while True:
            candidate = clip_context(item, min(item_lines, limit), seeds)
            lines = {
                (candidate["file"], line)
                for line in range(candidate["start"], candidate["end"] + 1)
            }
            novel = lines - covered
            if (
                not policy.get("fill_novel_lines")
                or len(novel) >= remaining
                or limit >= item_lines
            ):
                break
            limit = min(item_lines, limit + remaining - len(novel))
        if not novel or len(novel) > remaining:
            return False
        if candidate not in selected:
            selected.append(candidate)
        covered |= lines
        return True

    for seed in seeds:
        append(seed, line_budget)
    primary_cap = min(line_budget, max(len(covered), int(policy["primary_lines"])))
    seed_padding = int(policy.get("seed_padding", 0))
    if seed_padding > 0:
        seed_padding_step = max(1, int(policy.get("seed_padding_step", seed_padding)))
        padding_rings = list(range(seed_padding_step, seed_padding + 1, seed_padding_step))
        if not padding_rings or padding_rings[-1] != seed_padding:
            padding_rings.append(seed_padding)
        for padding in padding_rings:
            for seed in seeds:
                append(
                    {
                        "file": seed["file"],
                        "start": max(1, seed["start"] - padding),
                        "end": seed["end"] + padding,
                    },
                    primary_cap,
                )
                if len(covered) >= primary_cap:
                    break
            if len(covered) >= primary_cap:
                break
    primary_candidates = [
        candidate for candidate in candidates if candidate["file"] in anchor_files
    ]
    primary_chunk_lines = int(policy.get("primary_chunk_lines", 0))
    if not policy.get("trajectory_fill", True):
        primary_candidates = []
    if primary_chunk_lines > 0:
        while len(covered) < primary_cap:
            before = len(covered)
            for candidate in primary_candidates:
                append(
                    candidate,
                    min(primary_cap, len(covered) + primary_chunk_lines),
                )
                if len(covered) >= primary_cap:
                    break
            if len(covered) == before:
                break
    else:
        for candidate in primary_candidates:
            append(candidate, primary_cap)

    cross_files = 0
    used_cross_files: set[str] = set()
    for candidate in candidates:
        file = candidate["file"]
        if (
            file in anchor_files
            or file in used_cross_files
            or len(file_steps.get(file, set()))
            < int(policy.get("min_file_steps", 2))
            or cross_files >= int(policy["cross_files"])
        ):
            continue
        cross_cap = min(
            line_budget,
            len(covered) + int(policy["cross_lines"]),
        )
        if append(candidate, cross_cap):
            used_cross_files.add(file)
            cross_files += 1
    fill_lines = int(policy.get("fill_lines", 0))
    if fill_lines > 0:
        for candidate in candidates:
            if len(covered) >= line_budget:
                break
            file = candidate["file"]
            if file not in anchor_files and file not in used_cross_files:
                continue
            fill_cap = min(line_budget, len(covered) + fill_lines)
            append(candidate, fill_cap)
    return selected


def git_patch(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--binary", "--no-ext-diff", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, separators=(",", ":")) + "\n")
        handle.flush()


def load_completed(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {
        str(json.loads(line)["instance_id"])
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def run_codex(
    codex_binary: Path,
    codex_home: Path,
    repo_root: Path,
    prompt: str,
    schema_path: Path,
    final_path: Path,
    events_path: Path,
    model: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for Codex execution")
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    env["CODEX_API_KEY"] = api_key
    env.pop("OPENAI_API_KEY", None)
    command = [
        str(codex_binary),
        "exec",
        "--json",
        "--ephemeral",
        "--strict-config",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        model,
        "--cd",
        str(repo_root),
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(final_path),
        prompt,
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        events_path.write_text(error.stdout or "", encoding="utf-8")
        raise RuntimeError(f"Codex exceeded {timeout} seconds") from error
    events_path.write_text(completed.stdout, encoding="utf-8")
    (events_path.with_suffix(".stderr.log")).write_text(
        completed.stderr, encoding="utf-8"
    )
    setattr(completed, "elapsed_seconds", round(time.monotonic() - started, 3))
    return completed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--instance-id")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--graph-cache-dir", type=Path, required=True)
    parser.add_argument("--cache-namespace", required=True)
    parser.add_argument("--memtrace-binary", type=Path, required=True)
    parser.add_argument("--memtrace-skills-dir", type=Path, required=True)
    parser.add_argument("--rerank-model-dir", type=Path, required=True)
    parser.add_argument("--codex-binary", type=Path, required=True)
    parser.add_argument("--agent-model", default="gpt-5")
    parser.add_argument("--line-budget", type=int, default=200)
    parser.add_argument(
        "--history-days",
        type=int,
        default=0,
        help="optional temporal replay lookback; the ContextBench Codex lane uses 0",
    )
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()

    retrieval.load_env_file(Path(".env"))
    retrieval.validate_rerank_model(args.rerank_model_dir)
    for required in (args.memtrace_binary, args.codex_binary):
        if not required.is_file() or not os.access(required, os.X_OK):
            raise SystemExit(f"required executable missing: {required}")
    if not args.memtrace_skills_dir.is_dir():
        raise SystemExit(f"Memtrace skill bundle missing: {args.memtrace_skills_dir}")
    if args.line_budget < 1 or args.timeout < 1:
        raise SystemExit("line budget and timeout must be positive")

    rows = retrieval.load_rows(args.dataset, args.instance_id, args.limit)
    if not rows:
        raise SystemExit("no matching ContextBench rows")
    predictions_path = args.output_dir / "predictions.jsonl"
    failures_path = args.output_dir / "failures.jsonl"
    audit_dir = args.output_dir / "audit"
    completed_ids = load_completed(predictions_path)

    for index, row in enumerate(rows, 1):
        instance_id = str(row["instance_id"])
        if instance_id in completed_ids:
            print(f"[{index}/{len(rows)}] skip {instance_id}", file=sys.stderr)
            continue
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", instance_id)
        instance_work = args.work_dir / slug
        repo_root = instance_work / "repo"
        started = time.monotonic()
        lock_handle = None
        try:
            print(f"[{index}/{len(rows)}] {instance_id}", file=sys.stderr)
            cache_entry, index_repo, cache_audit, lock_handle = prepare_cache(
                row,
                args.graph_cache_dir,
                args.cache_namespace,
                args.memtrace_binary,
                args.rerank_model_dir,
                args.history_days,
            )
            ensure_clean_checkout(
                str(row["repo_url"]),
                str(row["base_commit"]),
                repo_root,
                reference=index_repo,
            )
            mcp_env = memtrace_environment(
                index_repo,
                cache_entry,
                args.rerank_model_dir,
                args.memtrace_binary,
            )
            codex_home = instance_work / "codex-home"
            profile_audit = configure_codex_home(
                codex_home,
                args.memtrace_skills_dir,
                args.memtrace_binary,
                mcp_env,
                index_repo,
                args.agent_model,
            )
            schema_path = instance_work / "final-schema.json"
            final_path = instance_work / "codex-final.json"
            events_path = instance_work / "codex-events.jsonl"
            write_output_schema(schema_path)
            prompt = render_prompt(row, args.line_budget)
            codex_attempts: list[dict[str, Any]] = []
            for codex_attempt in range(1, 3):
                final_path = instance_work / f"codex-final-{codex_attempt}.json"
                events_path = instance_work / f"codex-events-{codex_attempt}.jsonl"
                codex = run_codex(
                    args.codex_binary,
                    codex_home,
                    repo_root,
                    prompt,
                    schema_path,
                    final_path,
                    events_path,
                    args.agent_model,
                    args.timeout,
                )
                if codex.returncode != 0:
                    raise RuntimeError(
                        f"Codex exited with return code {codex.returncode}"
                    )
                events = load_events(events_path)
                steps, mcp_calls, usage = trajectory_from_events(events, repo_root)
                codex_attempts.append(
                    {
                        "attempt": codex_attempt,
                        "events": str(events_path),
                        "mcp_calls": mcp_calls,
                        "elapsed_seconds": getattr(codex, "elapsed_seconds", None),
                    }
                )
                if mcp_calls >= 1:
                    break
                if codex_attempt == 2:
                    raise RuntimeError(
                        "Codex completed twice without a Memtrace MCP tool call"
                    )
                ensure_clean_checkout(
                    str(row["repo_url"]),
                    str(row["base_commit"]),
                    repo_root,
                    reference=index_repo,
                )
                prompt = (
                    render_prompt(row, args.line_budget)
                    + "\nThis is a clean retry because the prior turn made no Memtrace "
                    "MCP call. Before shell or source discovery, invoke "
                    "$memtrace-first and perform at least one Memtrace MCP search.\n"
                )
            final = read_json(final_path)
            patch = git_patch(repo_root)
            structured_contexts = bounded_contexts(
                final.get("contexts", []) if isinstance(final, dict) else [],
                repo_root,
                args.line_budget,
            )
            final_source = "codex_structured_plus_trajectory"
            if not structured_contexts:
                structured_contexts = bounded_contexts(
                    diff_contexts(patch), repo_root, args.line_budget
                )
                final_source = "git_diff_fallback"
            contexts = project_hierarchical_recall(
                structured_contexts,
                steps,
                patch,
                args.line_budget,
                DEFAULT_PROJECTION_VARIANT,
            )
            contexts = bounded_contexts(contexts, repo_root, args.line_budget)
            if not contexts:
                raise RuntimeError("Codex produced no valid final source context")
            pred_files = list(dict.fromkeys(item["file"] for item in contexts))
            pred_spans: dict[str, list[dict[str, int]]] = {}
            for item in contexts:
                pred_spans.setdefault(item["file"], []).append(
                    {"type": "line", "start": item["start"], "end": item["end"]}
                )
            prediction = {
                "instance_id": instance_id,
                "traj_data": {
                    "pred_steps": steps,
                    "pred_files": pred_files,
                    "pred_spans": pred_spans,
                    "pred_symbols": {},
                },
                "model_patch": patch,
            }
            append_jsonl(predictions_path, prediction)
            audit = {
                "instance_id": instance_id,
                "policy": CODEX_POLICY,
                "final_context_policy": FINAL_CONTEXT_POLICY,
                "line_budget": args.line_budget,
                "projection": {
                    "variant": DEFAULT_PROJECTION_VARIANT,
                    "contexts": contexts,
                    "unique_lines": len(
                        {
                            (item["file"], line)
                            for item in contexts
                            for line in range(item["start"], item["end"] + 1)
                        }
                    ),
                },
                "cache": cache_audit,
                "codex": {
                    "binary": str(args.codex_binary),
                    "binary_sha256": sha256_file(args.codex_binary),
                    "model": args.agent_model,
                    "elapsed_seconds": getattr(codex, "elapsed_seconds", None),
                    "returncode": codex.returncode,
                    "usage": usage,
                    "mcp_calls": mcp_calls,
                    "attempts": codex_attempts,
                    "trajectory_steps": len(steps),
                    "profile": profile_audit,
                    "events": str(events_path),
                    "final": final,
                    "final_context_source": final_source,
                    "patch_bytes": len(patch.encode("utf-8")),
                },
                "end_to_end_seconds": round(time.monotonic() - started, 3),
            }
            audit_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(audit_dir / f"{slug}.json", audit)
        except Exception as error:
            append_jsonl(
                failures_path,
                {
                    "instance_id": instance_id,
                    "error": type(error).__name__,
                    "detail": str(error),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                },
            )
            print(f"ERROR {instance_id}: {error}", file=sys.stderr)
            if args.fail_fast:
                raise
        finally:
            if lock_handle is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
