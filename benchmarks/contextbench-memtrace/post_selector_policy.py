#!/usr/bin/env python3
"""Dependency-free post-selector policy shared by live and sealed replay.

The policy consumes only a prediction and its retrieval audit.  It never
reads repository source, benchmark labels, evaluator output, AWS state, or
other tasks.  Keeping the projection here makes a live opt-in run and the
sealed counterfactual replay execute the same implementation.
"""

from __future__ import annotations

from collections import defaultdict
import copy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, Mapping, Sequence


POLICY_VERSION = "offline-packing-v2"
POLICY_V3_VERSION = "offline-packing-v3"
POLICY_V4_VERSION = "offline-packing-v4"
PROJECTION_VERSION = "contextbench-trajectory-spans-v1"
POST_SELECTOR_POLICY_CHOICES = (
    "off", POLICY_VERSION, POLICY_V3_VERSION, POLICY_V4_VERSION,
)
POLICY_FINGERPRINT_SCHEMA_VERSION = 1
PROHIBITED_INPUT_KEYS = {
    "evaluation",
    "gold",
    "gold_context",
    "ground_truth",
    "leaderboard",
    "metrics",
}
TESTISH_PARTS = {
    "doc",
    "docs",
    "example",
    "examples",
    "fixture",
    "fixtures",
    "generated",
    "node_modules",
    "test",
    "tests",
    "testing",
    "third_party",
    "vendor",
}
CONFIG_SUFFIXES = {
    ".cfg",
    ".ini",
    ".json",
    ".lock",
    ".md",
    ".rst",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
RUNTIME_AUDIT_LIST_FIELDS = (
    "search_queries",
    "searches",
    "refinement_searches",
    "graph_contexts",
    "final_candidates",
    "exact_identifier_candidates",
    "lane_floor",
)


class PostSelectorPolicyError(ValueError):
    """The post-selector input violates the fixed policy contract."""


@dataclass(frozen=True)
class PackingPolicy:
    """Fixed, task-agnostic post-selector policy used for both sides."""

    novel_line_numerator: int = 1
    novel_line_denominator: int = 4
    support_max_new_files: int = 2
    support_max_new_candidates: int = 2
    minimum_independent_query_families: int = 2
    same_scope_closure_max_candidates: int = 2
    version: str = POLICY_VERSION
    support_selected_file_first: bool = False
    protect_causal_closure: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "version": self.version,
            "selector_choices_are_protected": True,
            "empty_replay_falls_back_to_original_nonempty_context": True,
            "same_scope_closure_first": True,
            "same_scope_closure_max_candidates": self.same_scope_closure_max_candidates,
            "support_novel_line_cap": {
                "numerator": self.novel_line_numerator,
                "denominator": self.novel_line_denominator,
                "percent": 25,
            },
            "support_max_new_files": self.support_max_new_files,
            "support_max_new_candidates": self.support_max_new_candidates,
            "minimum_independent_query_families": (
                self.minimum_independent_query_families
            ),
            "exact_and_lane_share_support_budget": True,
            "refinement_and_graph_inherit_root_query_family": True,
            "exact_identifier_alone_is_not_corroboration": True,
            "mandatory_support_rejects_test_docs_config_generated": True,
            "ranking": [
                "independent_query_family_count_desc",
                "reciprocal_rank_score_desc",
                "best_rank_asc",
                "exact_source_first",
                "novel_line_cost_asc",
                "path_start_end_asc",
            ],
            "task_specific_overrides": False,
            "gold_or_evaluator_inputs": False,
        }
        if self.support_selected_file_first:
            payload["support_selected_file_first"] = True
            payload["ranking"].insert(0, "selected_file_first")
        if self.protect_causal_closure:
            payload["causal_closure_is_protected"] = True
        return payload

    def novel_line_cap(self, line_budget: int) -> int:
        return line_budget * self.novel_line_numerator // self.novel_line_denominator


