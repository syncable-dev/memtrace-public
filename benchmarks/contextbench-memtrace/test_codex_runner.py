import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_runner


class CodexRunnerTests(unittest.TestCase):
    def make_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir(parents=True)
        (repo / "src").mkdir()
        (repo / "src" / "main.py").write_text(
            "\n".join(f"line_{index}" for index in range(1, 41)) + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.test",
                "commit",
                "-qm",
                "base",
            ],
            check=True,
        )
        return repo

    def test_final_contexts_are_repo_relative_deduplicated_and_budgeted(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self.make_repo(Path(temporary))
            selected = codex_runner.bounded_contexts(
                [
                    {"file": str(repo / "src/main.py"), "start": 2, "end": 8},
                    {"file": "src/main.py", "start": 7, "end": 12},
                    {"file": "../outside.py", "start": 1, "end": 3},
                    {"file": "src/main.py", "start": 30, "end": 40},
                ],
                repo,
                12,
            )
            self.assertEqual(
                selected,
                [
                    {"file": "src/main.py", "start": 2, "end": 8},
                    {"file": "src/main.py", "start": 7, "end": 12},
                ],
            )

    def test_final_context_accepts_path_with_line_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self.make_repo(Path(temporary))
            selected = codex_runner.bounded_contexts(
                [{"file": "src/main.py:14", "start": 14, "end": 18}], repo, 20
            )
            self.assertEqual(
                selected, [{"file": "src/main.py", "start": 14, "end": 18}]
            )

    def test_diff_context_uses_base_side_line_numbers(self):
        patch = (
            "diff --git a/src/main.py b/src/main.py\n"
            "--- a/src/main.py\n+++ b/src/main.py\n"
            "@@ -12,3 +12,4 @@\n"
            "diff --git a/src/other.py b/src/other.py\n"
            "@@ -8,0 +9,2 @@\n"
        )
        self.assertEqual(
            codex_runner.diff_contexts(patch),
            [
                {"file": "src/main.py", "start": 12, "end": 14},
                {"file": "src/other.py", "start": 8, "end": 8},
            ],
        )

    def test_jsonl_events_capture_memtrace_and_exact_shell_views(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self.make_repo(Path(temporary))
            events = [
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "memtrace",
                        "tool": "find_code",
                        "result": {
                            "results": [
                                {
                                    "file_path": "src/main.py",
                                    "symbol_start_line": 5,
                                    "symbol_end_line": 10,
                                }
                            ]
                        },
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "sed -n '20,25p' src/main.py",
                        "aggregated_output": "",
                    },
                },
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 100, "output_tokens": 25},
                },
            ]
            steps, calls, usage = codex_runner.trajectory_from_events(events, repo)
            self.assertEqual(calls, 1)
            self.assertEqual(len(steps), 2)
            self.assertEqual(steps[0]["spans"]["src/main.py"][0]["start"], 5)
            self.assertEqual(steps[1]["spans"]["src/main.py"][0]["start"], 20)
            self.assertEqual(usage["input_tokens"], 100)

    def test_codex_profile_contains_only_shipped_skills_and_required_mcp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skills = root / "bundle"
            for name in ("memtrace-first", "memtrace-search"):
                skill = skills / name
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: test\n---\n",
                    encoding="utf-8",
                )
            binary = root / "memtrace"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            index_repo = self.make_repo(root / "index")
            home = root / "codex-home"
            audit = codex_runner.configure_codex_home(
                home,
                skills,
                binary,
                {
                    "MEMTRACE_MEMDB_DATA_DIR": "/cache/memdb",
                    "MEMTRACE_RERANK": "on",
                    "OPENAI_API_KEY": "must-not-be-written",
                },
                index_repo,
                "gpt-5",
            )
            config = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn("required = true", config)
            self.assertIn('model_reasoning_effort = "medium"', config)
            self.assertIn('MEMTRACE_RERANK = "on"', config)
            self.assertNotIn("must-not-be-written", config)
            self.assertEqual(
                sorted(audit["skills"]), ["memtrace-first", "memtrace-search"]
            )
            self.assertEqual(
                sorted(path.name for path in (home / "skills").iterdir()),
                ["memtrace-first", "memtrace-search"],
            )

    def test_memtrace_environment_is_self_contained_for_benchmark_auth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = codex_runner.memtrace_environment(
                root / "repo",
                root / "cache",
                root / "reranker",
                root / "memtrace",
            )
            self.assertEqual(env["MEMTRACE_DEV"], "1")
            self.assertEqual(env["MEMTRACE_TELEMETRY"], "off")

    def test_memtrace_client_retries_transient_startup_failures(self):
        client = object()
        with mock.patch.object(
            codex_runner.retrieval,
            "McpClient",
            side_effect=[RuntimeError("first"), RuntimeError("second"), client],
        ) as constructor, mock.patch.object(codex_runner.time, "sleep") as sleep:
            opened = codex_runner.open_memtrace_client(
                Path("/repo"), {"MEMTRACE_DEV": "1"}
            )

        self.assertIs(opened, client)
        self.assertEqual(constructor.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_prompt_sets_task_agnostic_memtrace_discovery_budget(self):
        prompt = codex_runner.render_prompt(
            {"problem_statement": "Fix the broken parser."}, 200
        )
        self.assertIn("$memtrace-first", prompt)
        self.assertIn("no more than 24 Memtrace calls", prompt)
        self.assertIn("never append `:line`", prompt)
        self.assertIn("deterministic recall floor", prompt)
        self.assertIn("Include a test, configuration, fixture", prompt)
        self.assertNotIn("gold context", prompt.split("Task:", 1)[1])

    def test_projection_fills_unused_budget_from_repeated_repo_context(self):
        structured = [{"file": "src/main.py", "start": 20, "end": 24}]
        steps = [
            {
                "spans": {
                    "src/main.py": [{"start": 1, "end": 80, "type": "line"}],
                    "src/related.py": [{"start": 40, "end": 70, "type": "line"}],
                    "tests/test_main.py": [{"start": 1, "end": 80, "type": "line"}],
                }
            },
            {
                "spans": {
                    "src/related.py": [{"start": 45, "end": 65, "type": "line"}],
                    "tests/test_main.py": [
                        {"start": 20, "end": 45, "type": "line"}
                    ],
                }
            },
        ]
        selected = codex_runner.project_hierarchical_recall(
            structured, steps, "", 200, "balanced"
        )
        self.assertEqual(selected[0], structured[0])
        self.assertTrue(any(item["file"] == "src/related.py" for item in selected))
        self.assertTrue(any(item["file"].startswith("tests/") for item in selected))
        lines = {
            (item["file"], line)
            for item in selected
            for line in range(item["start"], item["end"] + 1)
        }
        self.assertLessEqual(len(lines), 200)

    def test_projection_allows_authored_documentation_but_not_vendored_source(self):
        self.assertTrue(codex_runner.projection_path_allowed("docs/upgrade.rst"))
        self.assertTrue(codex_runner.projection_path_allowed("tests/test_main.py"))
        self.assertFalse(codex_runner.projection_path_allowed("vendor/lib/main.py"))

    def test_legacy_cache_identity_requires_exact_repo_commit_and_namespace(self):
        manifest = {
            "repo_url": "https://example.test/repo.git",
            "base_commit": "abc",
            "namespace": "graph-v1",
        }
        self.assertTrue(
            codex_runner.legacy_cache_matches(
                manifest, "https://example.test/repo.git", "abc", "graph-v1"
            )
        )
        self.assertFalse(
            codex_runner.legacy_cache_matches(
                manifest, "https://example.test/repo.git", "def", "graph-v1"
            )
        )


if __name__ == "__main__":
    unittest.main()
