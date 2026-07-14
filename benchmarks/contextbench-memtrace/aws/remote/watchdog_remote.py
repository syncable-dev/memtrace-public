#!/usr/bin/env python3
"""Fail-closed per-instance watchdog for ContextBench runner processes.

The previous shell watchdog trusted one ``ps etimes`` sample.  A transient
invalid sample can therefore kill a brand-new runner.  This implementation
derives elapsed time from Linux ``/proc`` monotonic start ticks, rejects
impossible values, and requires the same PID/start-tick identity to be over
the limit in two consecutive scans before killing its process tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ProcessSample:
    pid: int
    ppid: int
    start_ticks: int
    elapsed_seconds: float
    slug: str

    @property
    def identity(self) -> tuple[int, int]:
        return (self.pid, self.start_ticks)


def parse_proc_stat(value: str) -> tuple[int, int, int]:
    """Return ``(pid, ppid, start_ticks)`` from one Linux proc stat row."""
    opening = value.find("(")
    closing = value.rfind(")")
    if opening <= 0 or closing <= opening:
        raise ValueError("malformed proc stat command field")
    pid = int(value[:opening].strip())
    tail = value[closing + 1 :].split()
    # tail[0] is field 3 (state), tail[1] is field 4 (ppid), and tail[19]
    # is field 22 (starttime). Parsing after the final ')' handles spaces in
    # the kernel command name safely.
    if len(tail) < 20:
        raise ValueError("proc stat row is missing starttime")
    return pid, int(tail[1]), int(tail[19])


def read_cmdline(path: Path) -> list[str]:
    raw = path.read_bytes()
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def runner_slug(argv: list[str], results: Path) -> str | None:
    if not any(Path(argument).name == "runner.py" for argument in argv):
        return None
    try:
        work_dir = Path(argv[argv.index("--work-dir") + 1])
        relative = work_dir.relative_to(results / "runs")
    except (ValueError, IndexError):
        return None
    if len(relative.parts) != 2 or relative.parts[1] != "work":
        return None
    return relative.parts[0]


def read_uptime(proc_root: Path) -> float:
    value = (proc_root / "uptime").read_text(encoding="utf-8").split()[0]
    return float(value)


def scan_runners(
    proc_root: Path,
    results: Path,
    clock_ticks: int,
) -> tuple[list[ProcessSample], list[str]]:
    uptime = read_uptime(proc_root)
    samples: list[ProcessSample] = []
    warnings: list[str] = []
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit():
            continue
        try:
            pid, ppid, start_ticks = parse_proc_stat(
                (process_dir / "stat").read_text(encoding="utf-8")
            )
            slug = runner_slug(read_cmdline(process_dir / "cmdline"), results)
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, OSError):
            continue
        if slug is None:
            continue
        elapsed = uptime - (start_ticks / clock_ticks)
        # A process cannot predate boot or start materially in the future.
        # Rejecting the sample is safer than converting an observation error
        # into a benchmark failure.
        if start_ticks < 0 or elapsed < -1.0 or elapsed > uptime + 1.0:
            warnings.append(
                f"outcome=invalid_elapsed slug={slug} pid={pid} "
                f"elapsed_s={elapsed:.3f} uptime_s={uptime:.3f} "
                f"start_ticks={start_ticks}"
            )
            continue
        samples.append(ProcessSample(pid, ppid, start_ticks, elapsed, slug))
    return samples, warnings


class OverLimitTracker:
    """Require the same process identity to exceed the limit twice."""

    def __init__(self) -> None:
        self._pending: set[tuple[int, int]] = set()

    def update(
        self,
        samples: Iterable[ProcessSample],
        limit_seconds: float,
    ) -> list[ProcessSample]:
        over = {
            sample.identity: sample
            for sample in samples
            if sample.elapsed_seconds >= limit_seconds
        }
        ready = [sample for identity, sample in over.items() if identity in self._pending]
        self._pending = set(over) - {sample.identity for sample in ready}
        return ready


def read_process_identity(proc_root: Path, pid: int) -> tuple[int, int]:
    observed_pid, _, start_ticks = parse_proc_stat(
        (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    )
    return observed_pid, start_ticks


def process_tree(
    proc_root: Path,
    root_identity: tuple[int, int],
) -> list[tuple[int, int]]:
    children: dict[int, list[tuple[int, int]]] = {}
    identities: dict[int, tuple[int, int]] = {}
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit():
            continue
        try:
            pid, ppid, start_ticks = parse_proc_stat(
                (process_dir / "stat").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, OSError):
            continue
        identity = (pid, start_ticks)
        identities[pid] = identity
        children.setdefault(ppid, []).append(identity)

    if identities.get(root_identity[0]) != root_identity:
        return []

    ordered: list[tuple[int, int]] = []

    def visit(identity: tuple[int, int]) -> None:
        ordered.append(identity)
        for child in sorted(children.get(identity[0], [])):
            visit(child)

    visit(root_identity)
    return ordered


def append_log(path: Path, message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with path.open("a", encoding="utf-8") as output:
        output.write(f"{stamp} {message}\n")
        output.flush()
        os.fsync(output.fileno())


def open_verified_pidfd(
    proc_root: Path,
    identity: tuple[int, int],
) -> int:
    """Pin one exact process identity or fail without exposing a bare PID."""
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        raise RuntimeError("pidfd APIs are unavailable")
    descriptor = pidfd_open(identity[0], 0)
    try:
        if read_process_identity(proc_root, identity[0]) != identity:
            raise ProcessLookupError("process identity changed before pidfd verification")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def kill_sample(
    sample: ProcessSample,
    proc_root: Path,
    results: Path,
    log: Path,
    limit: int,
) -> bool:
    identities = process_tree(proc_root, sample.identity)
    if not identities or identities[0] != sample.identity:
        append_log(
            log,
            f"outcome=stale_identity slug={sample.slug} pid={sample.pid} "
            f"expected_start_ticks={sample.start_ticks} stage=tree",
        )
        return False

    handles: list[tuple[tuple[int, int], int]] = []
    try:
        # Pin and verify the root before touching any descendant. If the PID
        # was reused, abort the whole timeout action and never create a marker.
        try:
            root_descriptor = open_verified_pidfd(proc_root, sample.identity)
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError) as error:
            append_log(
                log,
                f"outcome=stale_identity slug={sample.slug} pid={sample.pid} "
                f"expected_start_ticks={sample.start_ticks} stage=root_pidfd "
                f"error={type(error).__name__}",
            )
            return False
        handles.append((sample.identity, root_descriptor))

        # A stale descendant is skipped. It can no longer be targeted by PID;
        # every signal below goes through a verified pidfd.
        for identity in identities[1:]:
            try:
                handles.append((identity, open_verified_pidfd(proc_root, identity)))
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError) as error:
                append_log(
                    log,
                    f"outcome=stale_identity slug={sample.slug} pid={identity[0]} "
                    f"expected_start_ticks={identity[1]} stage=descendant_pidfd "
                    f"error={type(error).__name__}",
                )

        signaled: list[tuple[int, int]] = []
        root_signaled = False
        send_signal = getattr(signal, "pidfd_send_signal", None)
        if send_signal is None:
            raise RuntimeError("pidfd_send_signal is unavailable")
        # Descendants first prevents a surviving child from escaping when its
        # root is killed. The root is always the first handle and thus last.
        for identity, descriptor in reversed(handles):
            try:
                send_signal(descriptor, signal.SIGKILL, None, 0)
            except (ProcessLookupError, PermissionError):
                continue
            signaled.append(identity)
            if identity == sample.identity:
                root_signaled = True

        if not root_signaled:
            append_log(
                log,
                f"outcome=stale_identity slug={sample.slug} pid={sample.pid} "
                f"expected_start_ticks={sample.start_ticks} stage=root_signal",
            )
            return False

        killed = " ".join(f"{pid}:{start}" for pid, start in signaled)
        append_log(
            log,
            f"outcome=timeout slug={sample.slug} pid={sample.pid} "
            f"elapsed_s={int(sample.elapsed_seconds)} limit_s={limit} "
            f"start_ticks={sample.start_ticks} observations=2 "
            f"killed_identities='{killed}'",
        )
        timeout_path = results / "runs" / sample.slug / "WATCHDOG_TIMEOUT"
        timeout_path.write_text(
            f"watchdog: killed after {int(sample.elapsed_seconds)}s "
            f"(limit {limit}s) at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n",
            encoding="utf-8",
        )
    finally:
        for _, descriptor in handles:
            try:
                os.close(descriptor)
            except OSError:
                pass
    print(
        f"[watchdog] killed {sample.slug} pid={sample.pid} after "
        f"{int(sample.elapsed_seconds)}s (limit {limit}s) — driver continues",
        flush=True,
    )
    return True


def owner_alive(
    proc_root: Path,
    owner_pid: int,
    owner_start_ticks: int,
    exit_marker: Path,
) -> bool:
    if exit_marker.exists():
        return False
    try:
        return read_process_identity(proc_root, owner_pid) == (
            owner_pid,
            owner_start_ticks,
        )
    except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, OSError):
        return False


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def terminal_record_count(results: Path) -> int:
    return sum(1 for path in (results / "runs").glob("*/run_record.json") if path.is_file())


def write_session_provenance(args: argparse.Namespace, log: Path) -> None:
    activated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    watchdog_pid, watchdog_start_ticks = read_process_identity(
        args.proc_root,
        os.getpid(),
    )
    provenance = {
        "schema_version": 1,
        "session_id": args.session_id,
        "activated_at": activated_at,
        "watchdog_sha256": file_sha256(Path(__file__).resolve()),
        "run_id": args.results.name,
        "results": str(args.results),
        "owner_pid": args.owner_pid,
        "owner_start_ticks": args.owner_start_ticks,
        "watchdog_pid": watchdog_pid,
        "watchdog_start_ticks": watchdog_start_ticks,
        "limit_seconds": args.limit_seconds,
        "interval_seconds": args.interval_seconds,
        "exit_marker": str(args.exit_marker),
        "terminal_records_at_activation": terminal_record_count(args.results),
    }
    args.session_file.parent.mkdir(parents=True, exist_ok=True)
    with args.session_file.open("x", encoding="utf-8") as output:
        json.dump(provenance, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    append_log(
        log,
        f"outcome=watchdog_started session_id={args.session_id} "
        f"watchdog_sha256={provenance['watchdog_sha256']} "
        f"owner_pid={args.owner_pid} owner_start_ticks={args.owner_start_ticks} "
        f"terminal_records={provenance['terminal_records_at_activation']}",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--limit-seconds", type=int, required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--owner-start-ticks", type=int, required=True)
    parser.add_argument("--exit-marker", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-file", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    return parser.parse_args()


def run_watchdog(args: argparse.Namespace, log: Path, clock_ticks: int) -> None:
    tracker = OverLimitTracker()
    while owner_alive(
        args.proc_root,
        args.owner_pid,
        args.owner_start_ticks,
        args.exit_marker,
    ):
        time.sleep(args.interval_seconds)
        # The owner can exit while this process sleeps. Never scan or signal
        # based on a session whose exact owner identity is already gone.
        if not owner_alive(
            args.proc_root,
            args.owner_pid,
            args.owner_start_ticks,
            args.exit_marker,
        ):
            break
        try:
            samples, warnings = scan_runners(
                args.proc_root,
                args.results,
                clock_ticks,
            )
            for warning in warnings:
                append_log(log, warning)
            for sample in tracker.update(samples, args.limit_seconds):
                kill_sample(sample, args.proc_root, args.results, log, args.limit_seconds)
        except Exception as error:  # A diagnostic guard must not kill the benchmark.
            append_log(log, f"outcome=watchdog_error error={type(error).__name__}:{error}")


def main() -> int:
    args = parse_args()
    if args.limit_seconds <= 0 or args.interval_seconds <= 0:
        raise SystemExit("watchdog limits and interval must be positive")
    args.results = args.results.resolve()
    args.exit_marker = args.exit_marker.resolve()
    args.session_file = args.session_file.resolve()
    log = args.results / "watchdog.log"
    clock_ticks = int(os.sysconf("SC_CLK_TCK"))
    # Fail at startup instead of silently running an unarmed destructive
    # guard on a platform without the exact-process pidfd primitives.
    self_identity = read_process_identity(args.proc_root, os.getpid())
    self_pidfd = open_verified_pidfd(args.proc_root, self_identity)
    os.close(self_pidfd)
    write_session_provenance(args, log)
    run_watchdog(args, log, clock_ticks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