POLICY = PackingPolicy()
POLICY_V3 = PackingPolicy(
    version=POLICY_V3_VERSION,
    support_selected_file_first=True,
    protect_causal_closure=True,
)
POLICY_V4 = PackingPolicy(
    version=POLICY_V4_VERSION,
    protect_causal_closure=True,
)
POLICIES = {
    POLICY_VERSION: POLICY,
    POLICY_V3_VERSION: POLICY_V3,
    POLICY_V4_VERSION: POLICY_V4,
}


@dataclass(frozen=True, order=True)
class Span:
    file: str
    start: int
    end: int
    name: str = ""
    kind: str = ""

    @property
    def key(self) -> tuple[str, int, int]:
        return self.file, self.start, self.end

    @property
    def lines(self) -> set[tuple[str, int]]:
        return {(self.file, line) for line in range(self.start, self.end + 1)}

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "start": self.start,
            "end": self.end,
            "name": self.name,
            "kind": self.kind,
        }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_prediction_sha256(prediction: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(prediction)).hexdigest()


def implementation_sha256(source_path: Path | None = None) -> str:
    path = source_path or Path(__file__)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def policy_manifest(
    *,
    line_budget: int,
    source_sha256: str | None = None,
    policy: PackingPolicy = POLICY,
) -> dict[str, Any]:
    if line_budget < 1:
        raise PostSelectorPolicyError("line budget must be positive")
    implementation_hash = source_sha256 or implementation_sha256()
    if not re.fullmatch(r"[0-9a-f]{64}", implementation_hash):
        raise PostSelectorPolicyError("implementation source hash must be sha256 hex")
    return {
        "schema_version": POLICY_FINGERPRINT_SCHEMA_VERSION,
        "policy": policy.as_dict(),
        "runtime_parameters": {
            "line_budget": line_budget,
            "strict_runtime_audit": True,
        },
        "projection": {
            "version": PROJECTION_VERSION,
            "mutated_fields": [
                "traj_data.pred_files",
                "traj_data.pred_spans",
            ],
            "preserved_fields": [
                "instance_id",
                "model_patch",
                "traj_data.pred_steps",
                "traj_data.pred_symbols",
            ],
        },
        "implementation": {
            "module": Path(__file__).name,
            "sha256": implementation_hash,
        },
    }


def policy_fingerprint(
    *,
    line_budget: int,
    source_sha256: str | None = None,
    policy: PackingPolicy = POLICY,
) -> str:
    manifest = policy_manifest(
        line_budget=line_budget,
        source_sha256=source_sha256,
        policy=policy,
    )
    return hashlib.sha256(_canonical_json(manifest)).hexdigest()


def prediction_context(prediction: Mapping[str, Any]) -> dict[str, Any]:
    trajectory = prediction.get("traj_data")
    if not isinstance(trajectory, Mapping):
        return {"pred_files": [], "pred_spans": {}, "pred_symbols": {}}
    return copy.deepcopy(
        {
            "pred_files": trajectory.get("pred_files", []),
            "pred_spans": trajectory.get("pred_spans", {}),
            "pred_symbols": trajectory.get("pred_symbols", {}),
        }
    )


