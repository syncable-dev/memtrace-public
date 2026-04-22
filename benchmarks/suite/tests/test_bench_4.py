"""Tests for Bench #4 — incremental indexing & staleness.

Covers:
 - constants (BENCH_ID, PRIMARY_AXIS)
 - load_edits shape
 - each of the 4 edit kinds applies correctly on a tmp fixture
 - run_with_adapter against a Fake adapter produces the right row shape
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from benchmarks.suite.benches.bench_4_incremental import run as bench_4
from benchmarks.suite.benches.bench_4_incremental.edits import (
    EditError, apply_edit,
)
from benchmarks.suite.contract import (
    Adapter, NotSupported, QueryResult, ReindexReport, SetupReport,
)


# ── constants ────────────────────────────────────────────────────────────────


def test_bench_4_declares_primary_axis():
    assert bench_4.PRIMARY_AXIS == "time_to_queryable_p95"


def test_bench_4_declares_bench_id():
    assert "Bench #4" in bench_4.BENCH_ID
    assert "Incremental" in bench_4.BENCH_ID


def test_bench_4_default_dataset_path_points_at_bench_4_edits():
    p = bench_4.default_dataset_path()
    assert p.name == "bench_4_edits.json"
    assert p.parent.name == "datasets"


# ── dataset ──────────────────────────────────────────────────────────────────


def test_load_edits_returns_non_empty_list():
    edits = bench_4.load_edits()
    assert isinstance(edits, list)
    assert len(edits) > 0


def test_load_edits_schema():
    edits = bench_4.load_edits()
    required = {"id", "action_type", "file", "target_name", "expected_post_state"}
    for e in edits:
        missing = required - set(e.keys())
        assert not missing, f"edit {e.get('id')} missing keys {missing}"
        assert e["action_type"] in {
            "add_symbol", "rename_symbol", "move_symbol", "delete_symbol"
        }


def test_load_edits_respects_override(tmp_path):
    p = tmp_path / "custom.json"
    p.write_text(json.dumps([{"id": "ez", "action_type": "add_symbol",
                               "file": "x.py", "target_name": "foo",
                               "expected_post_state": {"symbol": "foo", "present": True}}]))
    edits = bench_4.load_edits(p)
    assert len(edits) == 1
    assert edits[0]["id"] == "ez"


# ── edit application ────────────────────────────────────────────────────────


@pytest.fixture
def mini_corpus(tmp_path):
    """Tiny 2-file fixture exercised by edit-application tests."""
    root = tmp_path / "mini"
    root.mkdir()
    (root / "alpha.py").write_text(
        "\"\"\"alpha module.\"\"\"\n"
        "\n"
        "def foo() -> int:\n"
        "    return 1\n"
        "\n"
        "\n"
        "def bar(x: int) -> int:\n"
        "    return x + foo()\n"
        "\n"
        "\n"
        "class Widget:\n"
        "    def tick(self) -> int:\n"
        "        return 0\n"
        "\n"
        "    def tock(self) -> int:\n"
        "        return 1\n"
    )
    (root / "beta.py").write_text(
        "\"\"\"beta module.\"\"\"\n"
    )
    return root


def test_apply_add_symbol_appends_function(mini_corpus):
    action = {
        "id": "e_add",
        "action_type": "add_symbol",
        "file": "alpha.py",
        "target_name": "newly_added",
        "expected_post_state": {"symbol": "newly_added", "present": True},
    }
    changed = apply_edit(action, mini_corpus)
    assert len(changed) == 1
    text = (mini_corpus / "alpha.py").read_text()
    assert "def newly_added" in text


def test_apply_rename_symbol_rewrites_def_and_same_file_refs(mini_corpus):
    action = {
        "id": "e_rn",
        "action_type": "rename_symbol",
        "file": "alpha.py",
        "target_name": "foo",
        "new_name": "foo_v2",
        "expected_post_state": {
            "symbol": "foo_v2", "present": True,
            "old_symbol": "foo", "old_present": False,
        },
    }
    changed = apply_edit(action, mini_corpus)
    assert len(changed) == 1
    text = (mini_corpus / "alpha.py").read_text()
    # Definition rewritten.
    assert "def foo_v2()" in text
    assert "def foo(" not in text
    # Same-file reference rewritten.
    assert "foo_v2()" in text


def test_apply_rename_symbol_rewrites_class(mini_corpus):
    action = {
        "id": "e_rc",
        "action_type": "rename_symbol",
        "file": "alpha.py",
        "target_name": "Widget",
        "new_name": "Gadget",
        "expected_post_state": {"symbol": "Gadget", "present": True},
    }
    apply_edit(action, mini_corpus)
    text = (mini_corpus / "alpha.py").read_text()
    assert "class Gadget:" in text
    assert "class Widget" not in text


def test_apply_rename_symbol_raises_on_unknown(mini_corpus):
    action = {
        "id": "e_rx",
        "action_type": "rename_symbol",
        "file": "alpha.py",
        "target_name": "does_not_exist",
        "new_name": "other",
        "expected_post_state": {"symbol": "other", "present": True},
    }
    with pytest.raises(EditError):
        apply_edit(action, mini_corpus)


def test_apply_move_symbol_moves_function_between_files(mini_corpus):
    action = {
        "id": "e_mv",
        "action_type": "move_symbol",
        "file": "alpha.py",
        "target_name": "bar",
        "dest_file": "beta.py",
        "expected_post_state": {
            "symbol": "bar", "file": "beta.py", "present": True,
            "old_file": "alpha.py", "old_present": False,
        },
    }
    changed = apply_edit(action, mini_corpus)
    assert len(changed) == 2
    alpha = (mini_corpus / "alpha.py").read_text()
    beta = (mini_corpus / "beta.py").read_text()
    assert "def bar(" not in alpha
    assert "def bar(" in beta


def test_apply_move_symbol_moves_method_out_of_class(mini_corpus):
    """Methods-as-move-targets: extraction preserves indentation of the source
    block (the block is a method body with 4-space indent). After the move,
    the method disappears from the class."""
    action = {
        "id": "e_mvm",
        "action_type": "move_symbol",
        "file": "alpha.py",
        "target_name": "tock",
        "dest_file": "beta.py",
        "expected_post_state": {"symbol": "tock", "file": "beta.py", "present": True},
    }
    apply_edit(action, mini_corpus)
    alpha = (mini_corpus / "alpha.py").read_text()
    assert "def tock" not in alpha
    # `tick` still present on the class
    assert "def tick" in alpha


def test_apply_delete_symbol_removes_function(mini_corpus):
    action = {
        "id": "e_del",
        "action_type": "delete_symbol",
        "file": "alpha.py",
        "target_name": "bar",
        "expected_post_state": {"symbol": "bar", "present": False},
    }
    changed = apply_edit(action, mini_corpus)
    assert len(changed) == 1
    text = (mini_corpus / "alpha.py").read_text()
    assert "def bar(" not in text
    assert "def foo(" in text  # other symbols untouched


def test_apply_unknown_action_raises():
    with pytest.raises(EditError):
        apply_edit({"id": "x", "action_type": "nope", "file": "f.py",
                    "target_name": "t"}, Path("/tmp"))


# ── runner integration with a Fake adapter ─────────────────────────────────


class FakeIncrementalAdapter(Adapter):
    """Records every call; treats every new symbol as queryable immediately
    and never stale. Used to exercise the runner's plumbing end-to-end."""
    name = "fake-inc"
    description = "test double"
    version = "fake@test"

    def __init__(self) -> None:
        self.reindexed: list[list[Path]] = []
        self.queryable_for: list[str] = []

    def setup(self, corpus) -> SetupReport:
        return SetupReport(indexed_files=0, wall_ms=0.0)

    def teardown(self) -> None:
        pass

    def query_symbol(self, name: str, limit: int) -> QueryResult:
        # Empty result for all names → no staleness for rename/delete/move.
        return QueryResult(paths=[], latency_ms=0.1)

    def reindex_paths(self, paths):
        self.reindexed.append(list(paths))
        return ReindexReport(files_reindexed=len(paths), wall_ms=2.0, incremental=True)

    def time_to_queryable(self, name: str, deadline_ms: int) -> float:
        self.queryable_for.append(name)
        return 3.0


