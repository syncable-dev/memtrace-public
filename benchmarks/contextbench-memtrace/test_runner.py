import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("runner.py")
SPEC = importlib.util.spec_from_file_location("contextbench_memtrace_runner", MODULE_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    @staticmethod
    def _selector_candidates(count=150):
        return [
            runner.Candidate(
                f"src/file_{index}.py",
                index + 1,
                index + 1,
                "target_handler" if index == 0 else f"candidate_{index}",
                "Function",
                "target handling" if index == 0 else f"unrelated noise {index}",
                index,
            )
            for index in range(count)
        ]

    @staticmethod
    def _selector_response(status, response_id=None, candidate_ids=None, reason=None):
        response = {
            "id": response_id,
            "status": status,
            "output": [],
            "usage": {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 40},
                "output_tokens": 20,
                "output_tokens_details": {"reasoning_tokens": 7},
                "total_tokens": 120,
            },
        }
        if reason:
            response["incomplete_details"] = {"reason": reason}
        if candidate_ids is not None:
            response["output"] = [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "candidate_ids": candidate_ids,
                                    "omission_justification": "",
                                }
                            ),
                        }
                    ],
                }
            ]
        return response

    def test_test_path_detection(self):
        self.assertTrue(runner.is_test_path("tests/test_proxy.py"))
        self.assertTrue(runner.is_test_path("src/test_utils.py"))
        self.assertFalse(runner.is_test_path("requests/utils.py"))

    def test_identifier_queries_preserve_exact_code_names(self):
        text = (
            "Proxy auth uses utils.get_auth_from_url and HTTPAdapter.proxy_headers "
            "with ProxyManager but ordinary words are ignored"
        )
        self.assertEqual(
            runner.identifier_queries(text),
            ["utils.get_auth_from_url", "HTTPAdapter.proxy_headers", "ProxyManager"],
        )

    def test_resolve_source_root_relative_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "src" / "flask" / "blueprints.py"
            target.parent.mkdir(parents=True)
            target.touch()
            self.assertEqual(
                runner.resolve_repo_path(root, "flask/blueprints.py"),
                "src/flask/blueprints.py",
            )

    def test_hydrates_empty_search_content_from_exact_span(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "utils.py"
            source.write_text("one\ntwo\nthree\n", encoding="utf-8")
            candidate = runner.Candidate("utils.py", 2, 3, "target", "Function")
            self.assertEqual(
                runner.hydrate_candidate_content([candidate], root)[0].content,
                "two\nthree",
            )

    def test_oversized_symbol_keeps_query_relevant_bounded_window(self):
        lines = [f"line {number}" for number in range(1, 121)]
        lines[91] = "upstreamURL uses HTTPS git protocol"
        candidate = runner.Candidate(
            "pkg/cmd/repo/clone/clone.go",
            77,
            196,
            "cloneRun",
            "Function",
            "\n".join(lines),
        )
        compacted = runner.compact_oversized_candidates(
            [candidate], "use HTTPS for upstream remote", 80
        )
        self.assertTrue(all(item.line_count <= 40 for item in compacted))
        self.assertTrue(any("upstreamURL" in item.content for item in compacted))

    def test_refinement_files_prefer_cross_lane_production_consensus(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("src/core.py", "src/noise.py", "tests/test_core.py"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            searches = [
                {
                    "results": [
                        {"file_path": str(root / "src/core.py")},
                        {"file_path": str(root / "src/noise.py")},
                        {"file_path": str(root / "tests/test_core.py")},
                    ]
                },
                {"results": [{"file_path": str(root / "src/core.py")}]},
            ]
            self.assertEqual(runner.rank_refinement_files(searches, root), ["src/core.py", "src/noise.py"])

    def test_file_refinement_scopes_each_query(self):
        class Client:
            def __init__(self):
                self.calls = []

            def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                return {"results": []}

        client = Client()
        refined = runner.refine_within_files(client, "repo", ["symptom", "flow"], ["src/core.py"])
        self.assertEqual(len(refined), 2)
        self.assertEqual(
            [call[1]["file_path"] for call in client.calls],
            ["src/core.py", "src/core.py"],
        )
        self.assertTrue(all(call[1]["limit"] == runner.DEFAULT_FILE_REFINEMENT_LIMIT for call in client.calls))

    def test_file_refinement_propagates_live_window_budget(self):
        class Client:
            def __init__(self):
                self.calls = []

            def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                return {"results": []}

        client = Client()
        runner.refine_within_files(
            client,
            "repo",
            ["mincnt"],
            ["axes.py"],
            view="live",
            line_budget=40,
        )
        self.assertEqual(client.calls[0][1]["view"], "live")
        self.assertEqual(client.calls[0][1]["line_budget"], 40)

    def test_constructor_cue_promotes_init(self):
        query = "Require a non-empty name when given a Blueprint"
        init = runner.Candidate(
            "src/flask/blueprints.py", 172, 206, "__init__", "Function", "self.name = name", 5
        )
        helper = runner.Candidate(
            "src/flask/helpers.py", 677, 684, "_split_blueprint_path", "Function", "blueprint path", 0
        )
        self.assertGreater(runner.candidate_score(init, query), runner.candidate_score(helper, query))

    def test_line_budget_keeps_best_candidate(self):
        query = "Blueprint empty name constructor"
        best = runner.Candidate("src/flask/blueprints.py", 172, 206, "__init__", "Function", "name", 1)
        noise = runner.Candidate("src/flask/app.py", 1, 100, "other", "Function", "", 2)
        self.assertEqual(runner.pack_final_context([noise, best], query, 40), [best])

    def test_selector_pool_preserves_query_lane_candidates(self):
        priority = runner.Candidate("utils.py", 10, 20, "normalize_input", "Function")
        lexical = runner.Candidate("api.py", 1, 5, "proxy_auth", "Function")
        self.assertEqual(
            runner.build_selector_pool([lexical, priority], "proxy auth", [priority]),
            [priority, lexical],
        )

    def test_candidates_from_rows_keeps_exact_symbol_beside_widened_hits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sympy/matrices/common.py"
            source.parent.mkdir(parents=True)
            source.write_text("\n" * 300, encoding="utf-8")
            rows = [
                {
                    "id": "symbol-col-insert",
                    "file_path": str(source),
                    "start_line": 76,
                    "end_line": 273,
                    "symbol_start_line": 182,
                    "symbol_end_line": 218,
                    "name": "col_insert",
                    "scope_path": "MatrixShaping::col_insert",
                    "kind": "Function",
                },
                {
                    "id": "symbol-eval-col-insert",
                    "file_path": str(source),
                    "start_line": 76,
                    "end_line": 273,
                    "symbol_start_line": 81,
                    "symbol_end_line": 92,
                    "name": "_eval_col_insert",
                    "scope_path": "MatrixShaping::_eval_col_insert",
                    "kind": "Function",
                },
                {
                    "id": "symbol-eval-col-insert",
                    "file_path": str(source),
                    "start_line": 76,
                    "end_line": 273,
                    "symbol_start_line": 81,
                    "symbol_end_line": 92,
                    "name": "_eval_col_insert",
                    "scope_path": "MatrixShaping::_eval_col_insert",
                    "kind": "Function",
                },
            ]

            candidates = runner.candidates_from_rows(rows, root)

        self.assertEqual(
            [(item.start, item.end) for item in candidates],
            [(182, 218), (76, 273), (81, 92), (76, 273), (76, 273)],
        )
        self.assertEqual(
            sum(
                item.name == "MatrixShaping::_eval_col_insert"
                and (item.start, item.end) == (81, 92)
                for item in candidates
            ),
            1,
        )
        deduped = runner.dedupe_candidates(candidates)
        self.assertIn(
            ("MatrixShaping::_eval_col_insert", 81, 92),
            {(item.name, item.start, item.end) for item in deduped},
        )
        widened = [
            item for item in deduped
            if (item.start, item.end) == (76, 273)
        ]
        self.assertEqual(
            {item.symbol_id for item in widened},
            {"symbol-col-insert", "symbol-eval-col-insert"},
        )
        compacted = runner.compact_oversized_candidates(
            runner.hydrate_candidate_content(deduped, root),
            "col insert",
            80,
        )
        exact_eval = [
            item for item in compacted
            if item.symbol_id == "symbol-eval-col-insert"
            and item.is_exact_symbol
        ]
        self.assertEqual(
            [(item.start, item.end) for item in exact_eval],
            [(81, 92)],
        )

    def test_selector_pool_caps_duplicate_gtest_symbol_windows(self):
        duplicate_windows = [
            runner.Candidate(
                "lib/gtest/gtest/internal/gtest-tuple.h",
                200 + index,
                239 + index,
                "tuple",
                "Function",
                "tuple interface compatibility",
                index,
            )
            for index in range(60)
        ]
        alternatives = [
            runner.Candidate(
                f"src/type/alternative_{index}.c",
                1,
                20,
                f"match_alternative_{index}",
                "Function",
                "structural matching",
                60 + index,
            )
            for index in range(180)
        ]

        pool = runner.build_selector_pool(
            [*duplicate_windows, *alternatives],
            "tuple interface compatibility",
            pool_size=runner.GUARDED_SELECTOR_CANDIDATES,
        )

        duplicate_count = sum(
            item.file == "lib/gtest/gtest/internal/gtest-tuple.h"
            and item.name == "tuple"
            for item in pool
        )
        self.assertEqual(len(pool), runner.GUARDED_SELECTOR_CANDIDATES)
        self.assertEqual(
            duplicate_count,
            runner.SELECTOR_POOL_SYMBOL_VARIANT_CAP,
        )
        self.assertTrue(any(item.file == "src/type/alternative_147.c" for item in pool))

    def test_selector_pool_soft_caps_one_file_when_alternatives_exist(self):
        dominant_file = [
            runner.Candidate(
                "src/type/matchtype.c",
                index * 3 + 1,
                index * 3 + 2,
                f"is_match_case_{index}",
                "Function",
                "tuple interface compatibility",
                index,
            )
            for index in range(80)
        ]
        alternatives = [
            runner.Candidate(
                f"src/other/case_{index}.c",
                1,
                2,
                f"other_case_{index}",
                "Function",
                "type matching",
                80 + index,
            )
            for index in range(180)
        ]

        pool = runner.build_selector_pool(
            [*dominant_file, *alternatives],
            "tuple interface compatibility",
            pool_size=runner.GUARDED_SELECTOR_CANDIDATES,
        )
        expected_file_cap = max(
            runner.REFINEMENT_FILE_PRIORITY_CAP,
            (
                runner.GUARDED_SELECTOR_CANDIDATES
                + runner.SELECTOR_POOL_FILE_SHARE_DENOMINATOR
                - 1
            )
            // runner.SELECTOR_POOL_FILE_SHARE_DENOMINATOR,
        )

        self.assertEqual(len(pool), runner.GUARDED_SELECTOR_CANDIDATES)
        self.assertEqual(
            sum(item.file == "src/type/matchtype.c" for item in pool),
            expected_file_cap,
        )

    def test_selector_pool_keeps_distinct_overloads_with_stable_symbol_ids(self):
        overloads = [
            runner.Candidate(
                "src/visitor.cc",
                index * 10 + 1,
                index * 10 + 8,
                "Visitor::visit",
                "Method",
                "visit node",
                index,
                f"memdb:overload-{index}",
                True,
            )
            for index in range(3)
        ]

        pool = runner.build_selector_pool(
            overloads,
            "visit node",
            pool_size=3,
        )

        self.assertEqual(pool, overloads)
        self.assertEqual({item.symbol_id for item in pool}, {
            "memdb:overload-0",
            "memdb:overload-1",
            "memdb:overload-2",
        })

    def test_selector_pool_reserves_exact_span_ahead_of_stronger_windows(self):
        exact = runner.Candidate(
            "src/matrix.py",
            81,
            92,
            "MatrixShaping::_eval_col_insert",
            "Function",
            "implementation",
            50,
            "memdb:eval-col-insert",
            True,
        )
        windows = [
            runner.Candidate(
                "src/matrix.py",
                start,
                start + 39,
                "MatrixShaping::_eval_col_insert",
                "Function",
                "tuple interface compatibility tuple interface compatibility",
                index,
                "memdb:eval-col-insert",
                False,
            )
            for index, start in enumerate((1, 41))
        ]

        pool = runner.build_selector_pool(
            [*windows, exact],
            "tuple interface compatibility",
            pool_size=runner.SELECTOR_POOL_SYMBOL_VARIANT_CAP,
        )

        self.assertIn(exact, pool)
        self.assertEqual(len(pool), runner.SELECTOR_POOL_SYMBOL_VARIANT_CAP)

    def test_selector_pool_normalizes_twelve_priority_windows_per_symbol(self):
        symbol_id = "memdb:priority-flood"
        exact = runner.Candidate(
            "src/matrix.py",
            81,
            92,
            "MatrixShaping::_eval_col_insert",
            "Function",
            "implementation",
            99,
            symbol_id,
            True,
        )
        windows = [
            runner.Candidate(
                "src/matrix.py",
                index * 40 + 1,
                index * 40 + 40,
                "MatrixShaping::_eval_col_insert",
                "Function",
                "priority tuple compatibility",
                index,
                symbol_id,
                False,
            )
            for index in range(11)
        ]
        priority = [*windows, exact]
        later_same_symbol = runner.Candidate(
            "src/matrix.py",
            501,
            540,
            "MatrixShaping::_eval_col_insert",
            "Function",
            "priority tuple compatibility " * 4,
            0,
            symbol_id,
            False,
        )
        alternatives = [
            runner.Candidate(
                f"src/other_{index}.py",
                1,
                20,
                f"other_{index}",
                "Function",
                "priority tuple compatibility",
                index + 20,
                f"memdb:other-{index}",
                True,
            )
            for index in range(4)
        ]

        pool = runner.build_selector_pool(
            [*priority, later_same_symbol, *alternatives],
            "priority tuple compatibility",
            priority_candidates=priority,
            pool_size=6,
        )

        symbol_candidates = [item for item in pool if item.symbol_id == symbol_id]
        self.assertEqual(len(priority), 12)
        self.assertEqual(symbol_candidates, [exact, windows[0]])
        self.assertNotIn(later_same_symbol, pool)
        self.assertTrue(all(candidate in pool for candidate in alternatives))

    def test_selector_pool_hard_preserves_priority_past_soft_file_cap(self):
        priority = [
            runner.Candidate(
                "src/type/matchtype.c",
                index * 3 + 1,
                index * 3 + 2,
                f"priority_case_{index}",
                "Function",
                "priority dispatch",
                index,
                f"memdb:priority-{index}",
                True,
            )
            for index in range(31)
        ]
        alternatives = [
            runner.Candidate(
                f"src/other/case_{index}.c",
                1,
                2,
                f"alternative_{index}",
                "Function",
                "priority dispatch",
                31 + index,
                f"memdb:alternative-{index}",
                True,
            )
            for index in range(180)
        ]

        pool = runner.build_selector_pool(
            [*priority, *alternatives],
            "priority dispatch",
            priority_candidates=priority,
            pool_size=runner.GUARDED_SELECTOR_CANDIDATES,
        )

        self.assertEqual(len(pool), runner.GUARDED_SELECTOR_CANDIDATES)
        self.assertTrue(all(candidate in pool for candidate in priority))
        self.assertIn(priority[30], pool)

    def test_oversized_class_does_not_displace_method(self):
        query = "Require a non-empty name when given a Blueprint"
        blueprint = runner.Candidate(
            "src/flask/blueprints.py", 117, 621, "Blueprint", "Class", "name", 0
        )
        init = runner.Candidate(
            "src/flask/blueprints.py", 172, 206, "__init__", "Function", "self.name = name", 1
        )
        self.assertEqual(runner.pack_final_context([blueprint, init], query, 80), [init])

    def test_dedupe_preserves_scoped_name_and_graph_content(self):
        search = runner.Candidate(
            "requests/models.py", 388, 395, "PreparedRequest::prepare_content_length", "Function", "", 0
        )
        graph = runner.Candidate(
            "requests/models.py", 388, 395, "prepare_content_length", "Function", "body", 8
        )
        self.assertEqual(
            runner.dedupe_candidates([search, graph]),
            [
                runner.Candidate(
                    "requests/models.py",
                    388,
                    395,
                    "PreparedRequest::prepare_content_length",
                    "Function",
                    "body",
                    0,
                )
            ],
        )

    def test_short_method_selection_adds_relevant_scoped_sibling(self):
        selected = runner.Candidate(
            "requests/models.py", 388, 395, "PreparedRequest::prepare_content_length", "Function", "", 0
        )
        sibling = runner.Candidate(
            "requests/models.py", 332, 386, "PreparedRequest::prepare_body", "Function", "", 1
        )
        unrelated = runner.Candidate(
            "requests/sessions.py", 1, 20, "Session::send", "Function", "", 2
        )
        self.assertEqual(
            runner.complete_scoped_context([selected], [sibling, unrelated, selected], 80),
            [selected, sibling],
        )

    def test_retrieval_floor_replaces_ungrounded_low_rank_selection(self):
        first = runner.Candidate(
            "requests/adapters.py", 293, 318, "HTTPAdapter::get_connection", "Function", "", 0
        )
        second = runner.Candidate(
            "requests/adapters.py", 373, 393, "HTTPAdapter::proxy_headers", "Function", "", 1
        )
        low_rank = runner.Candidate(
            "requests/sessions.py", 272, 299, "SessionRedirectMixin::rebuild_proxies", "Function", "", 4
        )
        grounded, applied = runner.apply_retrieval_floor(
            [low_rank], [first, second, low_rank], 80
        )
        self.assertTrue(applied)
        self.assertEqual(grounded, [first, second, low_rank])

    def test_retrieval_floor_leaves_top_ranked_selection_unchanged(self):
        first = runner.Candidate("a.py", 1, 10, "A::first", "Function", "", 0)
        second = runner.Candidate("a.py", 11, 20, "A::second", "Function", "", 1)
        grounded, applied = runner.apply_retrieval_floor(
            [second], [first, second], 80
        )
        self.assertFalse(applied)
        self.assertEqual(grounded, [second])

    def test_retrieval_floor_ignores_cross_file_or_test_anchors(self):
        test_hit = runner.Candidate("tests/test_proxy.py", 1, 8, "test_proxy", "Function", "", 0)
        first = runner.Candidate("a.py", 1, 10, "A::first", "Function", "", 1)
        second = runner.Candidate("b.py", 11, 20, "B::second", "Function", "", 2)
        low_rank = runner.Candidate("c.py", 21, 30, "C::third", "Function", "", 3)
        grounded, applied = runner.apply_retrieval_floor(
            [low_rank], [test_hit, first, second, low_rank], 80
        )
        self.assertFalse(applied)
        self.assertEqual(grounded, [low_rank])

    def test_scoped_normalization_prefers_complete_high_ranked_method_pair(self):
        content_length = runner.Candidate(
            "models.py", 388, 395, "PreparedRequest::prepare_content_length", "Function", "", 0
        )
        prepare_body = runner.Candidate(
            "models.py", 332, 386, "PreparedRequest::prepare_body", "Function", "", 1
        )
        low_one = runner.Candidate("models.py", 397, 411, "prepare_auth", "Function", "", 2)
        low_two = runner.Candidate("models.py", 216, 231, "prepare", "Function", "", 3)
        self.assertEqual(
            runner.normalize_scoped_selection(
                [content_length, low_one, low_two],
                [content_length, prepare_body, low_one, low_two],
                80,
            ),
            [content_length, prepare_body],
        )

    def test_scoped_normalization_keeps_only_top_constructor(self):
        blueprint = runner.Candidate(
            "blueprints.py", 172, 206, "Blueprint::__init__", "Function", "", 0
        )
        setup = runner.Candidate(
            "blueprints.py", 39, 83, "BlueprintSetupState::__init__", "Function", "", 1
        )
        self.assertEqual(
            runner.normalize_scoped_selection([blueprint, setup], [setup, blueprint], 80),
            [blueprint],
        )

    def test_load_env_file_does_not_override_existing_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("EXISTING=from-file\nNEW_VALUE=loaded\n")
            old_existing = runner.os.environ.get("EXISTING")
            old_new = runner.os.environ.get("NEW_VALUE")
            try:
                runner.os.environ["EXISTING"] = "from-shell"
                runner.os.environ.pop("NEW_VALUE", None)
                runner.load_env_file(path)
                self.assertEqual(runner.os.environ["EXISTING"], "from-shell")
                self.assertEqual(runner.os.environ["NEW_VALUE"], "loaded")
            finally:
                if old_existing is None:
                    runner.os.environ.pop("EXISTING", None)
                else:
                    runner.os.environ["EXISTING"] = old_existing
                if old_new is None:
                    runner.os.environ.pop("NEW_VALUE", None)
                else:
                    runner.os.environ["NEW_VALUE"] = old_new

    def test_load_rows_joins_repo_url_from_full_dataset(self):
        import pandas as pd

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pd.DataFrame(
                [{"instance_id": "task-1", "repo": "short", "problem_statement": "issue"}]
            ).to_parquet(root / "contextbench_verified_train.parquet")
            pd.DataFrame(
                [{
                    "instance_id": "task-1",
                    "repo": "owner/repo",
                    "repo_url": "https://github.com/owner/repo.git",
                }]
            ).to_parquet(root / "full.parquet")
            rows = runner.load_rows(root / "contextbench_verified_train.parquet", None, None)
            self.assertEqual(rows[0]["repo"], "owner/repo")
            self.assertEqual(rows[0]["repo_url"], "https://github.com/owner/repo.git")

    def test_memtrace_env_enables_real_reranker(self):
        env = runner.memtrace_env(Path("/tmp/repo"), Path("/tmp/work"), Path("/tmp/model"))
        self.assertEqual(env["MEMTRACE_RERANK"], "on")
        self.assertEqual(env["MEMTRACE_RERANK_MODEL_DIR"], "/tmp/model")
        self.assertEqual(env["MEMTRACE_INDEX_DECLARATION_FILES"], "1")

    def test_memtrace_env_uses_walker_override_for_hard_excluded_dirs(self):
        env = runner.memtrace_env(
            Path("/tmp/repo"),
            Path("/tmp/work"),
            walker_include_dirs=["out", "bin", "out"],
        )
        self.assertEqual(env["MEMTRACE_WALKER_INCLUDE_DIRS"], "bin,out")

    def test_memtrace_env_can_point_at_persistent_graph_cache(self):
        env = runner.memtrace_env(
            Path("/tmp/repo"),
            Path("/tmp/work"),
            Path("/tmp/model"),
            Path("/tmp/cache-entry"),
        )
        self.assertEqual(env["MEMTRACE_MEMDB_DATA_DIR"], "/tmp/cache-entry/memdb")
        self.assertEqual(env["MEMTRACE_DATA_DIR"], "/tmp/cache-entry/state")

    def test_graph_cache_key_is_commit_and_namespace_scoped(self):
        first = runner.graph_cache_key("https://example.test/repo.git", "abc", "v1")
        self.assertEqual(first, runner.graph_cache_key("https://example.test/repo.git", "abc", "v1"))
        self.assertNotEqual(first, runner.graph_cache_key("https://example.test/repo.git", "def", "v1"))
        self.assertNotEqual(first, runner.graph_cache_key("https://example.test/repo.git", "abc", "v2"))

    def test_tracked_nested_dirs_use_walker_and_ignore_reincludes(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            source_dir = repo / "src" / "build"
            source_dir.mkdir(parents=True)
            for index in range(1):
                (source_dir / f"tool_{index}.py").write_text(
                    f"def tool_{index}():\n    return {index}\n", encoding="utf-8"
                )
            subprocess.run(
                ["git", "-C", str(repo), "add", "src/build"], capture_output=True, check=True
            )
            self.assertEqual(runner.write_tracked_dir_reincludes(repo), ["build"])
            self.assertEqual(
                (repo / ".memtraceignore").read_text(encoding="utf-8"),
                "!**/build/\n!**/build/**\n",
            )

    def test_graph_cache_manifest_is_atomic_and_workspace_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "stable-workspace" / "repo"
            repo.mkdir(parents=True)
            entry = root / "cache"
            manifest = runner.graph_cache_manifest(
                "task", "https://example.test/repo.git", "abc", "v2", repo
            )
            runner.write_graph_cache_manifest(entry, manifest)
            self.assertTrue(runner.graph_cache_is_reusable(entry, manifest))
            moved = {**manifest, "workspace_root": str(root / "other" / "repo")}
            self.assertFalse(runner.graph_cache_is_reusable(entry, moved))
            self.assertEqual(list(entry.glob("complete.json.tmp-*")), [])

    def test_validate_rerank_model_rejects_lexical_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "refusing lexical fallback"):
                runner.validate_rerank_model(Path(directory))

    def test_dedupe_search_rows_keeps_highest_cross_encoder_score(self):
        low = {"file_path": "a.py", "name": "target", "start_line": 1, "end_line": 2, "score": 0.1}
        high = {**low, "score": 0.9}
        self.assertEqual(runner.dedupe_search_rows([low, high]), [high])

    def test_expands_top_structural_anchors_from_each_query(self):
        searches = [
            {
                "results": [
                    {"kind": "Class", "name": "BlueprintSetupState", "file_path": "blueprints.py"},
                    {"kind": "Class", "name": "Blueprint", "file_path": "blueprints.py"},
                    {"kind": "Function", "name": "register", "file_path": "blueprints.py"},
                    {"kind": "Function", "name": "ignored", "file_path": "blueprints.py"},
                ]
            },
            {
                "results": [
                    {"kind": "Class", "name": "Blueprint", "file_path": "blueprints.py"},
                    {"kind": "Function", "name": "add_url_rule", "file_path": "app.py"},
                ]
            },
        ]
        selected = runner.select_expansion_rows(searches, per_query_limit=3)
        self.assertEqual(
            [(row["name"], row["file_path"]) for row in selected],
            [
                ("BlueprintSetupState", "blueprints.py"),
                ("Blueprint", "blueprints.py"),
                ("register", "blueprints.py"),
                ("add_url_rule", "app.py"),
            ],
        )

    def test_parent_class_anchor_promotes_method_container(self):
        context = {
            "contained_by": {
                "kind": "Class",
                "name": "PreparedRequest",
                "file_path": "requests/models.py",
            }
        }
        self.assertEqual(
            runner.parent_class_anchor(context),
            context["contained_by"],
        )

    def test_parent_class_anchor_ignores_file_container(self):
        self.assertIsNone(
            runner.parent_class_anchor(
                {"contained_by": {"kind": "File", "name": "models.py", "file_path": "models.py"}}
            )
        )

    def test_observation_step_preserves_one_retrieval_action(self):
        first = runner.Candidate("src/a.py", 10, 20, "first", "Function")
        duplicate = runner.Candidate("src/a.py", 10, 20, "first", "Function")
        second = runner.Candidate("src/b.py", 30, 35, "second", "Function")

        self.assertEqual(
            runner.observation_step([first, duplicate, second]),
            {
                "files": ["src/a.py", "src/b.py"],
                "spans": {
                    "src/a.py": [{"type": "line", "start": 10, "end": 20}],
                    "src/b.py": [{"type": "line", "start": 30, "end": 35}],
                },
                "symbols": {},
            },
        )

    def test_observation_step_omits_empty_actions(self):
        self.assertIsNone(runner.observation_step([]))

    def test_exact_identifier_floor_adds_named_function_within_budget(self):
        selected = runner.Candidate(
            "requests/adapters.py", 373, 393, "proxy_headers", "Function", "return headers"
        )
        duplicate = runner.Candidate(
            "requests/adapters.py", 373, 393, "HTTPAdapter::proxy_headers", "Function"
        )
        named = runner.Candidate("requests/utils.py", 985, 998, "get_auth_from_url", "Function")
        grounded, applied = runner.apply_exact_identifier_floor(
            [selected], [duplicate, named], 80
        )
        self.assertTrue(applied)
        self.assertEqual(
            grounded,
            [
                runner.Candidate(
                    "requests/adapters.py",
                    373,
                    393,
                    "HTTPAdapter::proxy_headers",
                    "Function",
                    "return headers",
                ),
                named,
            ],
        )

    def test_exact_identifier_floor_respects_budget(self):
        # `selected` (70) + `named` (20) = 90 > budget (80), so only one can
        # fit. `selected` must win the budget race: it's the selector's own
        # deliberate pick, `named` is only a floor candidate. (Reordering
        # fixed a real regression -- see
        # test_exact_identifier_floor_prefers_selected_over_floor_when_budget_tight
        # below for the forensic case this protects.)
        selected = runner.Candidate("a.py", 1, 70, "selected", "Function")
        named = runner.Candidate("b.py", 1, 20, "named", "Function")
        grounded, applied = runner.apply_exact_identifier_floor([selected], [named], 80)
        self.assertFalse(
            applied, "the floor anchor could not fit, so it was never actually applied"
        )
        self.assertEqual(grounded, [selected])

    def test_exact_identifier_floor_prefers_selected_over_floor_when_budget_tight(self):
        # Regression for a real ContextBench forensic finding (prettier/prettier,
        # SWE-PolyBench javascript 3b6e6f3a, guarded mode): GPT-5 explicitly
        # selected only `createParse` (42 lines) with a detailed, correct
        # justification for why several small exact-identifier-match
        # candidates from the printing phase (unrelated to this parse-error
        # bug) should NOT be included. With the old `[*exact, *selected]`
        # ordering those smaller floor candidates greedily consumed the
        # budget first, so `createParse` -- the selector's real, deliberate
        # pick -- silently never made it into the final prediction while an
        # explicitly-rejected file did.
        selected = runner.Candidate(
            "src/language-js/parser-babel.js", 63, 104, "createParse", "Function"
        )  # 42 lines
        floor_a = runner.Candidate(
            "src/language-yaml/printer-yaml.js", 69, 139, "genericPrint", "Function"
        )  # 71 lines
        floor_b = runner.Candidate(
            "src/main/comments.js", 500, 541, "printComments", "Function"
        )  # 42 lines
        floor_c = runner.Candidate(
            "src/main/core.js", 66, 171, "coreFormat", "Function"
        )  # 106 lines
        grounded, _applied = runner.apply_exact_identifier_floor(
            [selected], [floor_a, floor_b, floor_c], 150
        )
        self.assertIn(
            selected,
            grounded,
            "the selector's own pick must survive even when floor candidates "
            "alone would otherwise exhaust the budget first",
        )

    def test_lane_top_candidates_looks_past_already_claimed_file_in_same_lane(self):
        # Regression for the guarded-selector SELECTION-bucket miss: a sibling
        # file (e.g. flipt's cache.go) ranked second in a lane whose top hit
        # (config.go) another lane already floored used to make the whole
        # lane a no-op (old code `break`s on the first already-seen file).
        # The floor must keep scanning the lane for its next distinct file.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("internal/config/config.go", "internal/config/cache.go"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            pool = [
                runner.Candidate("internal/config/config.go", 1, 10, "Load", "Function"),
                runner.Candidate("internal/config/cache.go", 1, 10, "Get", "Function"),
            ]
            lane_one = {
                "results": [
                    {
                        "file_path": str(root / "internal/config/config.go"),
                        "start_line": 1,
                        "end_line": 10,
                        "name": "Load",
                        "kind": "Function",
                    },
                ]
            }
            lane_two = {
                "results": [
                    {
                        "file_path": str(root / "internal/config/config.go"),
                        "start_line": 1,
                        "end_line": 10,
                        "name": "Load",
                        "kind": "Function",
                    },
                    {
                        "file_path": str(root / "internal/config/cache.go"),
                        "start_line": 1,
                        "end_line": 10,
                        "name": "Get",
                        "kind": "Function",
                    },
                ]
            }
            tops = runner.lane_top_candidates([lane_one, lane_two], pool, root, 80)
            self.assertEqual(
                {item.file for item in tops},
                {"internal/config/config.go", "internal/config/cache.go"},
            )

    def test_lane_top_candidates_caps_at_two_distinct_files_per_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("a.py", "b.py", "c.py"):
                (root / relative).touch()
            pool = [
                runner.Candidate("a.py", 1, 5, "a", "Function"),
                runner.Candidate("b.py", 1, 5, "b", "Function"),
                runner.Candidate("c.py", 1, 5, "c", "Function"),
            ]
            lane = {
                "results": [
                    {
                        "file_path": str(root / "a.py"),
                        "start_line": 1,
                        "end_line": 5,
                        "name": "a",
                        "kind": "Function",
                    },
                    {
                        "file_path": str(root / "b.py"),
                        "start_line": 1,
                        "end_line": 5,
                        "name": "b",
                        "kind": "Function",
                    },
                    {
                        "file_path": str(root / "c.py"),
                        "start_line": 1,
                        "end_line": 5,
                        "name": "c",
                        "kind": "Function",
                    },
                ]
            }
            tops = runner.lane_top_candidates([lane], pool, root, 80)
            self.assertEqual({item.file for item in tops}, {"a.py", "b.py"})

    def test_graph_context_rows_feeds_lane_top_candidates_as_a_pseudo_lane(self):
        # Mirrors how the guarded call site wraps a graph_contexts entry as a
        # {"results": [...]} pseudo-search so graph-only hits (e.g.
        # qutebrowser's miscmodels.py, surfaced only via graph expansion)
        # are visible to the lane floor.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = "qutebrowser/misc/miscmodels.py"
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            pool = [runner.Candidate(relative, 1, 5, "miscmodels", "Function")]
            context = {
                "symbol": {
                    "file_path": str(path),
                    "start_line": 1,
                    "end_line": 5,
                    "name": "miscmodels",
                    "kind": "Function",
                }
            }
            pseudo_lane = {"results": runner.graph_context_rows(context)}
            tops = runner.lane_top_candidates([pseudo_lane], pool, root, 80)
            self.assertEqual(tops, pool)

    def test_response_usage_tracks_cached_input_tokens(self):
        self.assertEqual(
            runner.response_usage(
                {
                    "usage": {
                        "input_tokens": 100,
                        "input_tokens_details": {"cached_tokens": 40},
                        "output_tokens": 20,
                        "output_tokens_details": {"reasoning_tokens": 7},
                        "total_tokens": 120,
                    }
                }
            ),
            {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "output_tokens": 20,
                "reasoning_tokens": 7,
                "total_tokens": 120,
            },
        )

    def test_concept_requirement_uses_file_evidence_and_strongest_query_window(self):
        query = "add a command-line --size-hint option and propagate the stream size"
        pool = [
            runner.Candidate(
                "contrib/parallel/Options.cpp", 1, 40, "Options", "Function",
                "parse parallel worker options",
            ),
            runner.Candidate(
                "programs/toolcli.c", 100, 139, "main", "Function",
                "command line stream source size option compression parameters",
            ),
            runner.Candidate(
                "programs/toolcli.c", 20, 59, "parseLegacy", "Function",
                "parse unrelated legacy dictionary option",
            ),
        ]
        requirements = runner.selector_concept_requirements(
            pool,
            query,
            [
                "stdin compression size behavior",
                "command line option parsing into stream setup",
                "validate numeric source size",
            ],
            200,
            # Direct root evidence belongs to the file, not necessarily the
            # exact bounded window that best covers the concept.
            candidate_lanes={
                ("programs/toolcli.c", 20, 59): ["data_flow"],
            },
            file_query_families={
                "programs/toolcli.c": ["symptom", "data_flow", "input_transform"],
            },
        )

        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0]["name"], "command_line_option_surface")
        self.assertEqual(requirements[0]["candidate_ids"][0], 1)
        self.assertEqual(requirements[0]["reserved_line_budget"], 50)

    def test_guarded_selector_adds_missing_concept_and_records_accepted_ids(self):
        query = "add a command-line --size-hint option for stdin stream size"
        candidates = [
            runner.Candidate(
                "contrib/parallel/Options.h", 1, 47, "Options", "Class",
                "parallel options",
            ),
            runner.Candidate(
                "contrib/parallel/Options.cpp", 50, 89, "Options", "Function",
                "parse parallel options",
            ),
            runner.Candidate(
                "programs/toolcli.c", 100, 139, "main", "Function",
                "command line stream source size option setup",
            ),
        ]
        pool = runner.build_selector_pool(
            candidates, query, candidates, runner.GUARDED_SELECTOR_CANDIDATES
        )
        aux_ids = [
            pool.index(candidate)
            for candidate in candidates[:2]
        ]
        completed = self._selector_response("completed", "resp-concept", [*aux_ids, 999])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch.object(
            runner, "send_openai_request", return_value=completed
        ):
            chosen, audit = runner.select_context_with_gpt(
                candidates,
                query,
                200,
                "gpt-test",
                priority_candidates=candidates,
                candidate_lanes={
                    ("programs/toolcli.c", 100, 139): ["data_flow"],
                },
                selector_mode="guarded",
                concept_queries=[
                    "stdin stream size behavior",
                    "command line option parsing into setup",
                    "validate numeric size",
                ],
                file_query_families={
                    "programs/toolcli.c": [
                        "symptom", "data_flow", "input_transform"
                    ],
                },
            )

        self.assertIn("programs/toolcli.c", {candidate.file for candidate in chosen})
        self.assertLessEqual(sum(candidate.line_count for candidate in chosen), 190)
        self.assertEqual(audit["model_candidate_ids"], [*aux_ids, 999])
        self.assertNotIn(999, audit["selected_candidate_ids"])
        self.assertTrue(audit["concept_coverage"]["applied"])
        self.assertEqual(audit["concept_coverage"]["boundary_line_reserve"], 10)

    def test_concept_boundary_identifiers_keep_semantic_adjacent_setter_cluster(self):
        anchor = runner.Candidate(
            "programs/toolcli.c",
            100,
            139,
            "main",
            "Function",
            "\n".join(
                [
                    "IO_setWorkers(prefs, workers);",
                    "IO_setEstimatedInputSize(prefs, estimatedInputSize);",
                    "IO_setTargetBlockSize(prefs, targetBlockSize);",
                    "IO_setColorMode(prefs, colorMode);",
                ]
            ),
        )
        self.assertEqual(
            runner.concept_boundary_identifiers(
                anchor,
                ["propagate estimated input stream size into target block selection"],
            ),
            ["IO_setEstimatedInputSize", "IO_setTargetBlockSize"],
        )

    def test_concept_boundary_expansion_resolves_and_protects_exact_definitions(self):
        class FakeClient:
            def __init__(self, root):
                self.root = root
                self.calls = []

            def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                symbol = arguments["symbol"]
                spans = {
                    "IO_setEstimatedInputSize": (1, 3),
                    "IO_setTargetBlockSize": (5, 7),
                }
                start, end = spans[symbol]
                return {
                    "found": True,
                    "symbol": {
                        "file_path": str(self.root / "src/io.c"),
                        "start_line": start,
                        "end_line": end,
                        "name": symbol,
                        "kind": "Function",
                    },
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src/io.c"
            source.parent.mkdir(parents=True)
            source.write_text(
                "void IO_setEstimatedInputSize(void) {\n  apply_size();\n}\n\n"
                "void IO_setTargetBlockSize(void) {\n  apply_target();\n}\n",
                encoding="utf-8",
            )
            anchor = runner.Candidate(
                "programs/toolcli.c",
                100,
                139,
                "main",
                "Function",
                "IO_setEstimatedInputSize(prefs, estimatedInputSize);\n"
                "IO_setTargetBlockSize(prefs, targetBlockSize);",
            )
            audit = {
                "candidate_pool": [
                    {
                        "id": 0,
                        "file": anchor.file,
                        "start": anchor.start,
                        "end": anchor.end,
                        "name": anchor.name,
                        "retrieval_lanes": ["data_flow"],
                    }
                ],
                "selected_candidate_ids": [0],
                "concept_coverage": {"added_candidate_ids": [0]},
            }
            client = FakeClient(root)
            expanded, graph_records = runner.expand_concept_boundaries(
                client,
                "repo",
                root,
                [anchor],
                audit,
                ["estimated input stream size and target block size"],
                200,
            )

        self.assertEqual(len(expanded), 3)
        self.assertEqual(
            {(candidate.file, candidate.start, candidate.end) for candidate in expanded[1:]},
            {("src/io.c", 1, 3), ("src/io.c", 5, 7)},
        )
        self.assertEqual(audit["selected_candidate_ids"], [0, 1, 2])
        self.assertEqual(
            [entry["retrieval_lanes"] for entry in audit["candidate_pool"][1:]],
            [["concept_boundary"], ["concept_boundary"]],
        )
        self.assertEqual(len(graph_records), 2)
        self.assertTrue(
            all(
                event["accepted"]
                for event in audit["concept_coverage"]["boundary_expansion"]
            )
        )
        # Ordinary exact/lane floors may add other context later, but the
        # protected concept chain itself remains present and under its cap.
        runner.validate_concept_boundary_cap(
            [
                *expanded,
                runner.Candidate("src/unrelated.c", 1, 100, "other", "Function"),
            ],
            audit,
            200,
        )

    def test_boundary_graph_receipts_cannot_reenter_generic_lane_floor(self):
        ordinary = {
            "anchor": {"name": "ordinary"},
            "context": {
                "symbol": {
                    "file_path": "src/ordinary.c",
                    "start_line": 1,
                    "end_line": 3,
                    "name": "ordinary",
                    "kind": "Function",
                }
            },
        }
        boundary = {
            "anchor": {"name": "setter", "source": "concept_boundary"},
            "context": {
                "symbol": {
                    "file_path": "src/unrelated.c",
                    "start_line": 1,
                    "end_line": 100,
                    "name": "unrelated",
                    "kind": "Function",
                }
            },
        }

        searches = runner.graph_lane_searches_for_floor([ordinary, boundary])

        self.assertEqual(len(searches), 1)
        self.assertEqual(searches[0]["results"][0]["name"], "ordinary")

    def test_concept_boundary_cap_rejects_adversarial_overflow(self):
        final = [
            runner.Candidate("programs/cli.c", 1, 40, "main", "Function"),
            runner.Candidate("src/io.c", 1, 11, "setSize", "Function"),
        ]
        audit = {
            "candidate_pool": [
                {"id": 0, "file": "programs/cli.c", "start": 1, "end": 40},
                {"id": 1, "file": "src/io.c", "start": 1, "end": 11},
            ],
            "concept_coverage": {
                "added_candidate_ids": [0],
                "boundary_expansion": [
                    {"accepted": True, "candidate_id": 1},
                ],
            },
        }

        with self.assertRaisesRegex(RuntimeError, "reserved support cap"):
            runner.validate_concept_boundary_cap(final, audit, 200)

    def test_selector_continuation_policy_completes_in_one_guarded_request(self):
        candidates = self._selector_candidates()
        completed = self._selector_response("completed", "resp-1", [0])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch.object(
            runner, "send_openai_request", return_value=completed
        ) as sender:
            chosen, audit = runner.select_context_with_gpt(
                candidates,
                "target handling",
                80,
                "gpt-test",
                selector_mode="guarded",
                selector_policy="continuation-v2",
            )

        request_body = json.loads(sender.call_args.args[0].data)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(request_body["max_output_tokens"], 12400)
        self.assertEqual(chosen[0].name, "target_handler")
        self.assertEqual(audit["selector_policy"], "continuation-v2")
        self.assertEqual(audit["attempts"], 1)
        self.assertEqual(audit["reasoning_tokens"], 7)
        self.assertEqual(audit["logical_attempts"][0]["strategy"], "initial")
        self.assertEqual(audit["logical_attempts"][0]["parse_status"], "parsed")
        self.assertTrue(audit["logical_attempts"][0]["full_candidate_payload"])

    def test_selector_default_policy_preserves_full_replay_sequence(self):
        responses = [
            self._selector_response(
                "incomplete", "resp-1", reason="max_output_tokens"
            ),
            self._selector_response(
                "incomplete", "resp-2", reason="max_output_tokens"
            ),
            self._selector_response("completed", "resp-3", [0]),
        ]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch.object(
            runner, "send_openai_request", side_effect=responses
        ) as sender:
            _, audit = runner.select_context_with_gpt(
                self._selector_candidates(),
                "target handling",
                80,
                "gpt-test",
                selector_mode="guarded",
            )

        request_bodies = [json.loads(call.args[0].data) for call in sender.call_args_list]
        self.assertEqual(audit["selector_policy"], "replay-v1")
        self.assertEqual(
            [body["max_output_tokens"] for body in request_bodies],
            [6200, 12400, 18600],
        )
        self.assertTrue(all('"candidates":' in body["input"] for body in request_bodies))
        self.assertEqual(
            [attempt["strategy"] for attempt in audit["logical_attempts"]],
            ["initial", "full_replay", "full_replay"],
        )

    def test_selector_continues_max_token_response_without_replaying_pool(self):
        responses = [
            self._selector_response(
                "incomplete", "resp-1", reason="max_output_tokens"
            ),
            self._selector_response("completed", "resp-2", [0]),
        ]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch.object(
            runner, "send_openai_request", side_effect=responses
        ) as sender:
            chosen, audit = runner.select_context_with_gpt(
                self._selector_candidates(),
                "target handling",
                80,
                "gpt-test",
                selector_mode="guarded",
                selector_policy="continuation-v2",
            )

        request_bodies = [json.loads(call.args[0].data) for call in sender.call_args_list]
        first, continuation = request_bodies
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(first["max_output_tokens"], 12400)
        self.assertEqual(continuation["max_output_tokens"], 12400)
        self.assertEqual(continuation["previous_response_id"], "resp-1")
        self.assertEqual(continuation["instructions"], first["instructions"])
        self.assertEqual(continuation["text"], first["text"])
        self.assertNotIn('"candidates":', continuation["input"])
        self.assertEqual(chosen[0].name, "target_handler")
        self.assertFalse(audit["fallback_used"])
        self.assertEqual(
            [attempt["strategy"] for attempt in audit["logical_attempts"]],
            ["initial", "continuation"],
        )
        self.assertFalse(audit["logical_attempts"][1]["full_candidate_payload"])
        self.assertEqual(
            audit["logical_attempts"][0]["candidate_pool_sha256"],
            audit["logical_attempts"][1]["candidate_pool_sha256"],
        )

    def test_selector_continuation_policy_falls_back_on_non_token_incomplete(self):
        incomplete = self._selector_response(
            "incomplete", "resp-1", reason="content_filter"
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch.object(
            runner, "send_openai_request", return_value=incomplete
        ) as sender:
            chosen, audit = runner.select_context_with_gpt(
                self._selector_candidates(),
                "target handling",
                80,
                "gpt-test",
                selector_mode="guarded",
                selector_policy="continuation-v2",
            )

        self.assertEqual(sender.call_count, 1)
        self.assertTrue(chosen)
        self.assertTrue(audit["fallback_used"])
        self.assertEqual(
            audit["fallback_reason"], "non_retryable_incomplete:content_filter"
        )

    def test_selector_continuation_policy_falls_back_without_response_id(self):
        incomplete = self._selector_response(
            "incomplete", reason="max_output_tokens"
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch.object(
            runner, "send_openai_request", return_value=incomplete
        ) as sender:
            _, audit = runner.select_context_with_gpt(
                self._selector_candidates(),
                "target handling",
                80,
                "gpt-test",
                selector_mode="guarded",
                selector_policy="continuation-v2",
            )

        self.assertEqual(sender.call_count, 1)
        self.assertTrue(audit["fallback_used"])
        self.assertEqual(audit["fallback_reason"], "missing_response_id")

    def test_selector_continuation_policy_never_makes_a_third_logical_attempt(self):
        responses = [
            self._selector_response(
                "incomplete", "resp-1", reason="max_output_tokens"
            ),
            self._selector_response(
                "incomplete", "resp-2", reason="max_output_tokens"
            ),
        ]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch.object(
            runner, "send_openai_request", side_effect=responses
        ) as sender:
            _, audit = runner.select_context_with_gpt(
                self._selector_candidates(),
                "target handling",
                80,
                "gpt-test",
                selector_mode="guarded",
                selector_policy="continuation-v2",
            )

        self.assertEqual(sender.call_count, 2)
        self.assertEqual(audit["attempts"], 2)
        self.assertTrue(audit["fallback_used"])
        self.assertEqual(
            audit["fallback_reason"],
            "continuation_incomplete:max_output_tokens",
        )

    def test_transport_retry_does_not_increment_logical_selector_attempts(self):
        completed = self._selector_response("completed", "resp-1", [0])
        rate_limit = urllib.error.HTTPError(
            "https://api.openai.com/v1/responses",
            429,
            "rate limited",
            None,
            io.BytesIO(b'{"error":"rate limited"}'),
        )
        response_stream = io.BytesIO(json.dumps(completed).encode("utf-8"))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch.object(
            runner.urllib.request,
            "urlopen",
            side_effect=[rate_limit, response_stream],
        ) as urlopen, patch.object(runner.time, "sleep") as sleep:
            _, audit = runner.select_context_with_gpt(
                self._selector_candidates(2),
                "target handling",
                80,
                "gpt-test",
                selector_mode="guarded",
                selector_policy="continuation-v2",
            )

        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(audit["attempts"], 1)
        self.assertEqual(audit["attempt_count_semantics"], "logical_selector_requests")
        self.assertEqual(len(audit["logical_attempts"]), 1)

    def test_guarded_selector_raises_reasoning_effort(self):
        # Guarded mode pays for a 3x larger candidate pool because the
        # cross-file inclusion judgment is harder; reasoning effort should
        # scale with that instead of staying at the default-mode setting.
        source = Path(runner.__file__).read_text(encoding="utf-8")
        self.assertIn('"reasoning": {"effort": "medium" if guarded else "low"}', source)

    def test_guarded_instructions_include_unconditional_sibling_selfcheck_and_test_override(self):
        # Regression for the two prompt-level SELECTION-bucket root causes:
        # (1) the old multi-file nudge only fired when lanes *disagreed*,
        # which never triggers for a same-lane sibling file; (2) the base
        # "avoid tests" clause discouraged legitimate test-file gold hits.
        source = Path(runner.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "shares 2 or more retrieval_lanes with an already-selected candidate",
            source,
        )
        self.assertIn("Do not exclude a candidate", source)
        self.assertIn("solely because it is a test file", source)

    def test_guarded_pool_widened_for_symbol_dense_confirmed_files(self):
        # Regression for the SPAN-bucket root cause: build_selector_pool's
        # flat pool_size truncation, combined with refinement_priority's old
        # bare-6-per-file / first-encountered-order floor, silently dropped
        # gold siblings from symbol-dense files even though the file was
        # already a confirmed refinement_file (verified live on ponyc
        # matchtype.c: is_x_match_nominal + is_nominal_match_nominal fell
        # out of a 120-candidate pool that otherwise covered ~16 of that
        # file's ~18 sibling dispatch functions). The floor must protect
        # more than 6 candidates per confirmed-relevant file, chosen by
        # relevance rather than first-encountered order, with pool_size
        # grown to give the wider floor room instead of crowding out other
        # files' candidates.
        self.assertGreater(runner.GUARDED_SELECTOR_CANDIDATES, 120)
        self.assertGreater(runner.REFINEMENT_FILE_PRIORITY_CAP, 6)
        source = Path(runner.__file__).read_text(encoding="utf-8")
        self.assertIn("key=lambda item: -candidate_score(item, issue)", source)
        self.assertIn("[:REFINEMENT_FILE_PRIORITY_CAP]", source)

    def test_build_selector_pool_keeps_wide_per_file_priority_floor(self):
        # priority_candidates is the actual floor mechanism: as long as they
        # fit within pool_size none should be evicted by the flat
        # global-score truncation, however many come from one symbol-dense
        # file. This exercises that guarantee at the new, wider cap.
        query = "tuple match nominal interface dispatch"
        priority = [
            runner.Candidate(
                "matchtype.c", i * 10, i * 10 + 5, f"is_case_{i}", "Function", "match dispatch"
            )
            for i in range(runner.REFINEMENT_FILE_PRIORITY_CAP)
        ]
        noise = [
            runner.Candidate(f"other_{i}.c", 1, 5, f"unrelated_{i}", "Function", "irrelevant noise")
            for i in range(200)
        ]
        pool = runner.build_selector_pool(
            [*priority, *noise], query, priority, runner.GUARDED_SELECTOR_CANDIDATES
        )
        for candidate in priority:
            self.assertIn(candidate, pool)

    def test_guarded_max_items_scales_with_established_relevant_pool_share(self):
        # GUARDED_SELECTOR_MAX_ITEMS_CAP lets the schema allow more than the
        # flat 16 items when the pool is dominated by candidates from files
        # already confirmed relevant (file_refinement lane / exact-identifier
        # match) -- e.g. fmt's color.h, where 16 of 20 valid in-pool
        # candidates from one confirmed file couldn't be selected purely
        # because of the flat cap. The final line-budget accounting is
        # untouched, so this only widens what the selector is *allowed* to
        # name, not the true output size.
        self.assertGreaterEqual(
            runner.GUARDED_SELECTOR_MAX_ITEMS_CAP, runner.GUARDED_SELECTOR_MAX_ITEMS
        )
        source = Path(runner.__file__).read_text(encoding="utf-8")
        self.assertIn('"maxItems": max_items if guarded else 10,', source)
        self.assertIn("established_pool_count", source)


class PolicyV2Tests(unittest.TestCase):
    """Flag-gated retrieval policy v2; defaults must reproduce v1 behavior."""

    def test_normalize_issue_decodes_json_string_statement(self):
        encoded = '"# Title.\\n\\n## Description.\\n\\nWorker processes hang."'
        self.assertEqual(
            runner.normalize_issue_text(encoded),
            "# Title.\n\n## Description.\n\nWorker processes hang.",
        )

    def test_normalize_issue_collapses_literal_escape_runs(self):
        text = "prefix\\n\\nWorker\\nCurrently\\tbroken with body long enough"
        normalized = runner.normalize_issue_text(text)
        self.assertNotIn("\\n", normalized)
        self.assertNotIn("nWorker", "".join(runner.identifier_queries(normalized)))

    def test_normalize_issue_passes_plain_text_through(self):
        plain = "Blueprint requires a name\nwith real newlines"
        self.assertEqual(runner.normalize_issue_text(plain), plain)

    def test_discriminative_name_gate(self):
        self.assertTrue(runner.is_discriminative_name("fixture_dirs"))
        self.assertTrue(runner.is_discriminative_name("useAutocomplete"))
        self.assertTrue(runner.is_discriminative_name("loaddata"))
        self.assertFalse(runner.is_discriminative_name("update"))
        self.assertFalse(runner.is_discriminative_name("IP"))
        self.assertFalse(runner.is_discriminative_name("ensure"))

    def test_v2_score_denies_generic_word_name_bonus(self):
        generic = runner.Candidate("a.py", 1, 10, "update", "Function", "update the value")
        query = "the tool should update the config on disk"
        self.assertGreater(runner.candidate_score(generic, query), 2.0)
        self.assertLess(runner.candidate_score_v2(generic, query), 1.0)

    def test_v2_score_keeps_bonus_for_boundary_matched_identifier(self):
        named = runner.Candidate("a.py", 1, 10, "fixture_dirs", "Function", "")
        query = "loaddata crashes when fixture_dirs contains Path instances"
        self.assertGreater(runner.candidate_score_v2(named, query), 2.0)

    def test_v2_score_denies_name_embedded_inside_larger_word(self):
        named = runner.Candidate("a.py", 1, 10, "Autocomplete", "Class", "")
        query = "useAutocompleteDefault breaks focus"
        self.assertLess(runner.candidate_score_v2(named, query), 1.0)

    def test_junk_context_paths(self):
        for path in (
            "packages/mui/useAutocomplete.test.js",
            "src/component.spec.ts",
            "extern/llvm/lib/x.c",
            "node_modules/dep/index.js",
            "docs/usage.md",
            "config/settings.yml",
            "tests/test_core.py",
        ):
            self.assertTrue(runner.is_junk_context_path(path), path)
        self.assertFalse(runner.is_junk_context_path("src/useAutocomplete.js"))
        self.assertFalse(runner.is_junk_context_path("lib/ansible/executor/process/worker.py"))

    def test_is_test_path_extended_covers_dot_test_suffixes(self):
        self.assertFalse(runner.is_test_path("src/useAutocomplete.test.js"))
        self.assertTrue(runner.is_test_path("src/useAutocomplete.test.js", extended=True))
        self.assertTrue(runner.is_test_path("src/x.spec.ts", extended=True))
        self.assertFalse(runner.is_test_path("src/contest.js", extended=True))

    def test_pack_v2_novel_line_accounting_dedupes_overlaps(self):
        query = "fixture_dirs duplicate check"
        first = runner.Candidate("a.py", 100, 129, "fixture_dirs", "Function", "fixture_dirs body")
        overlap = runner.Candidate("a.py", 101, 129, "fixture_dirs", "Function", "fixture_dirs body")
        packed = runner.pack_final_context_v2([first, overlap], query, 40)
        covered = sorted(
            line for item in packed for line in range(item.start, item.end + 1)
        )
        self.assertEqual(len(covered), len(set(covered)) + 29)  # 29 shared lines billed once
        lines = runner.candidate_line_set(packed)
        self.assertEqual(len(lines), 30)

    def test_pack_v2_filters_junk_paths(self):
        query = "proxy_headers logic"
        junk = runner.Candidate("tests/test_proxy.py", 1, 10, "proxy_headers", "Function", "proxy_headers")
        real = runner.Candidate("src/adapters.py", 1, 10, "proxy_headers", "Function", "proxy_headers")
        packed = runner.pack_final_context_v2([junk, real], query, 80)
        self.assertEqual({item.file for item in packed}, {"src/adapters.py"})

    def test_pack_v2_tight_regime_stops_instead_of_filling(self):
        query = "loaddata crashes in fixture_dirs"
        anchor = runner.Candidate("cmd/loaddata.py", 139, 168, "loaddata", "Function", "loaddata fixture_dirs")
        sibling = runner.Candidate("cmd/loaddata.py", 355, 383, "fixture_dirs", "Function", "fixture_dirs")
        far_noise = runner.Candidate("other/noise.py", 1, 60, "noise", "Function", "unrelated words")
        packed = runner.pack_final_context_v2([anchor, sibling, far_noise], query, 200)
        self.assertIn(anchor, packed)
        self.assertIn(sibling, packed)
        self.assertNotIn(far_noise, packed)

    def test_pack_v2_consolidated_regime_fills_with_low_relevance_siblings(self):
        """Documents the cb-r1 django/gson regression mechanism: v2's
        consolidated regime has no score floor on same-file siblings, so it
        happily stuffs the budget with an unrelated same-file method even
        though it barely clears the global 0.2 relevance floor."""
        query = "fixture_dirs duplicate check other_helper in loaddata command"
        top = runner.Candidate(
            "cmd/loaddata.py", 354, 383, "fixture_dirs", "Function",
            "settings FIXTURE_DIRS check duplicate",
        )
        weak_sibling = runner.Candidate(
            "cmd/loaddata.py", 225, 279, "load_label", "Function",
            "load_label handling unrelated other stuff entirely",
        )
        rival = runner.Candidate(
            "other/helper.py", 1, 40, "other_helper", "Function",
            "other_helper duplicate check",
        )
        # sanity: not a tight-anchor case (rival ties top, so file dominance
        # never clears V2_TIGHT_FILE_DOMINANCE) and weak_sibling clears the
        # global floor but sits well under half of top's score.
        self.assertFalse(
            runner.candidate_score_v2(top, query)
            >= runner.V2_TIGHT_FILE_DOMINANCE * runner.candidate_score_v2(rival, query)
        )
        self.assertGreaterEqual(
            runner.candidate_score_v2(weak_sibling, query), runner.V2_RELEVANCE_FLOOR
        )
        self.assertLess(
            runner.candidate_score_v2(weak_sibling, query),
            0.5 * runner.candidate_score_v2(top, query),
        )
        v2_packed = runner.pack_final_context_v2(
            [top, weak_sibling, rival], query, 200, pack_policy="v2"
        )
        self.assertIn(weak_sibling, v2_packed)  # the bug: filler gets packed

    def test_pack_v3_consolidated_regime_drops_low_relevance_siblings(self):
        """v3 fix: the same scenario as above no longer packs the
        low-relevance same-file filler, but keeps the anchor and the
        genuinely useful cross-file spillover candidate."""
        query = "fixture_dirs duplicate check other_helper in loaddata command"
        top = runner.Candidate(
            "cmd/loaddata.py", 354, 383, "fixture_dirs", "Function",
            "settings FIXTURE_DIRS check duplicate",
        )
        weak_sibling = runner.Candidate(
            "cmd/loaddata.py", 225, 279, "load_label", "Function",
            "load_label handling unrelated other stuff entirely",
        )
        rival = runner.Candidate(
            "other/helper.py", 1, 40, "other_helper", "Function",
            "other_helper duplicate check",
        )
        v3_packed = runner.pack_final_context_v2(
            [top, weak_sibling, rival], query, 200, pack_policy="v3"
        )
        self.assertIn(top, v3_packed)
        self.assertIn(rival, v3_packed)
        self.assertNotIn(weak_sibling, v3_packed)

    def test_pack_v3_keeps_close_scoring_siblings(self):
        """v3 must not become a de-facto tight regime: a same-file sibling
        that scores close to the anchor (the sklearn/cli case v2 correctly
        helped) is still packed."""
        query = "update_state emit_diagnostics record_event pipeline handler in state machine"
        top = runner.Candidate(
            "pkg/state.py", 1, 30, "update_state", "Function",
            "update_state pipeline handler",
        )
        close_sibling = runner.Candidate(
            "pkg/state.py", 40, 70, "emit_diagnostics", "Function",
            "emit_diagnostics pipeline handler record_event",
        )
        v3_packed = runner.pack_final_context_v2(
            [top, close_sibling], query, 200, pack_policy="v3"
        )
        self.assertIn(top, v3_packed)
        self.assertIn(close_sibling, v3_packed)

    def test_pack_v3_tight_regime_unaffected(self):
        """v3 only touches the consolidated regime; the tight regime keeps
        v2's exact behavior (same test scenario as
        test_pack_v2_tight_regime_stops_instead_of_filling)."""
        query = "loaddata crashes in fixture_dirs"
        anchor = runner.Candidate("cmd/loaddata.py", 139, 168, "loaddata", "Function", "loaddata fixture_dirs")
        sibling = runner.Candidate("cmd/loaddata.py", 355, 383, "fixture_dirs", "Function", "fixture_dirs")
        far_noise = runner.Candidate("other/noise.py", 1, 60, "noise", "Function", "unrelated words")
        packed = runner.pack_final_context_v2([anchor, sibling, far_noise], query, 200, pack_policy="v3")
        self.assertIn(anchor, packed)
        self.assertIn(sibling, packed)
        self.assertNotIn(far_noise, packed)

    def test_pack_anchor_consolidated_sibling_floor_excludes_from_both_passes(self):
        """Regression guard for a narrower bug than the fix itself: a
        same-file candidate rejected by the floor in the first (same-file)
        pass must not be re-admitted by the second (global spillover) pass
        just because it is still present in `ranked`."""
        query = "anchor text only"
        top = runner.Candidate("a.py", 1, 10, "anchor", "Function", "anchor text only")
        weak_same_file = runner.Candidate("a.py", 20, 30, "filler", "Function", "text")

        def score_of(item):
            return {"anchor": 10.0, "filler": 2.0}[item.name]

        ranked = [top, weak_same_file]
        packed = runner.pack_anchor_consolidated(
            ranked, top, score_of, line_budget=200, sibling_rel_floor=0.5
        )
        self.assertIn(top, packed)
        self.assertNotIn(weak_same_file, packed)

    def test_pack_v4_keeps_close_scoring_siblings(self):
        """v4 must not become a de-facto tight regime either: a same-file
        sibling scoring close to the anchor (the sklearn/cli case both v2
        and v3 correctly kept) is still packed -- same scenario as
        test_pack_v3_keeps_close_scoring_siblings."""
        query = "update_state emit_diagnostics record_event pipeline handler in state machine"
        top = runner.Candidate(
            "pkg/state.py", 1, 30, "update_state", "Function",
            "update_state pipeline handler",
        )
        close_sibling = runner.Candidate(
            "pkg/state.py", 40, 70, "emit_diagnostics", "Function",
            "emit_diagnostics pipeline handler record_event",
        )
        v4_packed = runner.pack_final_context_v2(
            [top, close_sibling], query, 200, pack_policy="v4"
        )
        self.assertIn(top, v4_packed)
        self.assertIn(close_sibling, v4_packed)

    def test_pack_v4_tight_regime_unaffected(self):
        """v4 only touches the consolidated regime; the tight regime keeps
        v2's exact behavior (same scenario as
        test_pack_v2_tight_regime_stops_instead_of_filling)."""
        query = "loaddata crashes in fixture_dirs"
        anchor = runner.Candidate("cmd/loaddata.py", 139, 168, "loaddata", "Function", "loaddata fixture_dirs")
        sibling = runner.Candidate("cmd/loaddata.py", 355, 383, "fixture_dirs", "Function", "fixture_dirs")
        far_noise = runner.Candidate("other/noise.py", 1, 60, "noise", "Function", "unrelated words")
        packed = runner.pack_final_context_v2([anchor, sibling, far_noise], query, 200, pack_policy="v4")
        self.assertIn(anchor, packed)
        self.assertIn(sibling, packed)
        self.assertNotIn(far_noise, packed)

    def test_pack_v4_bounds_spillover_by_count(self):
        """The cross-file spillover pass is count-capped too, not just
        score-floored: even candidates that clear V4_SPILLOVER_REL_FLOOR
        stop being added once V4_MAX_SPILLOVER_ITEMS is reached."""
        top = runner.Candidate("a.py", 1, 10, "anchor", "Function", "x")

        def score_of(item):
            # Every rival scores at 0.95x the anchor -- comfortably above
            # V4_SPILLOVER_REL_FLOOR, so only the count cap can stop them.
            return {"anchor": 1.0}.get(item.name, 0.95)

        rivals = [
            runner.Candidate(f"other{i}.py", 1, 5, f"rival{i}", "Function", "x")
            for i in range(6)
        ]
        packed = runner.pack_anchor_consolidated_v4(
            [top, *rivals], top, score_of, line_budget=200
        )
        spilled = [c for c in packed if c.file != top.file]
        self.assertLessEqual(len(spilled), runner.V4_MAX_SPILLOVER_ITEMS)

    def test_pack_v4_django_regression_live_scenario(self):
        """Real django cb-r2-packv3 scenario: loaddata.py's six Command
        methods, gold entirely inside Command.fixture_dirs (30/31 gold
        lines land there per live recall 0.9677 --
        /tmp/cb-r2-packv3/predictions-audit/..3f745086.json). v2/v3 pack
        all 6 methods for 199 predicted lines and line precision 0.1507/
        0.1508 -- confirmed byte-for-byte identical between v2 and v3 on
        live data (v3's raw-score floor did not exclude a single sibling,
        because the live candidate_score_v2 spread across this file's
        methods is far flatter than the offline replay simulator the 0.75
        coefficient was tuned against assumed). This is reproduced here
        with the exact real file/method spans and a flat score
        distribution (every sibling clears the 0.75 floor) matching what
        was observed live.

        v4's structural (count, not score) cap must bound the filler
        regardless of that flat distribution: anchor + at most
        V4_MAX_SAME_FILE_SIBLINGS siblings -> 96 predicted lines / 0.3125
        precision in this fixture, verified against a real offline replay
        of the live candidate pool (scratchpad replay_pack_policies.py)."""
        fixture_dirs = runner.Candidate(
            "django/core/management/commands/loaddata.py", 354, 383,
            "Command::fixture_dirs", "Function", "x",
        )
        loaddata = runner.Candidate(
            "django/core/management/commands/loaddata.py", 139, 195,
            "Command::loaddata", "Function", "x",
        )
        get_fixture_name_and_dirs = runner.Candidate(
            "django/core/management/commands/loaddata.py", 281, 289,
            "Command::get_fixture_name_and_dirs", "Function", "x",
        )
        find_fixtures = runner.Candidate(
            "django/core/management/commands/loaddata.py", 314, 352,
            "Command::find_fixtures", "Function", "x",
        )
        find_fixture_files_in_dir = runner.Candidate(
            "django/core/management/commands/loaddata.py", 304, 312,
            "Command::find_fixture_files_in_dir", "Function", "x",
        )
        load_label = runner.Candidate(
            "django/core/management/commands/loaddata.py", 225, 279,
            "Command::load_label", "Function", "x",
        )
        ranked = [
            fixture_dirs, loaddata, get_fixture_name_and_dirs,
            find_fixtures, find_fixture_files_in_dir, load_label,
        ]
        # Flat live-observed score distribution: every sibling clears
        # V3_CONSOLIDATION_CHAIN_REL_FLOOR (0.75) of the anchor.
        scores = {
            "Command::fixture_dirs": 1.0,
            "Command::loaddata": 0.92,
            "Command::get_fixture_name_and_dirs": 0.90,
            "Command::find_fixtures": 0.88,
            "Command::find_fixture_files_in_dir": 0.86,
            "Command::load_label": 0.84,
        }

        def score_of(item):
            return scores[item.name]

        gold_lines_hit = 30  # of 31 gold lines total (live recall 0.9677)

        v3_packed = runner.pack_anchor_consolidated(
            ranked, fixture_dirs, score_of, line_budget=200,
            sibling_rel_floor=runner.V3_CONSOLIDATION_CHAIN_REL_FLOOR,
            sibling_floor_score_of=score_of,
        )
        v3_lines = sum(c.line_count for c in v3_packed)
        self.assertEqual(len(v3_packed), 6)  # the floor excluded nothing
        self.assertEqual(v3_lines, 199)  # matches the live pred_size exactly

        v4_packed = runner.pack_anchor_consolidated_v4(
            ranked, fixture_dirs, score_of, line_budget=200
        )
        self.assertIn(fixture_dirs, v4_packed)
        v4_lines = sum(c.line_count for c in v4_packed)
        self.assertLessEqual(
            len({c.name for c in v4_packed}), 1 + runner.V4_MAX_SAME_FILE_SIBLINGS
        )
        self.assertLess(v4_lines, v3_lines)

        v3_precision = gold_lines_hit / v3_lines
        v4_precision = gold_lines_hit / v4_lines
        # v3 reproduces the live 0.1508 precision; v4 must clear it
        # substantially (target: meaningfully above 0.151, approaching or
        # beating v1's live 0.201) while keeping the same gold hit (no
        # recall collapse -- fixture_dirs, the sole gold-bearing span, is
        # never dropped).
        self.assertAlmostEqual(v3_precision, 0.1508, places=3)
        self.assertGreater(v4_precision, 0.201)
        self.assertGreater(v4_precision, v3_precision * 1.5)

    def test_pack_policy_choices_include_v4(self):
        self.assertIn("v4", runner.PACK_POLICY_CHOICES)

    def test_recall_policy_choices_include_v5_and_query_v5(self):
        self.assertIn("v5", runner.PACK_POLICY_CHOICES)
        self.assertIn("v3", runner.QUERY_STRATEGY_CHOICES)
        self.assertIn("v4", runner.QUERY_STRATEGY_CHOICES)
        self.assertIn("v5", runner.QUERY_STRATEGY_CHOICES)

    def test_consensus_priority_recovers_deep_cross_lane_file(self):
        wrong = runner.Candidate(
            "api/queries.go", 1, 20, "PullRequest", "Struct", "number url"
        )
        consumer = runner.Candidate(
            "pkg/cmd/pr/view/view.go",
            103,
            122,
            "printRawPrPreview",
            "Function",
            "non TTY stdout print number url",
            source_rank=52,
        )
        priorities = runner.cross_lane_consensus_priority(
            [wrong, consumer],
            ["non TTY output", "stdout formatter", "number url"],
            {
                wrong.file: ["symptom"],
                consumer.file: ["symptom", "data_flow", "input_transform"],
            },
            line_budget=200,
        )
        self.assertIn(consumer, priorities)
        self.assertNotIn(wrong, priorities)

    def test_consensus_priority_rejects_test_and_single_lane_files(self):
        test = runner.Candidate(
            "tests/test_view.py", 1, 10, "test_view", "Function", "stdout"
        )
        single = runner.Candidate(
            "src/view.py", 1, 10, "view", "Function", "stdout"
        )
        priorities = runner.cross_lane_consensus_priority(
            [test, single],
            ["stdout"],
            {
                test.file: ["symptom", "data_flow"],
                single.file: ["symptom"],
            },
            line_budget=200,
        )
        self.assertEqual(priorities, [])

    def test_causal_closure_adds_compact_same_file_producer(self):
        consumer = runner.Candidate(
            "src/_pytest/skipping.py",
            260,
            305,
            "pytest_runtest_makereport",
            "Function",
            "if item._store.get(skipped_by_mark_key, False):\n    report.longrepr = item.location",
        )
        producer = runner.Candidate(
            "src/_pytest/skipping.py",
            232,
            244,
            "pytest_runtest_setup",
            "Function",
            "item._store[skipped_by_mark_key] = True",
        )
        unrelated = runner.Candidate(
            "src/_pytest/skipping.py",
            100,
            115,
            "evaluate_condition",
            "Function",
            "return condition == expected_condition",
        )
        expanded, events = runner.expand_causal_same_file_producers(
            [consumer], [consumer, producer, unrelated], 200
        )
        self.assertIn(producer, expanded)
        self.assertNotIn(unrelated, expanded)
        self.assertEqual(events[0]["shared_identifiers"], ["skipped_by_mark_key"])

    def test_causal_closure_does_not_cross_files_or_add_comparisons(self):
        consumer = runner.Candidate(
            "src/consumer.py", 1, 10, "consume", "Function", "shared_state_key"
        )
        other_file = runner.Candidate(
            "src/producer.py",
            1,
            10,
            "produce",
            "Function",
            "shared_state_key = True",
        )
        comparison = runner.Candidate(
            "src/consumer.py",
            20,
            30,
            "compare",
            "Function",
            "return shared_state_key == expected_state_key",
        )
        expanded, events = runner.expand_causal_same_file_producers(
            [consumer], [consumer, other_file, comparison], 200
        )
        self.assertEqual(expanded, [consumer])
        self.assertEqual(events, [])

    def test_compact_budget_aware_window_count(self):
        lines = [f"line {n}" for n in range(1, 201)]
        big = runner.Candidate("a.py", 1, 200, "Big", "Class", "\n".join(lines))
        default_windows = runner.compact_oversized_candidates([big], "line", 200)
        self.assertEqual(len(default_windows), 1)  # v1: threshold == budget, kept whole
        v2_windows = runner.compact_oversized_candidates(
            [big], "line", 200,
            oversize_threshold=runner.V2_OVERSIZE_THRESHOLD,
            max_windows=None,
            scorer=runner.candidate_score_v2,
        )
        self.assertGreater(len(v2_windows), 2)
        self.assertTrue(all(item.line_count <= 40 for item in v2_windows))

    def test_bm25_window_ranker_prefers_deep_implementation_branch(self):
        doc_lines = [
            "mincnt controls whether a hexbin cell is displayed."
        ] + [f"parameter documentation line {index}" for index in range(39)]
        code_lines = [f"value_{index} = source[{index}]" for index in range(34)] + [
            "if C is None:",
            "    accum[accum < mincnt] = nan",
            "else:",
            "    mincnt = 0 if mincnt is None else mincnt",
            "    reduced = reduce_C_function(vals)",
            "    keep = len(vals) >= mincnt",
        ]
        symbol = runner.Candidate(
            "axes.py", 100, 179, "Axes::hexbin", "Method",
            "\n".join([*doc_lines, *code_lines]),
        )
        windows = runner.compact_oversized_candidates(
            [symbol],
            "hexbin mincnt C reduce_C_function len vals",
            line_budget=80,
            window_lines=40,
            overlap_lines=0,
            oversize_threshold=20,
            max_windows=2,
            scorer=runner.candidate_score_v2,
            window_ranker=runner.rank_windows_bm25,
        )
        self.assertEqual(windows[0].start, 140)
        self.assertIn("reduce_C_function", windows[0].content)

    def test_stack_frame_queries_prefer_deep_repo_local_frames(self):
        issue = (
            'Traceback (most recent call last):\n'
            '  File "/usr/local/lib/python3.10/site-packages/click/core.py", line 1130, in __call__\n'
            '    return self.main(*args, **kwargs)\n'
            '  File "/home/atlas/autogpt/agent/agent.py", line 94, in start_interaction_loop\n'
            '    assistant_reply = chat_with_ai(\n'
            '  File "/home/atlas/autogpt/llm/chat.py", line 166, in chat_with_ai\n'
            '    agent.summary_memory = update_running_summary(\n'
        )
        queries = runner.stack_frame_queries(issue)
        self.assertEqual(queries[0], "chat_with_ai")
        self.assertIn("chat", queries)
        self.assertNotIn("__call__", queries)
        self.assertNotIn("core", queries)

    def test_strip_issue_boilerplate_removes_template_noise(self):
        issue = (
            "Title line\r\n"
            "### DO NOT REMOVE OR SKIP THE ISSUE TEMPLATE\r\n"
            "- [X] I understand that I will be blocked\r\n"
            "<details><summary>versions</summary>python 3.11</details>\r\n"
            "<!-- hidden -->\r\n"
            "### Actual behavior\r\n"
            "the download is not simulated\r\n"
        )
        cleaned = runner.strip_issue_boilerplate(issue)
        self.assertIn("Actual behavior", cleaned)
        self.assertIn("not simulated", cleaned)
        self.assertNotIn("DO NOT REMOVE", cleaned)
        self.assertNotIn("I will be blocked", cleaned)
        self.assertNotIn("versions", cleaned)
        self.assertNotIn("hidden", cleaned)

    def test_inline_code_identifiers_recovers_lowercase_symbol_names(self):
        issue = (
            "Inconsistent `hexbin` behavior for `mincnt` when `C` is supplied. "
            "The branch calls `reduce_C_function(vals)`.\n"
            "```python\nimport unrelated_module\n```"
        )
        identifiers = runner.inline_code_identifiers(issue)
        self.assertEqual(identifiers[:3], ["hexbin", "mincnt", "reduce_C_function"])
        self.assertNotIn("unrelated_module", identifiers)

    def test_explicit_issue_queries_recovers_cli_flags_and_paths(self):
        issue = (
            "Add --size-hint for stdin in programs/zstdcli.c while keeping "
            "`reduce_C_function` behavior."
        )
        self.assertEqual(
            runner.explicit_issue_queries(issue),
            ["reduce_C_function", "--size-hint", "programs/zstdcli.c"],
        )

    def test_bounded_exact_queries_rejects_generated_payloads(self):
        generated = "N4Igxg9gdgLgprEAuEIA0IIAcYEtoDOyoAhgE5kQDuACuQkSiQG4S4Am6IJBMy"
        self.assertEqual(
            runner.bounded_exact_queries([generated, "endOfLine", "endOfLine"]),
            ["endOfLine"],
        )

    def test_compact_ranking_query_drops_template_and_long_reproduction(self):
        issue = (
            "Hexbin mincnt differs with C\n"
            "<!-- issue template instructions -->\n"
            "The behavior is inside `hexbin` and `mincnt`.\n"
            + "reproduction filler " * 200
        )
        query = runner.compact_ranking_query(
            issue,
            planned_queries=[issue],
            exact_queries=["reduce_C_function"],
        )
        self.assertIn("Hexbin mincnt differs with C", query)
        self.assertIn("hexbin", query)
        self.assertIn("reduce_C_function", query)
        self.assertNotIn("issue template instructions", query)
        self.assertLess(len(query), 400)

    def test_legacy_head_query_preserves_precise_issue_vocabulary(self):
        issue = (
            "non-TTY scriptable output of pull request commands missing number and URL fields\n"
            "<!-- issue template instructions -->\n"
            + "reproduction filler " * 100
        )
        query = runner.legacy_head_query(issue)
        self.assertTrue(query.startswith("non-TTY scriptable output"))
        self.assertIn("missing number and URL fields", query)
        self.assertNotIn("issue template instructions", query)
        self.assertLessEqual(len(query), 700)

    def test_stemmed_title_query_strips_light_suffixes(self):
        cleaned = "Restrict mutable tuple recovery\n\nbody text here"
        self.assertEqual(
            runner.stemmed_title_query(cleaned), "restrict mutable tuple recover"
        )

    def test_synthesize_queries_v2_keeps_head_lane_and_drops_mentions(self):
        issue = (
            "configurationDefaults contribution changes JSON auto-complete\n"
            "Reported originally by @JacksonKearl where GitLens breaks things.\n"
        )
        planned, exact, _ = runner.synthesize_queries_v2(issue)
        self.assertEqual(planned[0], issue[:700])
        self.assertIn("configurationDefaults", exact)
        self.assertNotIn("JacksonKearl", exact)

    def test_synthesize_queries_v2_is_superset_of_v1_identifiers(self):
        issue = (
            "useAutocomplete looses focus\n\n### Details\n"
            + "word " * 300
            + "\nlater section mentions handleOptionClick and prepareRequest here"
        )
        _, exact, _ = runner.synthesize_queries_v2(issue)
        for token in runner.identifier_queries(issue[:700]):
            self.assertIn(token, exact)

    def test_synthesize_queries_v2_adds_stemmed_lane_for_prose_issues(self):
        issue = "Restrict mutable tuple recovery\n\nRecovery of tuples is now forbidden."
        planned, exact, _ = runner.synthesize_queries_v2(issue)
        self.assertEqual(exact, [])
        self.assertIn("restrict mutable tuple recover", planned)

    def test_synthesize_queries_v2_skips_log_line_identifiers_past_head(self):
        # Log dumps live beyond the 700-char head lane in real issues; the
        # full-text extraction must skip them while head-lane identifiers
        # keep v1 parity via the union.
        issue = (
            "downloader bug title\n\nreal_download_flag breaks here\n"
            + "filler words " * 60
            + "\n```\n[debug] Loading youtube-nsig.7d1f7724 from cache\n"
            "[youtube] vYdxycJ0vBBgWEBA_9: Downloading webpage\n```\n"
        )
        self.assertGreater(issue.find("[debug]"), 700)
        _, exact, _ = runner.synthesize_queries_v2(issue)
        self.assertIn("real_download_flag", exact)
        self.assertNotIn("vYdxycJ0vBBgWEBA_9", exact)

    def test_retrieve_instance_defaults_preserve_v1_flags(self):
        import inspect

        signature = inspect.signature(runner.retrieve_instance)
        self.assertEqual(
            signature.parameters["selector_policy"].default,
            runner.DEFAULT_SELECTOR_POLICY,
        )
        self.assertEqual(signature.parameters["pack_policy"].default, "v1")
        self.assertEqual(signature.parameters["query_strategy"].default, "head")
        self.assertEqual(signature.parameters["normalize_issue"].default, False)
        self.assertEqual(
            signature.parameters["search_limit"].default, runner.DEFAULT_SEARCH_LIMIT
        )

    def test_apply_lane_file_floor_never_evicts_the_selectors_own_pick(self):
        # Regression for a real ContextBench forensic finding (cli/cli
        # eb5704a5, clap-rs 0e4346f7 -- both guarded mode): gold files with
        # in_selected=true ended up in_final_prediction=false. Root cause:
        # apply_lane_file_floor's eviction pass only protected
        # exact_anchors/lane_tops, never the candidates the selector itself
        # (`final`, i.e. `chosen`) actually picked -- so a genuine LLM
        # selection could be silently evicted to force in an unrelated
        # lane's top candidate. `final` must be in the protected set at the
        # call site so its own contents are never evictable by this pass.
        selector_pick = runner.Candidate(
            "clap_derive/src/derives/args.rs", 1, 34, "parse_args", "Function"
        )
        # A large low-relevance same-lane-floor candidate that would need to
        # evict something to fit within a tight budget.
        forced_lane_top = runner.Candidate(
            "clap_derive/src/derives/subcommand.rs", 1, 150, "parse_subcommand", "Function"
        )
        chosen = [selector_pick]
        selected, _events = runner.apply_lane_file_floor(
            chosen,
            [forced_lane_top],
            runner.dedupe_candidates([*chosen]),  # protected set includes `final`
            line_budget=150,  # tight enough that fitting both requires an eviction
            query="parse args and subcommands",
        )
        self.assertIn(
            selector_pick,
            selected,
            "apply_lane_file_floor evicted the selector's own pick to force in a "
            "lane-floor candidate -- this is the exact bug behind the cli/cli and "
            "clap-rs ContextBench regressions",
        )

    def test_lane_file_floor_call_site_protects_final(self):
        source = Path(runner.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "dedupe_candidates([*exact_anchors, *lane_tops, *final])", source
        )

    def test_plan_search_queries_falls_back_on_malformed_json_instead_of_crashing(self):
        # Regression for a real ContextBench forensic finding (prettier/prettier,
        # SWE-PolyBench__javascript__maintenance__bugfix__3b6e6f3a): a truncated
        # OpenAI response body ("Unterminated string starting at...") raised
        # json.decoder.JSONDecodeError uncaught, and the instance produced NO
        # prediction at all -- a hard crash from a malformed upstream response,
        # not a real "no queries" case. Must degrade to using the raw issue
        # text as a single query instead of raising.
        def fake_truncated_response(_request, _label, attempts=3):
            return {
                "id": "resp_fake",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                # Deliberately truncated mid-string, matching
                                # the real forensic error shape.
                                "text": '{"symptom_query": "flattened help',
                            }
                        ],
                    }
                ],
                "usage": {},
            }

        original = runner.send_openai_request
        runner.send_openai_request = fake_truncated_response
        had_key = "OPENAI_API_KEY" in os.environ
        prior_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "test-key-not-used-request-is-mocked"
        try:
            queries, audit = runner.plan_search_queries_with_gpt(
                "Don't leak help headings when flattening", "gpt-5"
            )
        finally:
            runner.send_openai_request = original
            if had_key:
                os.environ["OPENAI_API_KEY"] = prior_key
            else:
                del os.environ["OPENAI_API_KEY"]

        self.assertTrue(queries, "must fall back to a non-empty query list, not raise")
        self.assertIn("flatten", queries[0].lower())
        self.assertEqual(audit["model"], "gpt-5")

    def test_plan_search_queries_wraps_json_decode_in_try_except(self):
        source = Path(runner.__file__).read_text(encoding="utf-8")
        self.assertIn("except json.JSONDecodeError:", source)
        self.assertIn("fallback_query = issue.strip()", source)


if __name__ == "__main__":
    unittest.main()