def validate_runtime_audit(audit: Mapping[str, Any]) -> None:
    selector = audit.get("selector")
    if not isinstance(selector, Mapping):
        raise PostSelectorPolicyError("runtime audit has no selector decision")
    for key in ("candidate_pool", "selected_candidate_ids"):
        if not isinstance(selector.get(key), list):
            raise PostSelectorPolicyError(
                f"runtime selector audit has no complete {key} list"
            )
    for key in RUNTIME_AUDIT_LIST_FIELDS:
        if not isinstance(audit.get(key), list):
            raise PostSelectorPolicyError(
                f"runtime retrieval audit has no complete {key} list"
            )
    for key in (
        "final_candidates",
        "exact_identifier_candidates",
        "lane_floor",
    ):
        malformed = [
            index
            for index, value in enumerate(audit[key])
            if not isinstance(value, Mapping)
        ]
        if malformed:
            raise PostSelectorPolicyError(
                f"runtime retrieval audit has malformed {key} entries: {malformed}"
            )
    if not all(
        isinstance(value, str) and value.strip()
        for value in audit["search_queries"]
    ):
        raise PostSelectorPolicyError(
            "runtime retrieval audit has malformed search query entries"
        )
    for key in ("searches", "refinement_searches"):
        for index, value in enumerate(audit[key]):
            if not isinstance(value, Mapping) or not isinstance(
                value.get("results"), list
            ):
                raise PostSelectorPolicyError(
                    f"runtime retrieval audit has malformed {key} entry: {index}"
                )
            if any(not isinstance(result, Mapping) for result in value["results"]):
                raise PostSelectorPolicyError(
                    f"runtime retrieval audit has malformed {key} results: {index}"
                )
    for index, value in enumerate(audit["graph_contexts"]):
        if (
            not isinstance(value, Mapping)
            or not isinstance(value.get("anchor"), Mapping)
            or not isinstance(value.get("context"), Mapping)
        ):
            raise PostSelectorPolicyError(
                f"runtime retrieval audit has malformed graph_contexts entry: {index}"
            )
    planner = audit.get("search_planner")
    if not isinstance(planner, Mapping) or not isinstance(
        planner.get("identifier_queries"), list
    ):
        raise PostSelectorPolicyError(
            "runtime retrieval audit has no complete identifier query plan"
        )
    if not all(
        isinstance(value, str) and value.strip()
        for value in planner["identifier_queries"]
    ):
        raise PostSelectorPolicyError(
            "runtime retrieval audit has malformed identifier query entries"
        )


