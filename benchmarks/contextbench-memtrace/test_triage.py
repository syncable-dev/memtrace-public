"""Tests for triage.py.

Unit tests always run. Integration tests pin the manually verified failure
diagnoses for the baseline 16-slice (/tmp/cb-slice16) and the E1 rerun
(/tmp/cb-tune/e1); they skip automatically when those artifacts are absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from triage import (
    GoldFileStatus,
    aggregate,
    budget_sanity_check,
    build_funnel,
    cap_f1_at_budget,
    classify,
    exclusion_hit,
    extract_metrics,
    norm_path,
    run_triage,
    Funnel,
    FALLBACK_EXCLUDED_DIRS,
    FALLBACK_NOISE_PATTERNS,
)

SLICE = Path("/tmp/cb-slice16")
E1 = Path("/tmp/cb-tune/e1")


# --------------------------------------------------------------------------
# Unit tests
# --------------------------------------------------------------------------

def test_norm_path_workspace_prefix():
    assert norm_path("/workspace/cli__cli__0.1/pkg/cmd/run/watch/watch.go") == (
        "pkg/cmd/run/watch/watch.go"
    )


def test_norm_path_run_root_repo_prefix():
    p = "/private/tmp/cb-slice16/runs/x/work/x/repo/astropy/coordinates/attributes.py"
    assert norm_path(p) == "astropy/coordinates/attributes.py"


def test_norm_path_already_relative():
    assert norm_path("src/bytes.rs") == "src/bytes.rs"


def test_norm_path_gold_with_repo_dir_segment():
    # gh cli has a legitimate pkg/cmd/repo/ directory: the workspace form must
    # win and the in-repo repo/ segment must survive (regression: splitting on
    # ANY '/repo/' yielded 'list/list.go', hiding the pkg/ exclusion evidence).
    assert norm_path("/workspace/cli__cli__0.1/pkg/cmd/repo/list/list.go") == (
        "pkg/cmd/repo/list/list.go"
    )


def test_norm_path_run_root_with_repo_dir_segment():
    p = "/tmp/x/runs/s/work/s/repo/internal/repo/list/list.go"
    assert norm_path(p) == "internal/repo/list/list.go"


def test_norm_path_relative_repo_segment_untouched():
    assert norm_path("pkg/cmd/repo/list/list.go") == "pkg/cmd/repo/list/list.go"


def test_exclusion_hit_dir_segment():
    hit = exclusion_hit(
        "pkg/cmd/run/watch/watch.go", FALLBACK_EXCLUDED_DIRS, FALLBACK_NOISE_PATTERNS
    )
    assert hit == "dir:pkg/"


def test_exclusion_hit_noise_pattern():
    hit = exclusion_hit(
        "crates/foo/benchmarks/big.json", FALLBACK_EXCLUDED_DIRS, FALLBACK_NOISE_PATTERNS
    )
    assert hit is not None and "benchmarks" in hit


def test_exclusion_hit_clean_path():
    assert (
        exclusion_hit(
            "src/vs/platform/configuration/common/configurationRegistry.ts",
            FALLBACK_EXCLUDED_DIRS,
            FALLBACK_NOISE_PATTERNS,
        )
        is None
    )


def test_cap_f1_math():
    assert cap_f1_at_budget(80, 80) == 1.0
    assert cap_f1_at_budget(40, 80) == 1.0
    assert abs(cap_f1_at_budget(1088, 80) - 2 * 80 / 1168) < 1e-9


def _metrics(line_r=0.0, line_p=0.0, file_r=0.0, file_p=0.0, gold=100):
    return extract_metrics(
        {
            "final": {
                "file": {"coverage": file_r, "precision": file_p, "gold_size": 1, "pred_size": 1},
                "symbol": {"coverage": 0, "precision": 0, "gold_size": 1, "pred_size": 0},
                "span": {"coverage": 0, "precision": 0, "gold_size": 1, "pred_size": 0},
                "line": {"coverage": line_r, "precision": line_p, "gold_size": gold, "pred_size": 80},
            }
        }
    )


def _classify(statuses, metrics, has_prediction=True, gold_files=None, **kw):
    funnel = Funnel(statuses=statuses, audit_found=True)
    gold_files = gold_files or {s.file: s.gold_lines for s in statuses}
    return classify(
        "Fam__python__maintenance__bugfix__deadbeef",
        {"repo": "o/r", "language": "python", "files": gold_files},
        funnel,
        metrics,
        has_prediction,
        budget=kw.get("budget", 80),
        good_threshold=kw.get("good_threshold", 0.5),
    )


def test_classify_selection_when_gold_in_pool_not_final():
    st = GoldFileStatus(file="a.py", gold_lines=50, best_rank=1, best_lane="searches", in_pool=True)
    rec = _classify([st], _metrics(gold=50))
    assert rec["primary"] == "SELECTION"


def test_classify_selected_but_dropped_flag():
    st = GoldFileStatus(
        file="a.py", gold_lines=50, best_rank=1, best_lane="searches",
        in_pool=True, in_selected=True,
    )
    rec = _classify([st], _metrics(gold=50))
    assert rec["primary"] == "SELECTION"
    assert rec["sub_cause"] == "dropped_after_selection"


def test_classify_retrieval_with_indexing_secondary_for_excluded_dir():
    st = GoldFileStatus(file="pkg/x/y.go", gold_lines=200, excluded_pattern="dir:pkg/")
    rec = _classify([st], _metrics(gold=200))
    assert rec["primary"] == "RETRIEVAL"
    assert rec["secondary"] == "INDEXING"
    assert "pkg" in rec["sub_cause"]


def test_classify_budget_when_capped_and_gold_selected():
    st = GoldFileStatus(file="a.rs", gold_lines=1000, best_rank=1, in_pool=True,
                        in_selected=True, in_final_prediction=True)
    rec = _classify([st], _metrics(line_r=0.05, line_p=0.6, file_r=1, file_p=1, gold=1000))
    assert rec["primary"] == "BUDGET"
    assert rec["budget"]["budget_capped"] is True


def test_classify_not_budget_when_nothing_selected_even_if_capped():
    st = GoldFileStatus(file="a.go", gold_lines=1000)
    rec = _classify([st], _metrics(gold=1000))
    assert rec["primary"] == "RETRIEVAL"


def test_classify_span_when_right_file_wrong_lines():
    st = GoldFileStatus(file="a.py", gold_lines=60, best_rank=1, in_pool=True,
                        in_selected=True, in_final_prediction=True)
    rec = _classify([st], _metrics(line_r=0.05, line_p=0.1, file_r=1, file_p=1, gold=60))
    assert rec["primary"] == "SPAN"


def test_classify_none_when_good():
    st = GoldFileStatus(file="a.py", gold_lines=40, best_rank=1, in_pool=True,
                        in_selected=True, in_final_prediction=True)
    rec = _classify([st], _metrics(line_r=0.9, line_p=0.9, file_r=1, file_p=1, gold=40))
    assert rec["primary"] == "NONE"


def test_classify_indexing_on_missing_prediction():
    st = GoldFileStatus(file="a.ts", gold_lines=60)
    rec = _classify([st], _metrics(gold=60), has_prediction=False)
    assert rec["primary"] == "INDEXING"
    assert rec["sub_cause"] == "hang_or_empty_prediction"


def test_graph_context_lane_is_scanned():
    audit = {
        "searches": [],
        "refinement_searches": [],
        "graph_contexts": [
            {
                "anchor": {"file_path": "/x/runs/s/work/s/repo/pkg/a/y.go", "name": "F"},
                "context": {"callers": [{"symbol": {"file": "detector/detector.go"}}]},
            }
        ],
        "selector": {"candidate_pool": [{"id": 1, "file": "other.go"}]},
    }
    funnel = build_funnel(audit, {"detector/detector.go": 30, "pkg/a/y.go": 10}, set())
    by_file = {s.file: s for s in funnel.statuses}
    assert by_file["detector/detector.go"].in_graph_context
    assert by_file["detector/detector.go"].bucket() == "retrieval_graph_only"
    assert by_file["detector/detector.go"].best_lane == "graph_contexts"
    assert by_file["pkg/a/y.go"].in_graph_context


def test_graph_context_suppresses_excluded_dir_indexing_secondary():
    # A graph-context appearance PROVES the file was indexed: the excluded-dir
    # INDEXING secondary must not fire even when the path matches a pattern.
    st = GoldFileStatus(
        file="pkg/x/y.go", gold_lines=200, in_graph_context=True,
        best_lane="graph_contexts", excluded_pattern="dir:pkg/",
    )
    rec = _classify([st], _metrics(gold=200))
    assert rec["primary"] == "RETRIEVAL"
    assert rec["secondary"] != "INDEXING"
    assert "graph context" in rec["explanation"]


def test_budget_sanity_check_flags_impossible_f1():
    st = GoldFileStatus(file="a.rs", gold_lines=1000, best_rank=1, in_pool=True,
                        in_selected=True, in_final_prediction=True)
    # line F1 0.30 with an 80-line budget on 1000 gold lines (cap ~0.148) is
    # mathematically impossible — the run must have used a bigger budget.
    rec = _classify([st], _metrics(line_r=0.19, line_p=0.75, file_r=1, file_p=1, gold=1000))
    check = budget_sanity_check([rec], budget=80, max_pred_lines=199)
    assert check["ok"] is False
    assert rec["instance_id"] in check["impossible_achieved_over_cap_instances"]
    assert check["max_unique_predicted_lines"] == 199
    # ...and the SAME record is fine when triaged at the run's real budget.
    rec200 = _classify(
        [st], _metrics(line_r=0.19, line_p=0.75, file_r=1, file_p=1, gold=1000),
        budget=200,
    )
    check200 = budget_sanity_check([rec200], budget=200, max_pred_lines=199)
    assert check200["ok"] is True


def test_budget_lever_headroom_bounded_by_achievement():
    st = GoldFileStatus(file="a.rs", gold_lines=1000, best_rank=1, in_pool=True,
                        in_selected=True, in_final_prediction=True)
    # BUDGET-primary task achieving 0% of its cap books ~0 BUDGET headroom;
    # its residual goes to the secondary layer instead.
    rec = _classify([st], _metrics(line_r=0.0, line_p=0.0, file_r=1, file_p=1, gold=1000))
    if rec["primary"] != "BUDGET":
        pytest.skip("classification changed; lever test not applicable")
    summary = aggregate([rec], 0.5)
    budget_lever = next(l for l in summary["next_lever_ranking"] if l["layer"] == "BUDGET")
    assert budget_lever["est_line_f1_headroom_sum"] == 0.0


def test_aggregate_shapes():
    st = GoldFileStatus(file="a.py", gold_lines=40, best_rank=1, in_pool=True,
                        in_final_prediction=True)
    rec = _classify([st], _metrics(line_r=0.9, line_p=0.9, file_r=1, file_p=1, gold=40))
    summary = aggregate([rec], 0.5)
    assert summary["num_tasks"] == 1
    assert summary["layer_counts"] == {"NONE": 1}
    assert "next_lever_ranking" in summary


# --------------------------------------------------------------------------
# Integration: pinned manual ground truth on the baseline slice
# --------------------------------------------------------------------------

needs_slice = pytest.mark.skipif(
    not (SLICE / "results.jsonl").exists(), reason="/tmp/cb-slice16 not present"
)
needs_e1 = pytest.mark.skipif(
    not (E1 / "results.jsonl").exists(), reason="/tmp/cb-tune/e1 not present"
)


@pytest.fixture(scope="module")
def baseline_records():
    if not (SLICE / "results.jsonl").exists():
        pytest.skip("/tmp/cb-slice16 not present")
    records, summary = run_triage(
        SLICE / "gold_train_with_repo_url.parquet",
        SLICE / "predictions.jsonl",
        SLICE / "results.jsonl",
        SLICE / "predictions-audit",
    )
    return {r["instance_id"]: r for r in records}, summary


GT_BASELINE = {
    # manually diagnosed: gold in pool, selector/final assembly dropped it
    "SWE-Bench-Verified__python__maintenance__bugfix__1d90db61": "SELECTION",  # astropy
    "SWE-Bench-Verified__python__maintenance__bugfix__ec975b6c": "SELECTION",  # xarray
    "SWE-PolyBench__python__maintenance__bugfix__ed58622a": "SELECTION",  # AutoGPT
    # manually diagnosed: gold file never in any lane top-20
    "SWE-PolyBench__typescript__maintenance__bugfix__7d106697": "RETRIEVAL",  # vscode
    "Multi-SWE-Bench__go__maintenance__bugfix__4606de0b": "RETRIEVAL",  # gh cli
    # manually diagnosed: capped by the 80-line budget
    "Multi-SWE-Bench__rust__maintenance__bugfix__03fdad45": "BUDGET",  # bytes
    "Multi-SWE-Bench__c__maintenance__bugfix__0f94ce4d": "BUDGET",  # ponyc
    "SWE-Bench-Pro__python__maintenance__bugfix__31f13b61": "BUDGET",  # ansible
    # scored well
    "SWE-Bench-Verified__python__maintenance__bugfix__3d378646": "NONE",  # sklearn
    "SWE-Bench-Verified__python__maintenance__bugfix__3f745086": "NONE",  # django
}


@needs_slice
@pytest.mark.parametrize("instance_id,expected", sorted(GT_BASELINE.items()))
def test_baseline_ground_truth(baseline_records, instance_id, expected):
    records, _ = baseline_records
    assert records[instance_id]["primary"] == expected, records[instance_id]["explanation"]


@needs_slice
def test_baseline_cli_flags_excluded_dir_indexing_secondary(baseline_records):
    records, _ = baseline_records
    rec = records["Multi-SWE-Bench__go__maintenance__bugfix__4606de0b"]
    assert rec["secondary"] == "INDEXING"
    assert "pkg" in (rec["sub_cause"] or "")


@needs_slice
def test_baseline_astropy_dropped_after_selection(baseline_records):
    records, _ = baseline_records
    rec = records["SWE-Bench-Verified__python__maintenance__bugfix__1d90db61"]
    # gold was in the pool AND in selected ids, then dropped in final assembly
    assert rec["sub_cause"] == "dropped_after_selection"
    gf = rec["gold_files"][0]
    assert gf["in_pool"] and gf["in_selected"] and not gf["in_final_prediction"]


@needs_slice
def test_baseline_xarray_multifile_gold_collapsed(baseline_records):
    records, _ = baseline_records
    rec = records["SWE-Bench-Verified__python__maintenance__bugfix__ec975b6c"]
    assert len(rec["gold_files"]) == 4
    assert all(g["in_pool"] for g in rec["gold_files"])
    assert not any(g["in_final_prediction"] for g in rec["gold_files"])


@needs_slice
def test_baseline_macro_matches_known_value(baseline_records):
    _, summary = baseline_records
    assert abs(summary["macro_line_f1"] - 0.176) < 0.005


@needs_slice
def test_baseline_runs_layout_includes_hung_prettier():
    # The runs/ layout is the task universe the AWS pipeline must triage from:
    # the prettier instance hung (run dir present, no audit, empty prediction)
    # and must appear as INDEXING — the flat predictions-audit/ layout has no
    # entry for it at all (that is exactly how the '15 tasks' report happened).
    records, summary = run_triage(
        SLICE / "gold_train_with_repo_url.parquet",
        SLICE / "predictions.jsonl",
        SLICE / "results.jsonl",
        SLICE / "runs",
    )
    assert summary["num_tasks"] == 16
    by_id = {r["instance_id"]: r for r in records}
    prettier = by_id["SWE-PolyBench__javascript__maintenance__bugfix__c017c9ba"]
    assert prettier["primary"] == "INDEXING"
    assert prettier["sub_cause"] == "hang_or_empty_prediction"
    assert abs(summary["macro_line_f1"] - 0.1651) < 0.005


@needs_e1
def test_e1_vscode_hang_is_indexing():
    # E1 ran with --line-budget 200 (model 'memtrace-e1-line200-locked-plans');
    # triaging it at the default 80 produced impossible achieved_over_cap
    # values and misattributed 4/15 primaries to BUDGET.
    records, summary = run_triage(
        SLICE / "gold_train_with_repo_url.parquet",
        E1 / "predictions.jsonl",
        E1 / "results.jsonl",
        E1 / "runs",
        budget=200,
    )
    by_id = {r["instance_id"]: r for r in records}
    rec = by_id["SWE-PolyBench__typescript__maintenance__bugfix__7d106697"]
    assert rec["primary"] == "INDEXING"
    assert rec["sub_cause"] == "hang_or_empty_prediction"
    assert len(records) == 15  # hung instance still triaged
    assert summary["budget_check"]["ok"], summary["budget_check"]
    for r in records:
        over = r["budget"]["achieved_over_cap"]
        assert over is None or over <= 1.0 + 5e-3, (r["instance_id"], over)


@needs_e1
def test_e1_wrong_budget_is_flagged_not_silent():
    # Re-running E1 at the WRONG budget (80) must be loudly detected.
    _, summary = run_triage(
        SLICE / "gold_train_with_repo_url.parquet",
        E1 / "predictions.jsonl",
        E1 / "results.jsonl",
        E1 / "runs",
        budget=80,
    )
    check = summary["budget_check"]
    assert check["ok"] is False
    assert check["impossible_achieved_over_cap_instances"]
    assert check["max_unique_predicted_lines"] > 80
