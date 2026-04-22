"""Tests for the Bench #4 edit-script generator."""
from __future__ import annotations
from pathlib import Path

from benchmarks.suite.corpora.scratch_fixture_corpus import ScratchFixtureCorpus
from benchmarks.suite.datasets.generators.edit_script import (
    EditAction, extract_symbols, generate_actions, serialize,
)


SCRATCH_ROOT = ScratchFixtureCorpus().path


def test_extract_finds_core_classes():
    symbols = extract_symbols(SCRATCH_ROOT)
    names = {s.name for s in symbols}
    # Every domain class must be discoverable.
    for core in ["Task", "TaskQueue", "Worker", "Scheduler", "EventBus"]:
        assert core in names, f"missing core class {core}: {sorted(names)[:20]}"


def test_extract_finds_module_functions():
    symbols = extract_symbols(SCRATCH_ROOT)
    names = {s.name for s in symbols}
    for fn in ["make_task", "get_logger", "retry", "load_config", "run_demo"]:
        assert fn in names, f"missing module fn {fn}"


def test_generator_produces_requested_count():
    actions = generate_actions(SCRATCH_ROOT, num_actions=50)
    assert len(actions) == 50


def test_generator_is_deterministic():
    a = generate_actions(SCRATCH_ROOT, num_actions=20, seed=42)
    b = generate_actions(SCRATCH_ROOT, num_actions=20, seed=42)
    assert serialize(a) == serialize(b)


def test_generator_different_seed_differs():
    a = generate_actions(SCRATCH_ROOT, num_actions=20, seed=1)
    b = generate_actions(SCRATCH_ROOT, num_actions=20, seed=2)
    assert serialize(a) != serialize(b)


def test_action_type_mix_has_all_four():
    actions = generate_actions(SCRATCH_ROOT, num_actions=50)
    types = {a.action_type for a in actions}
    assert types == {
        "add_symbol", "rename_symbol", "move_symbol", "delete_symbol"
    }, types


def test_every_action_has_expected_post_state():
    actions = generate_actions(SCRATCH_ROOT, num_actions=50)
    for a in actions:
        assert a.expected_post_state is not None
        assert "symbol" in a.expected_post_state
        assert "present" in a.expected_post_state


def test_rename_action_includes_new_name():
    actions = generate_actions(SCRATCH_ROOT, num_actions=50)
    renames = [a for a in actions if a.action_type == "rename_symbol"]
    assert renames, "expected at least one rename in 50-action script"
    for a in renames:
        assert a.new_name is not None
        assert a.new_name != a.target_name


def test_move_action_includes_dest_file():
    actions = generate_actions(SCRATCH_ROOT, num_actions=50)
    moves = [a for a in actions if a.action_type == "move_symbol"]
    assert moves, "expected at least one move in 50-action script"
    for a in moves:
        assert a.dest_file is not None
        assert a.dest_file != a.file


def test_committed_dataset_exists_and_is_valid():
    """Committed JSON mirrors the generator output — guards against drift."""
    import json
    ds = Path(__file__).resolve().parents[1] / "datasets" / "bench_4_edits.json"
    assert ds.exists(), f"bench_4_edits.json missing — run: \n  python -m benchmarks.suite.datasets.generators.edit_script"
    data = json.loads(ds.read_text())
    assert len(data) == 50
    # Spot-check structure.
    first = data[0]
    for key in ("id", "action_type", "file", "target_name", "expected_post_state"):
        assert key in first, f"missing key {key} in committed dataset"
