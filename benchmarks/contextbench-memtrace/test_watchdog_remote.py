import importlib.util
import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parent / "aws" / "remote" / "watchdog_remote.py"
SPEC = importlib.util.spec_from_file_location("watchdog_remote", MODULE_PATH)
watchdog = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = watchdog
SPEC.loader.exec_module(watchdog)


def proc_stat(pid: int, ppid: int, start_ticks: int, command: str = "python runner") -> str:
    prefix = ["S", str(ppid)]
    padding = ["0"] * 17
    return f"{pid} ({command}) " + " ".join(prefix + padding + [str(start_ticks)]) + "\n"


class WatchdogRemoteTests(unittest.TestCase):
    def test_parse_proc_stat_handles_spaces_in_command(self):
        self.assertEqual(
            watchdog.parse_proc_stat(proc_stat(12, 7, 900, "python worker")),
            (12, 7, 900),
        )

    def test_tracker_requires_two_identical_over_limit_observations(self):
        tracker = watchdog.OverLimitTracker()
        sample = watchdog.ProcessSample(12, 7, 900, 91.0, "task")
        self.assertEqual(tracker.update([sample], 90.0), [])
        self.assertEqual(tracker.update([sample], 90.0), [sample])
        replacement = watchdog.ProcessSample(12, 7, 901, 91.0, "task")
        self.assertEqual(tracker.update([replacement], 90.0), [])

    def test_scan_uses_monotonic_start_ticks_and_rejects_impossible_elapsed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc"
            results = root / "results"
            (results / "runs" / "good" / "work").mkdir(parents=True)
            (results / "runs" / "bad" / "work").mkdir(parents=True)
            proc.mkdir()
            (proc / "uptime").write_text("100.00 0.00\n", encoding="utf-8")
            for pid, slug, start in ((12, "good", 9000), (13, "bad", -100)):
                process = proc / str(pid)
                process.mkdir()
                (process / "stat").write_text(proc_stat(pid, 1, start), encoding="utf-8")
                argv = [
                    "/venv/python",
                    "/adapter/runner.py",
                    "--work-dir",
                    str(results / "runs" / slug / "work"),
                ]
                (process / "cmdline").write_bytes(b"\0".join(v.encode() for v in argv) + b"\0")

            samples, warnings = watchdog.scan_runners(proc, results, clock_ticks=100)
            self.assertEqual([(item.pid, item.slug, item.elapsed_seconds) for item in samples], [(12, "good", 10.0)])
            self.assertEqual(len(warnings), 1)
            self.assertIn("outcome=invalid_elapsed slug=bad pid=13", warnings[0])

    def test_process_tree_is_root_first_and_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / "uptime").write_text("100.0 0.0\n", encoding="utf-8")
            for pid, ppid in ((10, 1), (11, 10), (12, 10), (13, 11), (20, 1)):
                process = proc / str(pid)
                process.mkdir()
                (process / "stat").write_text(proc_stat(pid, ppid, 100), encoding="utf-8")
            self.assertEqual(
                watchdog.process_tree(proc, (10, 100)),
                [(10, 100), (11, 100), (13, 100), (12, 100)],
            )

    def test_stale_root_identity_never_signals_or_writes_timeout_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc"
            results = root / "results"
            log = results / "watchdog.log"
            (results / "runs" / "task").mkdir(parents=True)
            process = proc / "10"
            process.mkdir(parents=True)
            (process / "stat").write_text(proc_stat(10, 1, 200), encoding="utf-8")
            sample = watchdog.ProcessSample(10, 1, 100, 100.0, "task")

            with mock.patch.object(
                watchdog.signal,
                "pidfd_send_signal",
                create=True,
            ) as send_signal:
                self.assertFalse(
                    watchdog.kill_sample(sample, proc, results, log, limit=90)
                )

            send_signal.assert_not_called()
            self.assertFalse((results / "runs" / "task" / "WATCHDOG_TIMEOUT").exists())
            self.assertIn("outcome=stale_identity", log.read_text(encoding="utf-8"))
            self.assertNotIn("outcome=timeout", log.read_text(encoding="utf-8"))

    def test_pidfds_skip_stale_descendant_and_signal_verified_tree_descendant_first(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc"
            results = root / "results"
            log = results / "watchdog.log"
            (results / "runs" / "task").mkdir(parents=True)
            for pid, ppid, start in (
                (10, 1, 100),
                (11, 10, 110),
                (12, 10, 120),
                (13, 11, 130),
            ):
                process = proc / str(pid)
                process.mkdir(parents=True)
                (process / "stat").write_text(
                    proc_stat(pid, ppid, start), encoding="utf-8"
                )
            sample = watchdog.ProcessSample(10, 1, 100, 100.0, "task")

            def open_pidfd(_proc_root, identity):
                if identity == (12, 120):
                    raise ProcessLookupError("reused")
                return identity[0] + 1000

            with (
                mock.patch.object(watchdog, "open_verified_pidfd", side_effect=open_pidfd),
                mock.patch.object(
                    watchdog.signal,
                    "pidfd_send_signal",
                    create=True,
                ) as send_signal,
                mock.patch.object(watchdog.os, "close"),
            ):
                self.assertTrue(
                    watchdog.kill_sample(sample, proc, results, log, limit=90)
                )

            self.assertEqual(
                [call.args[0] for call in send_signal.call_args_list],
                [1013, 1011, 1010],
            )
            self.assertTrue((results / "runs" / "task" / "WATCHDOG_TIMEOUT").is_file())
            contents = log.read_text(encoding="utf-8")
            self.assertIn("stage=descendant_pidfd", contents)
            self.assertIn("outcome=timeout", contents)
            self.assertIn("13:130 11:110 10:100", contents)

    def test_owner_identity_ignores_shared_marker_and_honors_session_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc"
            results = root / "results"
            exit_marker = results / "watchdog-sessions" / "session" / "driver_exit"
            process = proc / "42"
            process.mkdir(parents=True)
            results.mkdir()
            (process / "stat").write_text(proc_stat(42, 1, 500), encoding="utf-8")
            (results / "driver_exit").write_text("0\n", encoding="utf-8")

            self.assertTrue(watchdog.owner_alive(proc, 42, 500, exit_marker))
            self.assertFalse(watchdog.owner_alive(proc, 42, 501, exit_marker))
            exit_marker.parent.mkdir(parents=True)
            exit_marker.write_text("0\n", encoding="utf-8")
            self.assertFalse(watchdog.owner_alive(proc, 42, 500, exit_marker))

    def test_owner_death_during_sleep_prevents_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc"
            results = root / "results"
            log = results / "watchdog.log"
            exit_marker = results / "session" / "driver_exit"
            process = proc / "42"
            process.mkdir(parents=True)
            results.mkdir()
            (process / "stat").write_text(proc_stat(42, 1, 500), encoding="utf-8")
            args = argparse.Namespace(
                proc_root=proc,
                results=results,
                owner_pid=42,
                owner_start_ticks=500,
                exit_marker=exit_marker,
                interval_seconds=1.0,
                limit_seconds=90,
            )

            def owner_exits(_seconds):
                (process / "stat").unlink()
                process.rmdir()

            with (
                mock.patch.object(watchdog.time, "sleep", side_effect=owner_exits),
                mock.patch.object(watchdog, "scan_runners") as scan_runners,
            ):
                watchdog.run_watchdog(args, log, clock_ticks=100)

            scan_runners.assert_not_called()

    def test_runner_slug_rejects_work_outside_results(self):
        results = Path("/results")
        self.assertEqual(
            watchdog.runner_slug(
                ["python", "/adapter/runner.py", "--work-dir", "/results/runs/task/work"],
                results,
            ),
            "task",
        )
        self.assertIsNone(
            watchdog.runner_slug(
                ["python", "/adapter/runner.py", "--work-dir", "/other/runs/task/work"],
                results,
            )
        )


if __name__ == "__main__":
    unittest.main()