def test_run_with_adapter_end_to_end(tmp_path):
    """Apply one edit of each kind on a tiny corpus via the real runner."""
    # Build a mini on-disk corpus.
    root = tmp_path / "mini"
    root.mkdir()
    (root / "a.py").write_text("def hello():\n    return 1\n")
    (root / "b.py").write_text("")

    @dataclass
    class MiniCorpus:
        path: Path = field(default_factory=lambda: root)
        name: str = "mini"
        @property
        def parent(self) -> Path:
            return self.path.parent

    edits = [
        {"id": "a1", "action_type": "add_symbol", "file": "a.py",
         "target_name": "added_one",
         "expected_post_state": {"symbol": "added_one", "present": True}},
        {"id": "r1", "action_type": "rename_symbol", "file": "a.py",
         "target_name": "hello", "new_name": "hello_v2",
         "expected_post_state": {"symbol": "hello_v2", "present": True}},
        {"id": "m1", "action_type": "move_symbol", "file": "a.py",
         "target_name": "hello_v2", "dest_file": "b.py",
         "expected_post_state": {"symbol": "hello_v2", "file": "b.py", "present": True}},
        {"id": "d1", "action_type": "delete_symbol", "file": "b.py",
         "target_name": "hello_v2",
         "expected_post_state": {"symbol": "hello_v2", "present": False}},
    ]
    out = tmp_path / "out"
    adapter = FakeIncrementalAdapter()
    rows = bench_4.run_with_adapter(
        adapter, edits, out_dir=out, corpus=MiniCorpus(), deadline_ms=1000,
    )
    assert len(rows) == 4
    assert [r.kind for r in rows] == [
        "add_symbol", "rename_symbol", "move_symbol", "delete_symbol"
    ]
    # Every supported edit triggered a reindex call (delete too — the file changed).
    assert len(adapter.reindexed) == 4
    # The fake always returns 3.0 ms for queryable; add/rename/move check that.
    add_row = rows[0]
    assert add_row.reindex_supported is True
    assert add_row.queryable is True
    assert add_row.time_to_queryable_ms == 3.0
    # Delete skips the poll (no "new" symbol).
    del_row = rows[3]
    assert del_row.queryable is True  # trivially — nothing to wait for
    # jsonl written.
    outputs = list(out.glob("*.jsonl"))
    assert len(outputs) == 1


def test_run_with_adapter_records_notsupported_reindex(tmp_path):
    """Adapter returning NotSupported for reindex still produces a row
    with supported=False and a sentinel time_to_queryable."""
    root = tmp_path / "mini"
    root.mkdir()
    (root / "a.py").write_text("def hi(): return 1\n")

    class UnsupportedReindexAdapter(FakeIncrementalAdapter):
        name = "noreindex"
        def reindex_paths(self, paths):
            return NotSupported(reason="test: no reindex")

    @dataclass
    class MiniCorpus:
        path: Path = field(default_factory=lambda: root)
        name: str = "mini"
        @property
        def parent(self) -> Path:
            return self.path.parent

    edits = [{
        "id": "a1", "action_type": "add_symbol", "file": "a.py",
        "target_name": "x", "expected_post_state": {"symbol": "x", "present": True},
    }]
    rows = bench_4.run_with_adapter(
        UnsupportedReindexAdapter(), edits, out_dir=tmp_path / "o",
        corpus=MiniCorpus(), deadline_ms=500,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r.reindex_supported is False
    # Sentinel: deadline + 1
    assert r.time_to_queryable_ms == 501.0
    assert r.queryable is False