def _canonical_query(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _scope(value: str) -> str:
    normalized = re.sub(r"\s+", "", value or "").replace("#", "::")
    normalized = normalized.replace("/", "::")
    return normalized.casefold()


def _leaf_name(value: str) -> str:
    scope = _scope(value)
    leaf = scope.rsplit("::", 1)[-1]
    return re.sub(r"[^a-z0-9_]", "", leaf)


def _same_scope(left: Span, right: Span) -> bool:
    if left.file != right.file or not _scope(left.name):
        return False
    if _scope(left.name) != _scope(right.name):
        return False
    return left.start <= right.end + 1 and right.start <= left.end + 1


def _overlaps(left: Span, right: Span) -> bool:
    return left.file == right.file and left.start <= right.end and right.start <= left.end


def _production_path(path: str) -> bool:
    pure = PurePosixPath(path)
    lowered_parts = {part.casefold() for part in pure.parts}
    filename = pure.name.casefold()
    stem = pure.stem.casefold()
    if lowered_parts & TESTISH_PARTS:
        return False
    if pure.suffix.casefold() in CONFIG_SUFFIXES:
        return False
    if (
        stem.startswith("test_")
        or stem.endswith("_test")
        or ".test." in filename
        or ".spec." in filename
    ):
        return False
    return True


def _discriminative_name(name: str) -> bool:
    leaf = _leaf_name(name)
    if len(leaf) < 5:
        return False
    raw_leaf = re.split(r"::|#", name or "")[-1]
    return "_" in raw_leaf or bool(re.search(r"[a-z][A-Z]", raw_leaf)) or len(leaf) >= 8


def _safe_relative_file(value: Any, known_files: Iterable[str] = ()) -> str:
    raw = str(value or "").replace("\\", "/")
    candidates = sorted(set(known_files), key=len, reverse=True)
    for candidate in candidates:
        normalized = candidate[2:] if candidate.startswith("./") else candidate
        if raw == normalized or raw.endswith(f"/{normalized}"):
            return normalized
    if "/repo/" in raw:
        raw = raw.rsplit("/repo/", 1)[1]
    if raw.startswith("./"):
        raw = raw[2:]
    pure = PurePosixPath(raw)
    if not raw or pure.is_absolute() or ".." in pure.parts:
        raise PostSelectorPolicyError(
            f"audit contains an unsafe repository-relative path: {value}"
        )
    return str(pure)


def _span_from_mapping(
    value: Mapping[str, Any],
    *,
    known_files: Iterable[str] = (),
) -> Span:
    try:
        start = int(value.get("start", value.get("start_line")))
        end = int(value.get("end", value.get("end_line")))
    except (TypeError, ValueError) as error:
        raise PostSelectorPolicyError(
            f"candidate has invalid line bounds: {value}"
        ) from error
    if start < 1 or end < start:
        raise PostSelectorPolicyError(f"candidate has invalid line bounds: {value}")
    return Span(
        file=_safe_relative_file(
            value.get("file", value.get("file_path")), known_files
        ),
        start=start,
        end=end,
        name=str(value.get("name", value.get("scope_path", "")) or ""),
        kind=str(value.get("kind", "") or ""),
    )


def _deduplicate_spans(spans: Iterable[Span]) -> list[Span]:
    indexed: dict[tuple[str, int, int], Span] = {}
    for span in spans:
        prior = indexed.get(span.key)
        if prior is None or (not prior.name and span.name):
            indexed[span.key] = span
    return list(indexed.values())


def _validate_no_gold_keys(value: Any, label: str, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in PROHIBITED_INPUT_KEYS:
                location = ".".join((*path, str(key)))
                raise PostSelectorPolicyError(
                    f"{label} contains prohibited evaluator/gold key: {location}"
                )
            _validate_no_gold_keys(item, label, (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_no_gold_keys(item, label, (*path, str(index)))


@dataclass
class Evidence:
    families_by_file: dict[str, set[str]]
    ranks_by_file: dict[str, dict[str, int]]

    def families(self, span: Span) -> set[str]:
        return set(self.families_by_file.get(span.file, set()))

    def ranks(self, span: Span) -> list[int]:
        return sorted(self.ranks_by_file.get(span.file, {}).values())


def _known_files(audit: Mapping[str, Any]) -> set[str]:
    files: set[str] = set()
    selector = audit.get("selector")
    sequences: list[Any] = [
        audit.get("final_candidates"),
        audit.get("exact_identifier_candidates"),
        audit.get("lane_floor"),
        audit.get("causal_closure"),
    ]
    if isinstance(selector, dict):
        sequences.append(selector.get("candidate_pool"))
    for sequence in sequences:
        if not isinstance(sequence, list):
            continue
        for value in sequence:
            if not isinstance(value, dict):
                continue
            raw = value.get("file", value.get("file_path"))
            if raw and not Path(str(raw)).is_absolute():
                files.add(_safe_relative_file(raw))
    return files


def _semantic_query_families(audit: Mapping[str, Any]) -> dict[str, str]:
    planner = audit.get("search_planner")
    identifiers = set()
    if isinstance(planner, dict) and isinstance(planner.get("identifier_queries"), list):
        identifiers = {
            _canonical_query(query) for query in planner["identifier_queries"]
        }
    queries = audit.get("search_queries")
    if not isinstance(queries, list):
        return {}
    families: dict[str, str] = {}
    for query in queries:
        canonical = _canonical_query(query)
        if not canonical or canonical in identifiers or canonical in families:
            continue
        families[canonical] = f"root_query_{len(families):02d}"
    return families


def _build_evidence(audit: Mapping[str, Any], known_files: set[str]) -> Evidence:
    query_families = _semantic_query_families(audit)
    families_by_file: dict[str, set[str]] = defaultdict(set)
    ranks_by_file: dict[str, dict[str, int]] = defaultdict(dict)

    def record(file: str, family: str, rank: int) -> None:
        families_by_file[file].add(family)
        prior = ranks_by_file[file].get(family)
        if prior is None or rank < prior:
            ranks_by_file[file][family] = rank

    def ingest(rows: Any) -> None:
        if not isinstance(rows, list):
            return
        for search in rows:
            if not isinstance(search, dict):
                continue
            family = query_families.get(_canonical_query(search.get("query")))
            results = search.get("results")
            if family is None or not isinstance(results, list):
                continue
            for rank, result in enumerate(results, 1):
                if not isinstance(result, dict):
                    continue
                try:
                    file = _safe_relative_file(result.get("file_path"), known_files)
                except PostSelectorPolicyError:
                    continue
                record(file, family, rank)

    ingest(audit.get("searches"))
    ingest(audit.get("refinement_searches"))

    graph_contexts = audit.get("graph_contexts")
    if isinstance(graph_contexts, list):
        for graph in graph_contexts:
            if not isinstance(graph, dict) or not isinstance(graph.get("anchor"), dict):
                continue
            try:
                anchor_file = _safe_relative_file(
                    graph["anchor"].get("file_path"), known_files
                )
            except PostSelectorPolicyError:
                continue
            inherited = set(families_by_file.get(anchor_file, set()))
            if not inherited:
                continue
            context = graph.get("context")
            if not isinstance(context, dict):
                continue
            graph_rows: list[dict[str, Any]] = []
            for key in ("callers", "callees", "contains", "cross_repo_callers"):
                value = context.get(key)
                if isinstance(value, list):
                    graph_rows.extend(item for item in value if isinstance(item, dict))
            value = context.get("symbol")
            if isinstance(value, dict):
                graph_rows.append(value)
            for result in graph_rows:
                try:
                    file = _safe_relative_file(result.get("file_path"), known_files)
                except PostSelectorPolicyError:
                    continue
                for family in sorted(inherited):
                    anchor_rank = ranks_by_file.get(anchor_file, {}).get(family, 10_000)
                    record(file, family, anchor_rank + 1)

    return Evidence(dict(families_by_file), dict(ranks_by_file))


def _resolve_support_candidates(
    audit: Mapping[str, Any],
    pool: Sequence[Span],
    final_candidates: Sequence[Span],
    known_files: set[str],
) -> list[tuple[Span, set[str]]]:
    resolved: dict[tuple[str, int, int], tuple[Span, set[str]]] = {}

    def add(candidate: Span, source: str) -> None:
        prior = resolved.get(candidate.key)
        if prior is None:
            resolved[candidate.key] = (candidate, {source})
        else:
            prior[1].add(source)

    exact_values = audit.get("exact_identifier_candidates")
    if isinstance(exact_values, list):
        for value in exact_values:
            if not isinstance(value, dict):
                continue
            exact = _span_from_mapping(value, known_files=known_files)
            matches = [
                candidate
                for candidate in (*final_candidates, *pool)
                if candidate.file == exact.file
                and (_same_scope(candidate, exact) or _overlaps(candidate, exact))
            ]
            if matches:
                matches.sort(
                    key=lambda candidate: (
                        0 if _same_scope(candidate, exact) else 1,
                        len(candidate.lines),
                        candidate.start,
                        candidate.end,
                    )
                )
                add(matches[0], "exact")
            else:
                add(exact, "exact")

    lane_values = audit.get("lane_floor")
    if isinstance(lane_values, list):
        for value in lane_values:
            if not isinstance(value, dict):
                continue
            lane = _span_from_mapping(value, known_files=known_files)
            matches = [
                candidate
                for candidate in (*final_candidates, *pool)
                if candidate.file == lane.file and _overlaps(candidate, lane)
            ]
            if matches:
                matches.sort(
                    key=lambda candidate: (
                        0 if candidate.key == lane.key else 1,
                        abs(candidate.start - lane.start) + abs(candidate.end - lane.end),
                        len(candidate.lines),
                    )
                )
                add(matches[0], "lane")
            else:
                add(lane, "lane")

    return list(resolved.values())


def _prediction_from_spans(
    original: Mapping[str, Any], spans: Sequence[Span]
) -> dict[str, Any]:
    prediction = copy.deepcopy(dict(original))
    trajectory = prediction.get("traj_data")
    if not isinstance(trajectory, dict):
        trajectory = {}
        prediction["traj_data"] = trajectory
    files: list[str] = []
    pred_spans: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        if span.file not in pred_spans:
            files.append(span.file)
            pred_spans[span.file] = []
        pred_spans[span.file].append(
            {"start": span.start, "end": span.end, "type": "line"}
        )
    trajectory["pred_files"] = files
    trajectory["pred_spans"] = pred_spans
    trajectory.setdefault("pred_symbols", {})
    return prediction


def _prediction_has_context(prediction: Mapping[str, Any]) -> bool:
    trajectory = prediction.get("traj_data")
    if not isinstance(trajectory, Mapping):
        return False
    files = trajectory.get("pred_files")
    if isinstance(files, list) and bool(files):
        return True
    for key in ("pred_spans", "pred_symbols"):
        values = trajectory.get(key)
        if isinstance(values, Mapping) and any(bool(value) for value in values.values()):
            return True
    return False


def replay_prediction(
    prediction: Mapping[str, Any],
    audit: Mapping[str, Any],
    *,
    line_budget: int,
    strict_runtime_audit: bool = False,
    policy: PackingPolicy = POLICY,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replay one task from selector output using only its retrieval audit."""
    if line_budget < 1:
        raise PostSelectorPolicyError("line budget must be positive")
    if not isinstance(prediction, Mapping) or not isinstance(audit, Mapping):
        raise PostSelectorPolicyError("prediction and audit must be objects")
    _validate_no_gold_keys(prediction, "prediction")
    _validate_no_gold_keys(audit, "prediction audit")
    if strict_runtime_audit:
        validate_runtime_audit(audit)

    selector = audit.get("selector")
    if (
        not isinstance(selector, dict)
        or not isinstance(selector.get("candidate_pool"), list)
        or not isinstance(selector.get("selected_candidate_ids"), list)
    ):
        return copy.deepcopy(dict(prediction)), {
            "status": "unreplayable_passthrough",
            "reason": "sealed audit has no complete selector decision",
            "policy_version": policy.version,
            "projection_version": PROJECTION_VERSION,
        }

    known_files = _known_files(audit)
    pool_by_id: dict[str, Span] = {}
    pool: list[Span] = []
    for value in selector["candidate_pool"]:
        if not isinstance(value, dict) or "id" not in value:
            raise PostSelectorPolicyError(
                "selector candidate pool contains a malformed entry"
            )
        span = _span_from_mapping(value, known_files=known_files)
        key = str(value["id"])
        if key in pool_by_id:
            raise PostSelectorPolicyError(
                f"selector candidate pool contains duplicate ID: {key}"
            )
        pool_by_id[key] = span
        pool.append(span)

    selected: list[Span] = []
    selected_ids: list[str] = []
    selected_keys: set[tuple[str, int, int]] = set()
    for value in selector["selected_candidate_ids"]:
        key = str(value)
        if key not in pool_by_id:
            raise PostSelectorPolicyError(
                f"selector chose an ID outside its candidate pool: {value}"
            )
        span = pool_by_id[key]
        if span.key not in selected_keys:
            selected.append(span)
            selected_ids.append(key)
            selected_keys.add(span.key)

    causal_protected: list[Span] = []
    if policy.protect_causal_closure:
        causal_values = audit.get("causal_closure")
        if isinstance(causal_values, list):
            for value in causal_values:
                if not isinstance(value, dict):
                    raise PostSelectorPolicyError(
                        "causal closure contains a malformed entry"
                    )
                span = _span_from_mapping(value, known_files=known_files)
                if span.key in selected_keys:
                    continue
                selected.append(span)
                causal_protected.append(span)
                selected_keys.add(span.key)

    selected_lines: set[tuple[str, int]] = set()
    for span in selected:
        selected_lines.update(span.lines)
    if len(selected_lines) > line_budget:
        raise PostSelectorPolicyError(
            "protected selector choices exceed the line budget and cannot be evicted"
        )

    final_values = audit.get("final_candidates")
    final_candidates = (
        [
            _span_from_mapping(value, known_files=known_files)
            for value in final_values
            if isinstance(value, dict)
        ]
        if isinstance(final_values, list)
        else []
    )
    universe = _deduplicate_spans((*pool, *final_candidates))
    chosen = list(selected)
    occupied = set(selected_lines)
    closure_audit: list[dict[str, Any]] = []

    chosen_keys = {span.key for span in chosen}
    closure_candidates = [
        candidate
        for candidate in universe
        if candidate.key not in chosen_keys
        and any(_same_scope(candidate, anchor) for anchor in selected)
    ]
    closure_candidates.sort(
        key=lambda candidate: (
            len(candidate.lines - occupied),
            candidate.file,
            candidate.start,
            candidate.end,
        )
    )
    closure_added = 0
    for candidate in closure_candidates:
        novel = candidate.lines - occupied
        record = {"candidate": candidate.as_dict(), "novel_lines": len(novel)}
        if not novel:
            record.update({"accepted": False, "reason": "no_novel_lines"})
        elif closure_added >= policy.same_scope_closure_max_candidates:
            record.update({"accepted": False, "reason": "closure_candidate_cap"})
        elif len(occupied | novel) > line_budget:
            record.update({"accepted": False, "reason": "total_line_budget"})
        else:
            chosen.append(candidate)
            occupied.update(novel)
            closure_added += 1
            record.update({"accepted": True, "reason": "same_scope_closure"})
        closure_audit.append(record)

    evidence = _build_evidence(audit, known_files)
    support_candidates = _resolve_support_candidates(
        audit, pool, final_candidates, known_files
    )
    selected_files = {span.file for span in chosen}
    production_leaf_files: dict[str, set[str]] = defaultdict(set)
    for candidate in universe:
        leaf = _leaf_name(candidate.name)
        if leaf and _production_path(candidate.file):
            production_leaf_files[leaf].add(candidate.file)

    ranked_support: list[
        tuple[tuple[Any, ...], Span, set[str], dict[str, Any]]
    ] = []
    rejected_support: list[dict[str, Any]] = []
    for candidate, sources in support_candidates:
        families = evidence.families(candidate)
        ranks = evidence.ranks(candidate)
        novel = candidate.lines - occupied
        leaf = _leaf_name(candidate.name)
        ambiguous = (not _discriminative_name(candidate.name)) or (
            bool(leaf) and len(production_leaf_files.get(leaf, set())) > 1
        )
        record = {
            "candidate": candidate.as_dict(),
            "sources": sorted(sources),
            "independent_query_families": sorted(families),
            "independent_query_family_count": len(families),
            "ambiguous_or_generic": ambiguous,
            "novel_lines": len(novel),
        }
        if not _production_path(candidate.file):
            record.update({"accepted": False, "reason": "non_production_support"})
            rejected_support.append(record)
            continue
        if not novel:
            record.update({"accepted": False, "reason": "no_novel_lines"})
            rejected_support.append(record)
            continue
        if len(families) < policy.minimum_independent_query_families:
            record.update(
                {"accepted": False, "reason": "insufficient_independent_query_families"}
            )
            rejected_support.append(record)
            continue
        reciprocal_rank_score = math.fsum(1.0 / rank for rank in ranks if rank > 0)
        best_rank = min(ranks, default=10_000)
        rank_key = (
            *( (0 if candidate.file in selected_files else 1,) if policy.support_selected_file_first else () ),
            -len(families),
            -reciprocal_rank_score,
            best_rank,
            0 if "exact" in sources else 1,
            len(novel),
            candidate.file,
            candidate.start,
            candidate.end,
        )
        record["reciprocal_rank_score"] = reciprocal_rank_score
        record["best_rank"] = best_rank
        ranked_support.append((rank_key, candidate, sources, record))

    ranked_support.sort(key=lambda item: item[0])
    support_added = 0
    support_novel_lines = 0
    support_new_files: set[str] = set()
    support_audit: list[dict[str, Any]] = []
    support_line_cap = policy.novel_line_cap(line_budget)
    for _, candidate, _, record in ranked_support:
        novel = candidate.lines - occupied
        new_file = candidate.file not in selected_files
        reason: str | None = None
        if support_added >= policy.support_max_new_candidates:
            reason = "support_candidate_cap"
        elif new_file and len(support_new_files) >= policy.support_max_new_files:
            reason = "support_new_file_cap"
        elif support_novel_lines + len(novel) > support_line_cap:
            reason = "support_novel_line_cap"
        elif len(occupied | novel) > line_budget:
            reason = "total_line_budget"
        if reason is not None:
            record.update({"accepted": False, "reason": reason})
        else:
            chosen.append(candidate)
            occupied.update(novel)
            support_added += 1
            support_novel_lines += len(novel)
            if new_file:
                support_new_files.add(candidate.file)
            record.update({"accepted": True, "reason": "corroborated_support"})
        support_audit.append(record)
    support_audit.extend(rejected_support)

    projected = _prediction_from_spans(prediction, chosen)
    fail_open_to_original = (
        not _prediction_has_context(projected) and _prediction_has_context(prediction)
    )
    replayed = copy.deepcopy(dict(prediction)) if fail_open_to_original else projected
    replay_audit = {
        "status": (
            "replayed_fail_open_passthrough" if fail_open_to_original else "replayed"
        ),
        "policy_version": policy.version,
        "projection_version": PROJECTION_VERSION,
        "line_budget": line_budget,
        "support_novel_line_cap": support_line_cap,
        "selector": {
            "selected_candidate_ids": selected_ids,
            "protected_candidates": [span.as_dict() for span in selected],
            "causal_protected_candidates": [
                span.as_dict() for span in causal_protected
            ],
            "protected_unique_lines": len(selected_lines),
        },
        "same_scope_closure": closure_audit,
        "support": support_audit,
        "final": {
            "candidates": [span.as_dict() for span in chosen],
            "files": list(replayed["traj_data"]["pred_files"]),
            "unique_lines": len(occupied),
            "support_candidates_added": support_added,
            "support_new_files_added": len(support_new_files),
            "support_novel_lines_added": support_novel_lines,
            "fail_open_to_original": fail_open_to_original,
        },
    }
    return replayed, replay_audit


def apply_runtime_policy(
    prediction: Mapping[str, Any],
    audit: Mapping[str, Any],
    *,
    policy_name: str,
    line_budget: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    policy = POLICIES.get(policy_name)
    if policy is None:
        raise PostSelectorPolicyError(
            f"unsupported enabled post-selector policy: {policy_name}"
        )
    raw_prediction = copy.deepcopy(dict(prediction))
    replayed, replay_audit = replay_prediction(
        raw_prediction,
        audit,
        line_budget=line_budget,
        strict_runtime_audit=True,
        policy=policy,
    )
    manifest = policy_manifest(line_budget=line_budget, policy=policy)
    evidence = {
        "policy_name": policy_name,
        "fingerprint": hashlib.sha256(_canonical_json(manifest)).hexdigest(),
        "manifest": manifest,
        "raw_prediction": {
            "sha256": canonical_prediction_sha256(raw_prediction),
            "context": prediction_context(raw_prediction),
        },
        "output_prediction": {
            "sha256": canonical_prediction_sha256(replayed),
            "context": prediction_context(replayed),
        },
        "replay": replay_audit,
    }
    return replayed, evidence
