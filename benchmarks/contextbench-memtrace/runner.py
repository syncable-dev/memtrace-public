#!/usr/bin/env python3
"""Run Memtrace retrieval and emit ContextBench-compatible trajectories.

The runner records each Memtrace search or graph lookup as a separate context
observation, then emits a compact final context selected with an optional LLM.
The output can be passed directly to the official
``python -m contextbench.evaluate`` command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from post_selector_policy import (
    POST_SELECTOR_POLICY_CHOICES,
    apply_runtime_policy,
)


PROTOCOL_VERSION = "2025-03-26"
DEFAULT_LINE_BUDGET = 80
DEFAULT_SEARCH_LIMIT = 20
DEFAULT_SELECTOR_CANDIDATES = 80
GUARDED_SELECTOR_CANDIDATES = 150
GUARDED_SELECTOR_MAX_ITEMS = 16
DEFAULT_SELECTOR_POLICY = "replay-v1"
SELECTOR_POLICY_CHOICES = (DEFAULT_SELECTOR_POLICY, "continuation-v2")
# Absolute ceiling on the guarded selector's schema maxItems when the pool is
# dominated by candidates from files already established as relevant (see
# GUARDED_SELECTOR_MAX_ITEMS scaling in select_context_with_gpt). The final
# selection is still bounded by line_budget accounting regardless of this
# cap, so raising it mainly affects prompt size/cost, not overrun risk.
GUARDED_SELECTOR_MAX_ITEMS_CAP = 32
# Guarded selection may otherwise spend the entire line budget inside one
# plausible implementation surface even when retrieval independently found a
# second, explicit product concept (for example a canonical command-line
# entrypoint beside an auxiliary wrapper). Reserve at most one quarter of the
# budget for a corroborated concept anchor, and keep the eligible group small
# enough to remain an instruction rather than another ranking surface.
SELECTOR_CONCEPT_POLICY_VERSION = "guarded-concept-coverage-v1"
SELECTOR_CONCEPT_SUPPORT_DIVISOR = 4
SELECTOR_CONCEPT_CANDIDATE_CAP = 8
SELECTOR_CONCEPT_BOUNDARY_MAX_CALLS = 2
SELECTOR_CONCEPT_BOUNDARY_MAX_DEFINITION_LINES = 10
ROOT_RETRIEVAL_LANES = ("symptom", "data_flow", "input_transform")
V4_ROOT_RETRIEVAL_LANES = ("legacy_head", *ROOT_RETRIEVAL_LANES)
# V5 keeps v3's compact semantic ranking, but floors candidates from files
# independently recovered by multiple root queries into the selector pool.
# This is deliberately a file-level consensus signal: a relevant symbol can
# rank poorly in every individual cross-encoder list while its containing file
# is still the only implementation surface consistently recovered across the
# symptom, flow, and transformation views.
CONSENSUS_PRIORITY_MIN_ROOT_LANES = 2
CONSENSUS_PRIORITY_MAX_FILES = 24
CONSENSUS_PRIORITY_PER_FILE = 2
# Causal closure is a narrow, local data-flow repair after selection.  It may
# add one nearby producer for a long, repository-specific identifier read by a
# selected function.  The hard line cap prevents this from becoming another
# same-file greedy fill policy.
CAUSAL_CLOSURE_MAX_CANDIDATES = 1
CAUSAL_CLOSURE_LINE_DIVISOR = 8
# Per-file cap on how many candidates from an already cross-lane-confirmed
# refinement file are force-included ("floored") into the selector pool
# before the flat pool_size truncation runs. Previously a bare 6, taken in
# arbitrary (first-encountered) order, which let low-scoring-but-relevant
# siblings in symbol-dense files (e.g. large dispatch trees) fall out of the
# pool even though their file was already confirmed relevant.
REFINEMENT_FILE_PRIORITY_CAP = 12
# Selector pools fuse several query and graph lanes. A widened search hit can
# therefore produce many overlapping windows for one symbol, and one noisy
# file can consume most of the fixed-size pool before the selector sees the
# rest of the retrieval surface. For non-priority candidates, keep at most the
# exact symbol plus one useful widened/windowed variant per stable symbol ID,
# and reserve four fifths of the pool for other files when alternatives exist.
# Hard priority floors are exempt. The file cap is soft: a later pass fills
# from the deferred file when retrieval genuinely has no broader surface.
SELECTOR_POOL_SYMBOL_VARIANT_CAP = 2
SELECTOR_POOL_FILE_SHARE_DENOMINATOR = 5
DEFAULT_SEARCH_QUERY_COUNT = 3
DEFAULT_GRAPH_EXPANSIONS_PER_QUERY = 3
DEFAULT_MAX_GRAPH_CONTEXTS = 15
DEFAULT_IDENTIFIER_QUERY_COUNT = 8
DEFAULT_REFINED_FILE_COUNT = 8
DEFAULT_FILE_REFINEMENT_LIMIT = 12
REINCLUDE_SOURCE_EXTENSIONS = {
    ".c", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js", ".jsx",
    ".kt", ".m", ".mm", ".php", ".py", ".rb", ".rs", ".scala", ".swift",
    ".ts", ".tsx",
}
DEFAULT_EXCLUDED_DIR_COLLISIONS = (
    "bin", "build", "env", "out", "pkg", "site", "target",
)
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does",
    "for", "from", "if", "in", "into", "is", "it", "not", "of", "on",
    "or", "that", "the", "this", "to", "when", "with", "would",
}

# --- Opt-in retrieval policy v2 (ContextBench cb-r0 remediation) -----------
# Every v2 behavior below is flag-gated (--pack-policy v2, --query-strategy
# v2, --normalize-issue, --search-limit) with defaults that reproduce prior
# runs byte-for-byte. Constants were replay-validated on the cb-r0 15-task
# slice (see scratchpad policy_grid.py / adaptive_v3_policy_note.py).
PACK_POLICY_CHOICES = ("v1", "v2", "v3", "v4", "v5")
QUERY_STRATEGY_CHOICES = ("head", "v2", "v3", "v4", "v5")
WINDOW_AWARE_PACK_POLICIES = {"v2", "v3", "v4", "v5"}
V2_OVERSIZE_THRESHOLD = 60
V2_RELEVANCE_FLOOR = 0.2
V2_TIGHT_TOP_SCORE = 1.5
V2_TIGHT_FILE_DOMINANCE = 1.6
V2_TIGHT_CHAIN_REL_FLOOR = 1 / 3
V2_TIGHT_WINDOWS_PER_SYMBOL = 2
V2_SIBLING_MAX_LINES = 50
V2_CONSENSUS_WEIGHT = 0.5

# --- pack-policy v3 (cb-r1 django/gson regression fix) ----------------------
# v2's consolidation regime (pack_anchor_consolidated) packs *every*
# above-the-global-relevance-floor candidate from the anchor file before
# spilling to other files, with no score falloff check. On single-method
# gold spans inside a large class (django loaddata.py: gold is one
# @cached_property, ~30 lines) this stuffs the remaining line_budget with
# unrelated same-file siblings (other Command methods) that clear the low
# 0.2 global floor but are not part of the gold span, cratering precision
# without any recall gain (cb-r1: django line_f1 0.333->0.261, precision
# 0.201->0.151, recall unchanged 0.968->0.968; gson also dipped slightly).
# v3 keeps v2's dedup/novel-line accounting and TIGHT-regime routing
# unchanged, and only tightens the CONSOLIDATED regime's same-file pass
# with a chain relative-score floor (same idea as V2_TIGHT_CHAIN_REL_FLOOR
# in the tight regime): a same-file sibling only gets packed ahead of
# cross-file spillover if it scores within V3_CONSOLIDATION_CHAIN_REL_FLOOR
# of the anchor. This preserves v2's wins on sklearn/cli (their packed
# siblings score close to the anchor already) while curbing the low-value
# same-file filler that hurt django/gson.
# 0.75 against RAW candidate_score_v2 (see pack_final_context_v2's
# raw_score_of): live-validated on the django audit -- raw scores for
# loaddata.py's methods were fixture_dirs=0.613 (anchor), find_fixtures
# =0.436, loaddata/get_fixture_name_and_dirs/load_label=0.377,
# find_fixture_files_in_dir=0.319. 0.75 (=0.46 floor) is the first
# coefficient that excludes every off-target method while keeping the
# anchor; 0.5-0.7 all left at least one filler method in (the original
# 0.5-against-blended-score attempt excluded nothing at all).
V3_CONSOLIDATION_CHAIN_REL_FLOOR = 0.75
V2_CLASS_PENALTY = 1.0
V2_CONSOLIDATION_FILES = 3

# --- pack-policy v4 (cb-r2 fix: v3's score floor measurably no-ops) --------
# v3 tried to bound CONSOLIDATED same-file filler with a RAW relative-score
# floor (V3_CONSOLIDATION_CHAIN_REL_FLOOR = 0.75). Live-validated against the
# real find_code candidate pool (cb-r2-packv3,
# predictions-audit/..3f745086.json), this made ZERO difference on the
# django task the fix targeted: final_candidates is still all 6
# loaddata.py Command methods (fixture_dirs, loaddata, load_label,
# get_fixture_name_and_dirs, find_fixtures, find_fixture_files_in_dir),
# 199 predicted lines, line precision still 0.1507 -- byte-identical to
# v2's known-bad number. The offline replay simulator the 0.75 coefficient
# was tuned against assumed a score spread (fixture_dirs=0.613,
# find_fixtures=0.436, ...) that the LIVE candidate_score_v2 distribution on
# this file does not reproduce; every sibling still cleared 0.75x the
# anchor's live raw score. This is the generic failure mode of a
# score-threshold filler cap: it is a moving target tuned against one score
# distribution, and the moment the real distribution is flatter than
# assumed, the floor silently no-ops and greedy-fill continues exactly as
# before -- "raising the bar" doesn't stop the loop, it just changes which
# candidates clear it, and on this file everything still did.
#
# v4 drops the score threshold entirely and replaces it with a STRUCTURAL
# cap that cannot silently no-op regardless of the live score distribution:
#   1. At most V4_MAX_SAME_FILE_SIBLINGS additional same-file spans are
#      EVER added beyond the anchor -- a hard count cap, not a score cap.
#      Multiple windows of the SAME already-selected symbol (oversized-
#      symbol windowing) don't count against this cap; they're more
#      resolution on a hit already accepted, not new filler.
#   2. Cross-file spillover is similarly hard-capped by count
#      (V4_MAX_SPILLOVER_ITEMS) AND still requires each item to be a
#      genuinely strong, near-anchor-quality candidate
#      (score >= V4_SPILLOVER_REL_FLOOR * anchor score) -- a "new anchor"
#      elsewhere, not marginal same-file-style filler one file over.
# This mirrors Agentless's fixed-small-window discipline: stop once the
# fixed budget of *additional* spans is spent, full stop, regardless of how
# much line_budget remains and regardless of how many more candidates clear
# any relevance bar.
V4_MAX_SAME_FILE_SIBLINGS = 2
V4_MAX_SPILLOVER_ITEMS = 3
V4_SPILLOVER_REL_FLOOR = 0.85
V2_MAX_SECTION_QUERIES = 8
V2_MAX_FRAME_QUERIES = 5

VENDORED_PATH_RE = re.compile(
    r"(^|/)(extern|externals|vendor|vendored|third_party|node_modules)/"
    r"|(^|/)docs?/|\.min\.js$"
)
TESTISH_PATH_RE = re.compile(
    r"(^|/)tests?(/|$)|(^|/)test_|[._-](test|spec)s?\.[A-Za-z]+$|Tests?\.java$"
)
NONCODE_PATH_RE = re.compile(r"\.(yml|yaml|json|md|rst|txt|cfg|ini|toml|lock)$")


def is_junk_context_path(path: str) -> bool:
    """Vendored, test-ish, docs, or config paths that never belong in a packed
    change surface (they may still be legitimate retrieval/observation rows)."""
    return bool(
        VENDORED_PATH_RE.search(path)
        or TESTISH_PATH_RE.search(path)
        or NONCODE_PATH_RE.search(path)
    )


def is_test_path(path: str, extended: bool = False) -> bool:
    if any(
        part == "tests" or part.startswith("test_")
        for part in Path(path).parts
    ):
        return True
    return extended and bool(TESTISH_PATH_RE.search(path))


def identifier_queries(text: str, limit: int = DEFAULT_IDENTIFIER_QUERY_COUNT) -> list[str]:
    identifiers: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for position, token in enumerate(re.findall(r"\b[A-Za-z_][A-Za-z0-9_.]*\b", text)):
        token = token.strip(".")
        leaf = token.rsplit(".", 1)[-1]
        identifier_shaped = "_" in leaf or (
            not leaf.isupper() and any(char.isupper() for char in leaf[1:])
        )
        if not identifier_shaped or len(leaf) < 4 or leaf.lower() in STOP_WORDS:
            continue
        if token not in seen:
            seen.add(token)
            priority = 0 if "_" in leaf else 1
            identifiers.append((priority, position, token))
    identifiers.sort()
    return [token for _, _, token in identifiers[:limit]]


def normalize_issue_text(issue: str) -> str:
    """Decode JSON-string-encoded problem statements (--normalize-issue).

    Some benchmark rows ship the issue body as a JSON string literal; the
    literal ``\\n`` escape runs otherwise mint garbage identifiers at the
    word boundaries ("nWorker"). Decode when the whole statement parses as a
    JSON string; else collapse literal escape runs to whitespace when they
    dominate real newlines. Everything else passes through untouched.
    """
    text = issue.strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, str) and decoded.strip():
            return decoded
    literal_escapes = issue.count("\\n") + issue.count("\\t")
    if literal_escapes >= 3 and issue.count("\\n") > issue.count("\n"):
        return re.sub(r"(?:\\[ntr])+", " ", issue)
    return issue


ISSUE_CHECKLIST_RE = re.compile(r"^\s*-\s*\[[ xX]\].*$", re.MULTILINE)
ISSUE_DETAILS_RE = re.compile(r"<details[^>]*>.*?(</details>|\Z)", re.DOTALL | re.IGNORECASE)
ISSUE_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
ISSUE_TEMPLATE_HEADING_RE = re.compile(
    r"^#{1,6}\s*.*(DO NOT REMOVE|ISSUE TEMPLATE|existing issues first|Checklist"
    r"|Verbose log|Complete Verbose).*$",
    re.MULTILINE | re.IGNORECASE,
)
ISSUE_URL_RE = re.compile(r"https?://\S+")
ISSUE_MENTION_RE = re.compile(r"@([A-Za-z0-9_-]+)")
MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+\S.*$", re.MULTILINE)
LOG_LINE_RE = re.compile(r"^\s*\[[A-Za-z0-9._-]+\]\s.*$", re.MULTILINE)


def strip_issue_boilerplate(issue: str) -> str:
    """Deterministically remove GitHub issue-template noise: HTML comments,
    <details> dumps, checkbox checklist lines, and template-warning headings."""
    text = issue.replace("\r\n", "\n").replace("\r", "\n")
    text = ISSUE_HTML_COMMENT_RE.sub(" ", text)
    text = ISSUE_DETAILS_RE.sub(" ", text)
    text = ISSUE_CHECKLIST_RE.sub("", text)
    text = ISSUE_TEMPLATE_HEADING_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


STACK_FRAME_PATTERNS = (
    # Python: File "path/module.py", line 10, in function_name
    re.compile(r'File "(?P<path>[^"]+)", line \d+, in (?P<name>[A-Za-z_][A-Za-z0-9_]*)'),
    # JS/TS: at name (path:line:col) — name may be dotted/qualified
    re.compile(r"\bat (?P<name>[A-Za-z_$][\w$.]*) \((?P<path>[^()\s:]+):\d+:\d+\)"),
    # Go: pkg.name(...) then \t path.go:line
    re.compile(r"(?:^|\n)[\w./-]*\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)\([^)\n]*\)\n\s*(?P<path>\S+\.go):\d+"),
    # Java: at pkg.Class.method(File.java:123)
    re.compile(r"\bat [\w.$]+\.(?P<name>[A-Za-z_$][\w$]*)\((?P<path>[\w$]+\.java):\d+\)"),
    # Rust: N: crate::module::name \n at path.rs:line
    re.compile(r"\d+:\s+[\w:]*::(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\n\s*at (?P<path>\S+\.rs):\d+"),
)
EXTERNAL_FRAME_RE = re.compile(
    r"site-packages|dist-packages|node_modules|/usr/lib|/usr/local/lib"
    r"|(^|/)runpy\.py$|internal/|<frozen"
)
GENERIC_FRAME_NAMES = {
    "main", "invoke", "new_func", "wrapper", "inner", "run", "call", "module",
    "lambda",
}


def stack_frame_queries(issue: str, limit: int = V2_MAX_FRAME_QUERIES) -> list[str]:
    """Repo-local stack-trace frame functions and module stems, deepest-first.

    Frames from interpreter/vendored paths are dropped; the deepest frames
    (closest to the error) come first because they name the causal code.
    """
    frames: list[tuple[str, str]] = []
    for pattern in STACK_FRAME_PATTERNS:
        for match in pattern.finditer(issue):
            path = match.group("path")
            name = match.group("name")
            if EXTERNAL_FRAME_RE.search(path):
                continue
            leaf = name.rsplit(".", 1)[-1]
            frames.append((path, leaf))
    queries: list[str] = []
    seen: set[str] = set()
    for path, name in reversed(frames):
        stem = Path(path).stem
        for query in (name, stem):
            lowered = query.lower()
            if (
                len(query) < 4
                or lowered in GENERIC_FRAME_NAMES
                or lowered in STOP_WORDS
                or lowered in seen
            ):
                continue
            seen.add(lowered)
            queries.append(query)
    return queries[:limit]


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]{1,120})`(?!`)")
CLI_FLAG_RE = re.compile(r"(?<![A-Za-z0-9_-])--[A-Za-z][A-Za-z0-9-]{1,80}")
PATH_HINT_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8}"
)
CODE_LITERAL_STOP_WORDS = STOP_WORDS | {
    "bool", "class", "const", "def", "else", "false", "float", "for", "function",
    "if", "import", "int", "len", "let", "list", "none", "null", "object", "print",
    "return", "self", "set", "str", "string", "true", "tuple", "value", "values",
    "vals", "var", "while",
}


def inline_code_identifiers(issue: str, limit: int = DEFAULT_IDENTIFIER_QUERY_COUNT) -> list[str]:
    """Recover explicit code names even when they are lowercase prose words.

    ``identifier_queries`` intentionally rejects names such as ``hexbin`` and
    ``mincnt`` because, outside code formatting, they look like English words.
    Backticks are an author-supplied type signal, so v3 can safely use those
    names as exact BM25 lanes without guessing repository symbols.
    """
    identifiers: list[str] = []
    seen: set[str] = set()
    for fragment in INLINE_CODE_RE.findall(issue):
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", fragment):
            token = token.strip(".")
            leaf = token.rsplit(".", 1)[-1]
            lowered = leaf.lower()
            if len(leaf) < 3 or lowered in CODE_LITERAL_STOP_WORDS or lowered in seen:
                continue
            seen.add(lowered)
            identifiers.append(token)
            if len(identifiers) >= limit:
                return identifiers
    return identifiers


def explicit_issue_queries(issue: str, limit: int = DEFAULT_IDENTIFIER_QUERY_COUNT) -> list[str]:
    """High-confidence lexical lanes supplied explicitly by the issue author."""
    values = [
        *inline_code_identifiers(issue, limit=limit),
        *CLI_FLAG_RE.findall(issue),
        *PATH_HINT_RE.findall(issue),
    ]
    queries: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        longest_alphanumeric_run = max(
            (len(run) for run in re.findall(r"[A-Za-z0-9]+", value)),
            default=0,
        )
        if key in seen or len(value) > 100 or longest_alphanumeric_run > 48:
            continue
        seen.add(key)
        queries.append(value)
        if len(queries) >= limit:
            break
    return queries


def bounded_exact_queries(
    values: Iterable[str], limit: int = DEFAULT_IDENTIFIER_QUERY_COUNT
) -> list[str]:
    """Deduplicate exact lanes and reject generated hashes/base64 payloads."""
    queries: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = value.strip()
        key = value.casefold()
        longest_alphanumeric_run = max(
            (len(run) for run in re.findall(r"[A-Za-z0-9]+", value)),
            default=0,
        )
        if (
            not value
            or key in seen
            or len(value) > 100
            or longest_alphanumeric_run > 48
        ):
            continue
        seen.add(key)
        queries.append(value)
        if len(queries) >= limit:
            break
    return queries


def compact_ranking_query(
    issue: str,
    planned_queries: Iterable[str] = (),
    exact_queries: Iterable[str] = (),
    max_chars: int = 1600,
) -> str:
    """Build a low-noise query for candidate and oversized-symbol ranking.

    Search planning still receives the full cleaned issue. Ranking does not:
    long reproductions and environment dumps flatten lexical scores and make
    docstrings beat the implementation branch. The compact form retains the
    title, explicit inline-code names, exact lanes, and genuinely concise GPT
    planner lanes.
    """
    cleaned = strip_issue_boilerplate(issue)
    title = next((line.strip("# ") for line in cleaned.splitlines() if line.strip()), "")
    parts = [title, *explicit_issue_queries(cleaned), *exact_queries]
    parts.extend(
        collapse_whitespace(query)
        for query in planned_queries
        if query.strip() and len(collapse_whitespace(query)) <= 320
    )
    compact = collapse_whitespace(" ".join(dict.fromkeys(part for part in parts if part)))
    return compact[:max_chars] or collapse_whitespace(cleaned)[:max_chars]


def legacy_head_query(issue: str, max_chars: int = 700) -> str:
    """Preserve the high-precision legacy issue-head lane for v4.

    The semantic planner broadens recall, but it can rephrase away the exact
    repository vocabulary that made the old head query precise.  V4 carries
    that original lexical signal as an independent lane and uses it for local
    window ranking; the three planner lanes remain available for recall.
    """
    return collapse_whitespace(strip_issue_boilerplate(issue))[:max_chars]


def markdown_section_queries(
    cleaned_issue: str, limit: int = V2_MAX_SECTION_QUERIES, max_chars: int = 700
) -> list[str]:
    """One <=700-char query per markdown section, covering the whole cleaned
    issue instead of only its head; consecutive short sections are merged."""
    boundaries = [match.start() for match in MARKDOWN_HEADING_RE.finditer(cleaned_issue)]
    if not boundaries or boundaries[0] != 0:
        boundaries.insert(0, 0)
    sections = [
        collapse_whitespace(cleaned_issue[start:end])
        for start, end in zip(boundaries, [*boundaries[1:], len(cleaned_issue)])
    ]
    sections = [section for section in sections if len(section) >= 40]
    merged: list[str] = []
    for section in sections:
        if merged and len(merged[-1]) + len(section) + 1 <= max_chars:
            merged[-1] = f"{merged[-1]} {section}"
        else:
            merged.append(section[:max_chars])
    if len(merged) > limit:
        head_count = (limit + 1) // 2
        merged = merged[:head_count] + merged[-(limit - head_count):]
    return merged


TITLE_SUFFIXES = ("ies", "ing", "ed", "s", "y")


def stemmed_title_query(cleaned_issue: str) -> str:
    """Light morphological fallback for pure-prose issues with no identifiers:
    title keywords with -y/-s/-ies/-ing/-ed stripped (recovery -> recover)."""
    title = next(
        (line.strip() for line in cleaned_issue.splitlines() if line.strip()), ""
    )
    words = [
        word
        for word in re.findall(r"[A-Za-z]+", title.lower())
        if word not in STOP_WORDS and len(word) > 2
    ]
    stemmed: list[str] = []
    for word in words:
        for suffix in TITLE_SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[: -len(suffix)]
                break
        if word not in stemmed:
            stemmed.append(word)
    return " ".join(stemmed)


def synthesize_queries_v2(issue: str) -> tuple[list[str], list[str], str]:
    """Query-strategy v2: planned semantic lanes + exact frame/identifier lanes.

    Returns (planned_queries, exact_seed_queries, identifier_source). The
    original head lane stays first for comparability and floor behavior; the
    remaining lanes cover the whole cleaned issue plus its tail, where error
    text tends to cluster. Identifier extraction runs over the full cleaned
    text with URLs removed and @-mention names dropped.
    """
    cleaned = strip_issue_boilerplate(issue)
    raw_planned: list[str] = [issue[:700]]
    raw_planned.extend(markdown_section_queries(cleaned))
    collapsed_issue = collapse_whitespace(cleaned)
    if len(collapsed_issue) > 700:
        raw_planned.append(collapsed_issue[-700:])
    planned: list[str] = []
    seen_lane_keys: set[str] = set()
    for query in raw_planned:
        if not query.strip():
            continue
        # Fuzzy lane dedupe: the first section of a short issue is usually the
        # head lane again modulo whitespace; near-duplicate lanes double-vote
        # in refinement-file ranking without adding retrieval coverage.
        key = collapse_whitespace(query).lower()[:160]
        if key in seen_lane_keys:
            continue
        seen_lane_keys.add(key)
        planned.append(query)

    frame_queries = stack_frame_queries(issue)
    mentions = {name.lower() for name in ISSUE_MENTION_RE.findall(issue)}
    # Identifier extraction skips URLs, @-mentions, and tool-log lines
    # ("[debug] ...", "[youtube] aQvGIIdgFDM: ..."): log dumps mint
    # ID-shaped garbage tokens that burn exact-match lanes.
    identifier_source = LOG_LINE_RE.sub(" ", cleaned)
    identifier_source = ISSUE_URL_RE.sub(" ", ISSUE_MENTION_RE.sub(" ", identifier_source))
    # Union with the v1 head-lane extraction so existing exact-identifier
    # floors are never lost (only @-mention names are ever dropped).
    identifiers = [
        token
        for token in dict.fromkeys(
            [*identifier_queries(identifier_source), *identifier_queries(issue[:700])]
        )
        if token.rsplit(".", 1)[-1].lower() not in mentions
    ]
    exact_seed = list(dict.fromkeys([*frame_queries, *identifiers]))
    if not exact_seed:
        fallback = stemmed_title_query(cleaned)
        if fallback:
            planned.append(fallback)
    return planned, exact_seed, identifier_source


def send_openai_request(
    request: urllib.request.Request, label: str, attempts: int = 3
) -> dict[str, Any]:
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            if attempt == attempts or (error.code != 429 and error.code < 500):
                raise RuntimeError(
                    f"{label} failed with HTTP {error.code}: {details}"
                ) from error
        except (TimeoutError, urllib.error.URLError) as error:
            if attempt == attempts:
                raise RuntimeError(f"{label} failed after {attempts} attempts: {error}") from error
        time.sleep(2 ** (attempt - 1))
    raise AssertionError("OpenAI retry loop exhausted without returning or raising")


@dataclass(frozen=True)
class Candidate:
    file: str
    start: int
    end: int
    name: str
    kind: str
    content: str = ""
    source_rank: int = 100
    symbol_id: str = ""
    is_exact_symbol: bool = False

    @property
    def line_count(self) -> int:
        return max(1, self.end - self.start + 1)

    @property
    def symbol_identity(self) -> tuple[str, ...]:
        if self.symbol_id:
            return ("id", self.symbol_id)
        if self.name:
            return ("name", self.file, self.name, self.kind)
        return ("span", self.file, str(self.start), str(self.end), self.kind)


class McpClient:
    def __init__(self, repo_root: Path, env: dict[str, str]) -> None:
        self._next_id = 1
        self._stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        self._proc = subprocess.Popen(
            ["memtrace", "mcp"],
            cwd=repo_root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            bufsize=1,
        )
        try:
            self._rpc(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "contextbench-memtrace",
                        "version": "0.1",
                    },
                },
            )
            self._notify("notifications/initialized", {})
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._stderr.close()

    def _stderr_tail(self, limit: int = 4000) -> str:
        self._stderr.flush()
        self._stderr.seek(0)
        return self._stderr.read()[-limit:].strip()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        envelope = self._rpc("tools/call", {"name": name, "arguments": arguments})
        result = envelope.get("result", {})
        if result.get("isError"):
            raise RuntimeError(self._content_text(result) or f"{name} failed")
        text = self._content_text(result)
        return json.loads(text) if text else None

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        assert self._proc.stdout is not None
        line = self._proc.stdout.readline()
        if not line:
            code = self._proc.poll()
            detail = self._stderr_tail()
            message = f"Memtrace MCP exited before replying (rc={code})"
            if detail:
                message += f": {detail}"
            raise RuntimeError(message)
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(f"Memtrace MCP error: {response['error']}")
        return response

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, payload: dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self._proc.stdin.flush()

    @staticmethod
    def _content_text(result: dict[str, Any]) -> str:
        return "\n".join(
            item.get("text", "")
            for item in result.get("content", [])
            if item.get("type") == "text"
        )


def tokenize(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", text.lower())
        if token not in STOP_WORDS and len(token) > 1
    ]


def candidate_score(candidate: Candidate, query: str) -> float:
    query_tokens = Counter(tokenize(query))
    candidate_tokens = Counter(tokenize(f"{candidate.name} {candidate.content}"))
    overlap = sum(min(count, candidate_tokens[token]) for token, count in query_tokens.items())
    score = overlap / max(1, sum(query_tokens.values()))
    lowered_query = query.lower()
    if candidate.name.lower() in lowered_query and candidate.name != "__init__":
        score += 2.0
    if candidate.name == "__init__" and any(
        cue in lowered_query
        for cue in ("constructor", "initialize", "create ", "given an ", "given a ", "empty name")
    ):
        score += 1.5
    if candidate.kind.lower() in {"function", "method"}:
        score += 0.2
    if candidate.kind.lower() == "class":
        score -= 1.0
    score += 0.1 / (candidate.source_rank + 1)
    return score


def production_surface(path: str) -> str:
    """Return a stable, task-agnostic implementation-surface label.

    The first directory is normally the useful boundary. ``contrib`` is the
    exception: independent tools commonly live beside one another there, so
    keeping the second directory prevents one auxiliary binary from standing
    in for every other production surface.
    """
    parts = [part.casefold() for part in Path(path).parts if part not in ("", ".")]
    if not parts:
        return ""
    if parts[0] == "contrib" and len(parts) > 1:
        return f"contrib/{parts[1]}"
    return parts[0]


def is_command_line_surface(candidate: Candidate) -> bool:
    """Whether a candidate is structurally part of a command-line surface."""
    path = candidate.file.casefold()
    parts = [part for part in Path(path).parts if part not in ("", ".")]
    path_tokens = re.findall(r"[a-z0-9]+", path)
    explicit_tokens = {"bin", "cli", "cmd", "command", "commands", "programs"}
    return bool(
        explicit_tokens.intersection(parts)
        or explicit_tokens.intersection(path_tokens)
        or any(token.endswith("cli") for token in path_tokens)
        or (
            Path(path).stem in {"main", "program"}
            and candidate.name.casefold().rsplit("::", 1)[-1] == "main"
        )
    )


def selector_concept_requirements(
    pool: list[Candidate],
    query: str,
    concept_queries: Iterable[str],
    line_budget: int,
    candidate_lanes: dict[tuple[str, int, int], list[str]] | None = None,
    file_query_families: dict[str, Iterable[str]] | None = None,
) -> list[dict[str, Any]]:
    """Build small, evidence-backed concept groups for guarded selection.

    This deliberately uses only issue/search text and retrieval provenance. It
    never consumes task IDs, gold spans, evaluator output, or prior scores.
    A command-line option group is emitted only when the issue/search plan is
    explicit about both the CLI surface and an option/argument, and a candidate
    on that surface has direct root-lane evidence plus support from at least two
    independent query families.
    """
    semantic_queries = [query, *[str(value) for value in concept_queries]]
    semantic_text = "\n".join(semantic_queries)
    lowered = semantic_text.casefold()
    has_cli_cue = bool(re.search(r"\b(?:command[ -]line|cli)\b", lowered))
    has_option_cue = bool(
        re.search(r"--[a-z0-9][a-z0-9-]*", lowered)
        or re.search(r"\b(?:option|argument)[ -](?:parse|parsing|parser)\b", lowered)
    )
    if not (has_cli_cue and has_option_cue):
        return []

    support_cap = max(1, line_budget // SELECTOR_CONCEPT_SUPPORT_DIVISOR)
    root_lanes = set(V4_ROOT_RETRIEVAL_LANES)
    file_root_lanes: dict[str, set[str]] = {}
    for (file, _start, _end), lanes in (candidate_lanes or {}).items():
        file_root_lanes.setdefault(file, set()).update(set(lanes) & root_lanes)
    family_map = {
        file: {str(value) for value in values}
        for file, values in (file_query_families or {}).items()
    }
    ranked: list[tuple[tuple[Any, ...], int]] = []
    for candidate_id, candidate in enumerate(pool):
        families = family_map.get(candidate.file, set())
        if (
            candidate.line_count > support_cap
            or is_junk_context_path(candidate.file)
            or not is_command_line_surface(candidate)
            or not file_root_lanes.get(candidate.file)
            or len(families) < 2
        ):
            continue
        ranked.append(
            (
                (
                    -len(families),
                    -max(
                        candidate_score(candidate, semantic_query)
                        for semantic_query in semantic_queries
                        if semantic_query
                    ),
                    0 if candidate.kind.casefold() in {"function", "method"} else 1,
                    0 if candidate.is_exact_symbol else 1,
                    candidate.source_rank,
                    candidate.line_count,
                    candidate.file,
                    candidate.start,
                ),
                candidate_id,
            )
        )
    ranked.sort(key=lambda item: item[0])
    eligible_ids = [
        candidate_id
        for _, candidate_id in ranked[:SELECTOR_CONCEPT_CANDIDATE_CAP]
    ]
    if not eligible_ids:
        return []
    return [
        {
            "name": "command_line_option_surface",
            "candidate_ids": eligible_ids,
            "reserved_line_budget": support_cap,
            "minimum_query_families": 2,
        }
    ]


def selector_concept_seed_ids(
    requirements: Iterable[dict[str, Any]],
    pool: list[Candidate],
    model_candidate_ids: Iterable[Any],
    line_budget: int,
) -> tuple[list[int], list[int], list[str]]:
    """Prioritize or add concept anchors before ordinary selector choices.

    If the valid model choices are all in one implementation surface, a
    genuinely missing concept may add one corroborated anchor from a different
    surface. A concept the model already selected is left in the model's order,
    and empty/invalid model output still reaches the normal deterministic
    fallback. This keeps the intervention confined to actual omissions.
    """
    valid_model_ids: list[int] = []
    seen: set[int] = set()
    for value in model_candidate_ids:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value < len(pool)
            or value in seen
            or pool[value].line_count > line_budget
        ):
            continue
        seen.add(value)
        valid_model_ids.append(value)
    model_surfaces = sorted(
        {
            surface
            for candidate_id in valid_model_ids
            if (surface := production_surface(pool[candidate_id].file))
        }
    )
    if not valid_model_ids:
        return [], [], model_surfaces

    seeded: list[int] = []
    added: list[int] = []
    reserved_lines = 0
    valid_model_set = set(valid_model_ids)
    for requirement in requirements:
        raw_eligible = requirement.get("candidate_ids", [])
        eligible = [
            value
            for value in raw_eligible
            if isinstance(value, int) and 0 <= value < len(pool)
        ]
        if any(value in valid_model_set for value in eligible):
            continue
        selected_anchor = None
        if len(model_surfaces) == 1:
            selected_surface = model_surfaces[0]
            selected_anchor = next(
                (
                    value
                    for value in eligible
                    if production_surface(pool[value].file) != selected_surface
                ),
                None,
            )
        if selected_anchor is not None and selected_anchor not in seeded:
            added.append(selected_anchor)
        if selected_anchor is None or selected_anchor in seeded:
            continue
        reserve_cap = min(
            line_budget,
            int(
                requirement.get(
                    "reserved_line_budget",
                    max(1, line_budget // SELECTOR_CONCEPT_SUPPORT_DIVISOR),
                )
            ),
        )
        if reserved_lines + pool[selected_anchor].line_count > reserve_cap:
            if selected_anchor in added:
                added.remove(selected_anchor)
            continue
        seeded.append(selected_anchor)
        reserved_lines += pool[selected_anchor].line_count
    return seeded, added, model_surfaces


def identifier_subtokens(value: str) -> list[str]:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return [
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9]*", separated.replace("_", " "))
        if len(token) > 1 and token.casefold() not in STOP_WORDS
    ]


def concept_boundary_identifiers(
    anchor: Candidate,
    semantic_queries: Iterable[str],
    limit: int = SELECTOR_CONCEPT_BOUNDARY_MAX_CALLS,
) -> list[str]:
    """Find a tiny semantic setter/application cluster in a selected window.

    The strongest mutator-style call is selected by overlap between its callee
    and argument identifiers and the issue/search-plan concepts. One adjacent
    call with the same API prefix may follow it, which preserves a local value-
    propagation cluster without turning the closure into general call-graph
    expansion.
    """
    if limit <= 0 or not anchor.content:
        return []
    query_tokens = set(identifier_subtokens("\n".join(semantic_queries)))
    if not query_tokens:
        return []
    calls: list[dict[str, Any]] = []
    control_names = {"if", "for", "while", "switch", "sizeof", "return"}
    mutator_tokens = {
        "apply", "configure", "init", "initialize", "parse", "set", "update",
        "validate", "write",
    }
    for line_index, line in enumerate(anchor.content.splitlines()):
        for match in re.finditer(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(([^;\n]*)\)\s*;",
            line,
        ):
            identifier, arguments = match.groups()
            if identifier.casefold() in control_names:
                continue
            identifier_tokens = identifier_subtokens(identifier)
            if not mutator_tokens.intersection(identifier_tokens):
                continue
            semantic_tokens = set(identifier_subtokens(f"{identifier} {arguments}"))
            overlap = semantic_tokens & query_tokens
            if not overlap:
                continue
            mutator_position = next(
                (
                    index
                    for index, token in enumerate(identifier_tokens)
                    if token in mutator_tokens
                ),
                0,
            )
            family = tuple(identifier_tokens[: mutator_position + 1])
            calls.append(
                {
                    "identifier": identifier,
                    "line_index": line_index,
                    "family": family,
                    "overlap": len(overlap),
                }
            )
    if not calls:
        return []
    calls.sort(
        key=lambda call: (
            -int(call["overlap"]),
            int(call["line_index"]),
            str(call["identifier"]),
        )
    )
    primary = calls[0]
    selected = [str(primary["identifier"])]
    adjacent = sorted(
        (
            call
            for call in calls[1:]
            if call["family"] == primary["family"]
            and abs(int(call["line_index"]) - int(primary["line_index"])) <= 2
        ),
        key=lambda call: (
            abs(int(call["line_index"]) - int(primary["line_index"])),
            -int(call["overlap"]),
            int(call["line_index"]),
        ),
    )
    for call in adjacent:
        identifier = str(call["identifier"])
        if identifier not in selected:
            selected.append(identifier)
        if len(selected) >= limit:
            break
    return selected[:limit]


def local_exact_definition_candidate(
    repo_root: Path,
    identifier: str,
    excluded_file: str,
    max_lines: int = SELECTOR_CONCEPT_BOUNDARY_MAX_DEFINITION_LINES,
) -> Candidate | None:
    """Narrow fallback after Memtrace misses an exact extracted callee.

    This is intentionally limited to C-like brace definitions, exact symbol
    names, production source paths, and a tiny body. It does not run a general
    lexical search or use benchmark answer data.
    """
    definition_re = re.compile(rf"\b{re.escape(identifier)}\s*\(")
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in REINCLUDE_SOURCE_EXTENSIONS:
            continue
        relative = path.relative_to(repo_root).as_posix()
        if relative == excluded_file or is_junk_context_path(relative):
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            if not definition_re.search(line):
                continue
            brace_index = index
            while brace_index < min(len(lines), index + 3) and "{" not in lines[brace_index]:
                if ";" in lines[brace_index]:
                    break
                brace_index += 1
            if brace_index >= len(lines) or "{" not in lines[brace_index]:
                continue
            depth = 0
            end_index = brace_index
            for end_index in range(brace_index, min(len(lines), index + max_lines)):
                depth += lines[end_index].count("{") - lines[end_index].count("}")
                if depth == 0:
                    break
            if depth != 0 or end_index - index + 1 > max_lines:
                continue
            return Candidate(
                file=relative,
                start=index + 1,
                end=end_index + 1,
                name=identifier,
                kind="Function",
                content="\n".join(lines[index : end_index + 1]),
                source_rank=0,
                is_exact_symbol=True,
            )
    return None


def expand_concept_boundaries(
    client: "McpClient",
    repo_id: str,
    repo_root: Path,
    selected: list[Candidate],
    selector_audit: dict[str, Any],
    semantic_queries: Iterable[str],
    line_budget: int,
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Resolve and protect a bounded cross-file boundary chain.

    Expansion only runs for anchors the concept policy had to add because the
    selector omitted the entire product surface. Memtrace gets the first exact
    resolution attempt; a narrow local definition scan is an audited fallback.
    """
    coverage = selector_audit.get("concept_coverage")
    pool_entries = selector_audit.get("candidate_pool")
    selected_ids = selector_audit.get("selected_candidate_ids")
    if (
        not isinstance(coverage, dict)
        or not isinstance(pool_entries, list)
        or not isinstance(selected_ids, list)
    ):
        return selected, []
    added_anchor_ids = coverage.get("added_candidate_ids", [])
    if not isinstance(added_anchor_ids, list) or not added_anchor_ids:
        coverage["boundary_expansion"] = []
        return selected, []

    entry_by_id = {
        entry.get("id"): entry
        for entry in pool_entries
        if isinstance(entry, dict) and "id" in entry
    }
    anchors: list[Candidate] = []
    for anchor_id in added_anchor_ids:
        entry = entry_by_id.get(anchor_id)
        if not isinstance(entry, dict):
            continue
        anchor = next(
            (
                candidate
                for candidate in selected
                if candidate.file == entry.get("file")
                and candidate.start == entry.get("start")
                and candidate.end == entry.get("end")
            ),
            None,
        )
        if anchor is not None:
            anchors.append(anchor)
    if not anchors:
        coverage["boundary_expansion"] = []
        return selected, []

    support_cap = max(1, line_budget // SELECTOR_CONCEPT_SUPPORT_DIVISOR)
    support_lines = sum(anchor.line_count for anchor in anchors)
    total_lines = sum(candidate.line_count for candidate in selected)
    next_id = max(
        (
            int(entry.get("id"))
            for entry in pool_entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), int)
        ),
        default=-1,
    ) + 1
    events: list[dict[str, Any]] = []
    graph_records: list[dict[str, Any]] = []
    expanded = list(selected)
    seen_identifiers: set[str] = set()
    for anchor in anchors:
        identifiers = concept_boundary_identifiers(anchor, semantic_queries)
        for identifier in identifiers:
            if identifier in seen_identifiers:
                continue
            seen_identifiers.add(identifier)
            event: dict[str, Any] = {
                "anchor": {
                    "file": anchor.file,
                    "start": anchor.start,
                    "end": anchor.end,
                },
                "identifier": identifier,
            }
            context: dict[str, Any] | None = None
            try:
                raw_context = client.call_tool(
                    "get_symbol_context",
                    {"repo_id": repo_id, "symbol": identifier},
                )
                if isinstance(raw_context, dict):
                    context = raw_context
            except Exception as error:
                event["memtrace_error"] = type(error).__name__
            resolved = None
            if context is not None:
                graph_records.append(
                    {
                        "anchor": {
                            "name": identifier,
                            "file_path": anchor.file,
                            "source": "concept_boundary",
                        },
                        "context": context,
                    }
                )
                context_candidates = hydrate_candidate_content(
                    graph_candidates(context, repo_root), repo_root
                )
                resolved = min(
                    (
                        candidate
                        for candidate in context_candidates
                        if candidate.file != anchor.file
                        and candidate.name.casefold().rsplit("::", 1)[-1]
                        == identifier.casefold()
                        and candidate.line_count
                        <= SELECTOR_CONCEPT_BOUNDARY_MAX_DEFINITION_LINES
                        and not is_junk_context_path(candidate.file)
                    ),
                    key=lambda candidate: (
                        candidate.line_count,
                        candidate.file,
                        candidate.start,
                    ),
                    default=None,
                )
            resolution = "memtrace_symbol_context"
            if resolved is None:
                resolved = local_exact_definition_candidate(
                    repo_root,
                    identifier,
                    excluded_file=anchor.file,
                )
                resolution = "local_exact_definition_fallback"
            if resolved is None:
                event.update({"accepted": False, "reason": "definition_not_resolved"})
                events.append(event)
                continue
            if resolved in expanded:
                event.update({"accepted": False, "reason": "already_selected"})
                events.append(event)
                continue
            if support_lines + resolved.line_count > support_cap:
                event.update({"accepted": False, "reason": "concept_support_cap"})
                events.append(event)
                continue
            if total_lines + resolved.line_count > line_budget:
                event.update({"accepted": False, "reason": "total_line_budget"})
                events.append(event)
                continue
            expanded.append(resolved)
            support_lines += resolved.line_count
            total_lines += resolved.line_count
            pool_entries.append(
                {
                    "id": next_id,
                    "file": resolved.file,
                    "start": resolved.start,
                    "end": resolved.end,
                    "name": resolved.name,
                    "retrieval_lanes": ["concept_boundary"],
                }
            )
            selected_ids.append(next_id)
            event.update(
                {
                    "accepted": True,
                    "reason": "semantic_boundary_definition",
                    "resolution": resolution,
                    "candidate_id": next_id,
                    "candidate": {
                        "file": resolved.file,
                        "start": resolved.start,
                        "end": resolved.end,
                        "name": resolved.name,
                    },
                }
            )
            next_id += 1
            events.append(event)
    coverage["boundary_expansion"] = events
    coverage["boundary_graph_contexts"] = graph_records
    return expanded, graph_records


def validate_concept_boundary_cap(
    final: Iterable[Candidate],
    selector_audit: dict[str, Any] | None,
    line_budget: int,
) -> None:
    """Fail closed if downstream floors violate concept-boundary invariants."""
    if not isinstance(selector_audit, dict):
        return
    coverage = selector_audit.get("concept_coverage")
    pool_entries = selector_audit.get("candidate_pool")
    if not isinstance(coverage, dict) or not isinstance(pool_entries, list):
        return
    anchor_ids = coverage.get("added_candidate_ids", [])
    events = coverage.get("boundary_expansion", [])
    if not isinstance(anchor_ids, list) or not isinstance(events, list):
        return
    protected_ids = {
        value for value in anchor_ids if isinstance(value, int)
    } | {
        event.get("candidate_id")
        for event in events
        if isinstance(event, dict)
        and event.get("accepted") is True
        and isinstance(event.get("candidate_id"), int)
    }
    if not protected_ids:
        return
    entries = {
        entry.get("id"): entry
        for entry in pool_entries
        if isinstance(entry, dict) and entry.get("id") in protected_ids
    }
    covered_lines: set[tuple[str, int]] = set()
    protected_spans: set[tuple[str, int, int]] = set()
    for candidate_id in protected_ids:
        entry = entries.get(candidate_id)
        if not isinstance(entry, dict):
            raise RuntimeError(
                "concept boundary audit lost a protected candidate entry"
            )
        file = str(entry.get("file") or "")
        start = int(entry.get("start") or 0)
        end = int(entry.get("end") or 0)
        if not file or start < 1 or end < start:
            raise RuntimeError("concept boundary audit contains an invalid span")
        protected_spans.add((file, start, end))
        covered_lines.update((file, line) for line in range(start, end + 1))
    support_cap = max(1, line_budget // SELECTOR_CONCEPT_SUPPORT_DIVISOR)
    if len(covered_lines) > support_cap:
        raise RuntimeError(
            "concept boundary candidates exceed the reserved support cap"
        )
    final_spans = {
        (candidate.file, candidate.start, candidate.end) for candidate in final
    }
    if not protected_spans.issubset(final_spans):
        raise RuntimeError(
            "a downstream floor removed a protected concept boundary candidate"
        )


def is_discriminative_name(name: str) -> bool:
    """Names that plausibly identify one code symbol rather than an English
    word: snake_case, camelCase, or long single words."""
    return (
        "_" in name.strip("_")
        or len(name) >= 8
        or re.search(r"[a-z][A-Z]", name) is not None
    )


def name_matches_query_boundary(name: str, lowered_query: str) -> bool:
    """True when the full name occurs in the query at a token or camelCase
    subtoken boundary (the query is pre-lowered, so a following original
    uppercase hump reads as a lowercase letter only inside the same word)."""
    pattern = rf"(?<![a-z0-9_]){re.escape(name.lower())}(?![a-z0-9_])"
    return re.search(pattern, lowered_query) is not None


def candidate_score_v2(candidate: Candidate, query: str) -> float:
    """candidate_score with the S1 discriminative-name gate: the +2.0 exact
    name bonus only fires for identifier-shaped names matched at a token or
    subtoken boundary, so generic English words ("update", "ensure", "IP")
    and names embedded inside larger identifiers stop hijacking packing."""
    query_tokens = Counter(tokenize(query))
    candidate_tokens = Counter(tokenize(f"{candidate.name} {candidate.content}"))
    overlap = sum(min(count, candidate_tokens[token]) for token, count in query_tokens.items())
    score = overlap / max(1, sum(query_tokens.values()))
    lowered_query = query.lower()
    if (
        candidate.name != "__init__"
        and is_discriminative_name(candidate.name)
        and name_matches_query_boundary(candidate.name, lowered_query)
    ):
        score += 2.0
    if candidate.name == "__init__" and any(
        cue in lowered_query
        for cue in ("constructor", "initialize", "create ", "given an ", "given a ", "empty name")
    ):
        score += 1.5
    if candidate.kind.lower() in {"function", "method"}:
        score += 0.2
    if candidate.kind.lower() == "class":
        score -= V2_CLASS_PENALTY
    score += 0.1 / (candidate.source_rank + 1)
    return score


def rank_windows_bm25(
    windows: Iterable[Candidate], query: str
) -> list[Candidate]:
    """Rank slices of one oversized symbol with BM25 over their live source.

    The product reranker correctly identifies the symbol; this second-stage
    ranker decides *where inside it* the line budget should land. Scoring only
    source content (not the shared symbol name) and using set-valued query
    terms prevents a long reproduction from voting for its docstring once per
    repeated word.
    """
    ranked = list(windows)
    if len(ranked) < 2:
        return ranked
    query_terms = set(tokenize(query))
    documents = [tokenize(item.content) for item in ranked]
    if not query_terms or not any(documents):
        return sorted(ranked, key=lambda item: item.start)
    average_length = sum(len(document) for document in documents) / len(documents)
    document_frequency = {
        token: sum(token in document for document in documents)
        for token in query_terms
    }
    scores: dict[tuple[str, int, int], float] = {}
    for item, document in zip(ranked, documents):
        counts = Counter(document)
        length_norm = max(1.0, len(document) / max(1.0, average_length))
        score = 0.0
        for token in query_terms:
            frequency = counts[token]
            if not frequency:
                continue
            frequency_in_documents = document_frequency[token]
            inverse_document_frequency = math.log(
                1.0
                + (len(documents) - frequency_in_documents + 0.5)
                / (frequency_in_documents + 0.5)
            )
            denominator = frequency + 1.2 * (0.25 + 0.75 * length_norm)
            score += inverse_document_frequency * frequency * 2.2 / denominator
        scores[(item.file, item.start, item.end)] = score
    return sorted(
        ranked,
        key=lambda item: (
            -scores[(item.file, item.start, item.end)],
            -candidate_score_v2(item, query),
            item.start,
        ),
    )


def resolve_repo_path(repo_root: Path, raw_path: str) -> str | None:
    """Normalize absolute, source-root-relative, and repo-relative tool paths."""
    path = Path(raw_path)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            return None

    clean = raw_path.replace("\\", "/").lstrip("./")
    if (repo_root / clean).is_file():
        return clean

    matches = [
        candidate
        for candidate in repo_root.rglob(Path(clean).name)
        if candidate.is_file() and candidate.as_posix().endswith(f"/{clean}")
    ]
    if len(matches) == 1:
        return matches[0].relative_to(repo_root).as_posix()
    return None


def candidates_from_rows(
    rows: Iterable[dict[str, Any]], repo_root: Path, source_rank_offset: int = 0
) -> list[Candidate]:
    candidates: list[Candidate] = []
    exact_symbol_identities: set[tuple[Any, ...]] = set()
    for rank, row in enumerate(rows, source_rank_offset):
        file_path = resolve_repo_path(repo_root, str(row.get("file_path") or ""))
        start = row.get("start_line")
        end = row.get("end_line")
        if not file_path or not isinstance(start, int) or not isinstance(end, int):
            continue
        name = str(row.get("scope_path") or row.get("name") or "")
        kind = str(row.get("kind") or "")
        symbol_id = (
            str(row["id"])
            if row.get("id") not in (None, "")
            else ""
        )
        symbol_start = row.get("symbol_start_line")
        symbol_end = row.get("symbol_end_line")
        has_exact_span = (
            isinstance(symbol_start, int)
            and isinstance(symbol_end, int)
            and start <= symbol_start <= symbol_end <= end
        )
        if has_exact_span:
            symbol_identity = (
                ("id", symbol_id)
                if symbol_id
                else ("span", file_path, name, kind, symbol_start, symbol_end)
            )
            if symbol_identity not in exact_symbol_identities:
                exact_symbol_identities.add(symbol_identity)
                if (symbol_start, symbol_end) != (start, end):
                    candidates.append(
                        Candidate(
                            file=file_path,
                            start=symbol_start,
                            end=symbol_end,
                            name=name,
                            kind=kind,
                            source_rank=rank,
                            symbol_id=symbol_id,
                            is_exact_symbol=True,
                        )
                    )
        candidates.append(
            Candidate(
                file=file_path,
                start=start,
                end=end,
                name=name,
                kind=kind,
                content=str(row.get("content") or ""),
                source_rank=rank,
                symbol_id=symbol_id,
                is_exact_symbol=(symbol_start, symbol_end) == (start, end)
                if has_exact_span
                else False,
            )
        )
    return candidates


def rank_refinement_files(
    searches: Iterable[dict[str, Any]],
    repo_root: Path,
    limit: int = DEFAULT_REFINED_FILE_COUNT,
    extended_test_filter: bool = False,
) -> list[str]:
    """Choose high-confidence production files for a second, local search pass.

    A file earns support independently in each semantic query lane. Reciprocal
    rank keeps one strong hit useful without letting a file with many mediocre
    symbols dominate, while cross-lane agreement lifts files that explain the
    symptom and the data flow.
    """
    scores: Counter[str] = Counter()
    lane_hits: Counter[str] = Counter()
    for search in searches:
        seen_in_lane: set[str] = set()
        for rank, row in enumerate(search.get("results", [])):
            file_path = resolve_repo_path(repo_root, str(row.get("file_path") or ""))
            if not file_path or is_test_path(file_path, extended=extended_test_filter):
                continue
            scores[file_path] += 1.0 / (rank + 1)
            if file_path not in seen_in_lane:
                lane_hits[file_path] += 1
                seen_in_lane.add(file_path)
    ranked = sorted(
        scores,
        key=lambda path: (-lane_hits[path], -scores[path], path),
    )
    return ranked[:limit]


def refine_within_files(
    client: "McpClient",
    repo_id: str,
    queries: Iterable[str],
    files: Iterable[str],
    *,
    view: str = "committed",
    line_budget: int | None = None,
) -> list[dict[str, Any]]:
    """Search again inside likely files to recover lower-ranked exact blocks."""
    refined: list[dict[str, Any]] = []
    for file_path in files:
        for query in queries:
            arguments: dict[str, Any] = {
                    "repo_id": repo_id,
                    "query": query,
                    "file_path": file_path,
                    "limit": DEFAULT_FILE_REFINEMENT_LIMIT,
                    "include_diagnostics": True,
                    "view": view,
                }
            if line_budget is not None:
                arguments["line_budget"] = line_budget
            result = client.call_tool("find_code", arguments)
            if isinstance(result, dict):
                refined.append({**result, "refinement_file": file_path})
    return refined


def hydrate_candidate_content(
    candidates: Iterable[Candidate], repo_root: Path
) -> list[Candidate]:
    hydrated: list[Candidate] = []
    for candidate in candidates:
        content = candidate.content
        source_path = repo_root / candidate.file
        if source_path.is_file():
            lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if 1 <= candidate.start <= candidate.end <= len(lines):
                content = "\n".join(lines[candidate.start - 1 : candidate.end])
        hydrated.append(
            Candidate(
                file=candidate.file,
                start=candidate.start,
                end=candidate.end,
                name=candidate.name,
                kind=candidate.kind,
                content=content,
                source_rank=candidate.source_rank,
                symbol_id=candidate.symbol_id,
                is_exact_symbol=candidate.is_exact_symbol,
            )
        )
    return hydrated


def compact_oversized_candidates(
    candidates: Iterable[Candidate],
    query: str,
    line_budget: int,
    window_lines: int = 40,
    overlap_lines: int = 8,
    oversize_threshold: int | None = None,
    max_windows: int | None = 2,
    scorer: Any = None,
    window_ranker: Any = None,
) -> list[Candidate]:
    """Replace oversized symbols with their strongest bounded source windows.

    Defaults preserve v1 behavior exactly (threshold = the full line budget,
    top-2 windows, v1 scoring). Pack-policy v2 windows anything above
    V2_OVERSIZE_THRESHOLD lines and keeps a budget-aware window count so one
    task's pool is not starved down to two 40-line slices of a huge symbol.
    """
    score = scorer or candidate_score
    threshold = line_budget if oversize_threshold is None else oversize_threshold
    compacted: list[Candidate] = []
    window_lines = max(1, min(window_lines, line_budget))
    if max_windows is None:
        max_windows = max(2, line_budget // window_lines)
    stride = max(1, window_lines - overlap_lines)
    for candidate in candidates:
        if candidate.line_count <= threshold or not candidate.content:
            compacted.append(candidate)
            continue
        source_lines = candidate.content.splitlines()
        windows: list[Candidate] = []
        for offset in range(0, len(source_lines), stride):
            chunk = source_lines[offset : offset + window_lines]
            if not chunk:
                continue
            windows.append(
                Candidate(
                    file=candidate.file,
                    start=candidate.start + offset,
                    end=candidate.start + offset + len(chunk) - 1,
                    name=candidate.name,
                    kind=candidate.kind,
                    content="\n".join(chunk),
                    source_rank=candidate.source_rank,
                    symbol_id=candidate.symbol_id,
                    is_exact_symbol=False,
                )
            )
            if offset + window_lines >= len(source_lines):
                break
        if window_ranker is not None:
            windows = window_ranker(windows, query)
        else:
            windows.sort(
                key=lambda item: (-score(item, query), item.start)
            )
        compacted.extend(windows[:max_windows])
    return dedupe_candidates(compacted)


def graph_context_rows(context: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("symbol", "contained_by", "contains", "callers", "callees"):
        value = context.get(key)
        if isinstance(value, dict):
            rows.append(value)
        elif isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
    return rows


def graph_lane_searches_for_floor(
    graph_contexts: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert ordinary graph expansions into pseudo-search lanes.

    Concept-boundary contexts are audit receipts for an already capped,
    protected closure. Feeding them back through the generic lane floor would
    create a second, uncapped expansion path, so they are deliberately excluded.
    """
    searches: list[dict[str, Any]] = []
    for entry in graph_contexts:
        if not isinstance(entry, dict):
            continue
        anchor = entry.get("anchor")
        if isinstance(anchor, dict) and anchor.get("source") == "concept_boundary":
            continue
        context = entry.get("context")
        if isinstance(context, dict):
            searches.append({"results": graph_context_rows(context)})
    return searches


def graph_candidates(context: dict[str, Any], repo_root: Path) -> list[Candidate]:
    return candidates_from_rows(graph_context_rows(context), repo_root)


def select_expansion_rows(
    searches: Iterable[dict[str, Any]],
    per_query_limit: int = DEFAULT_GRAPH_EXPANSIONS_PER_QUERY,
) -> list[dict[str, Any]]:
    expandable = {"class", "function", "method", "struct", "interface"}
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for search in searches:
        taken = 0
        for row in search.get("results", []):
            if str(row.get("kind") or "").lower() not in expandable:
                continue
            key = (
                str(row.get("file_path") or ""),
                str(row.get("scope_path") or row.get("name") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            selected.append(row)
            taken += 1
            if taken >= per_query_limit:
                break
    return selected


def parent_class_anchor(context: dict[str, Any]) -> dict[str, Any] | None:
    parent = context.get("contained_by")
    if not isinstance(parent, dict) or str(parent.get("kind") or "").lower() != "class":
        return None
    if not parent.get("name") or not parent.get("file_path"):
        return None
    return parent


def dedupe_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    best: dict[tuple[str, int, int, str], Candidate] = {}
    for candidate in candidates:
        key = (
            candidate.file,
            candidate.start,
            candidate.end,
            candidate.symbol_id,
        )
        existing = best.get(key)
        if existing is None:
            best[key] = candidate
            continue
        name = existing.name
        if "::" not in name and "::" in candidate.name:
            name = candidate.name
        best[key] = Candidate(
            file=existing.file,
            start=existing.start,
            end=existing.end,
            name=name,
            kind=existing.kind or candidate.kind,
            content=existing.content or candidate.content,
            source_rank=min(existing.source_rank, candidate.source_rank),
            symbol_id=existing.symbol_id or candidate.symbol_id,
            is_exact_symbol=existing.is_exact_symbol or candidate.is_exact_symbol,
        )
    return list(best.values())


def complete_scoped_context(
    chosen: list[Candidate], pool: list[Candidate], line_budget: int
) -> list[Candidate]:
    lines = sum(candidate.line_count for candidate in chosen)
    if lines >= line_budget // 2:
        return chosen
    scopes = {
        candidate.name.rsplit("::", 1)[0]
        for candidate in chosen
        if "::" in candidate.name
    }
    if not scopes:
        return chosen
    completed = list(chosen)
    for candidate in pool:
        if candidate in completed or "::" not in candidate.name:
            continue
        if candidate.name.rsplit("::", 1)[0] not in scopes:
            continue
        if candidate.kind.lower() not in {"function", "method"}:
            continue
        if lines + candidate.line_count > line_budget:
            continue
        completed.append(candidate)
        lines += candidate.line_count
        if lines >= line_budget // 2:
            break
    return completed


def normalize_scoped_selection(
    chosen: list[Candidate], pool: list[Candidate], line_budget: int
) -> list[Candidate]:
    constructors = [candidate for candidate in chosen if candidate.name.endswith("::__init__")]
    if len(constructors) > 1:
        pool_rank = {candidate: rank for rank, candidate in enumerate(pool)}
        first = min(
            constructors,
            key=lambda candidate: (candidate.line_count, pool_rank.get(candidate, len(pool))),
        )
        chosen = [
            candidate
            for candidate in chosen
            if not candidate.name.endswith("::__init__") or candidate == first
        ]

    if sum(candidate.line_count for candidate in chosen) >= line_budget // 2:
        return chosen
    scoped = [candidate for candidate in chosen if "::" in candidate.name]
    if not scoped:
        return chosen
    scope = scoped[0].name.rsplit("::", 1)[0]
    same_scope = [
        candidate
        for candidate in pool
        if candidate.kind.lower() in {"function", "method"}
        and candidate.name.startswith(f"{scope}::")
    ]
    replacement: list[Candidate] = []
    lines = 0
    for candidate in same_scope:
        if lines + candidate.line_count > line_budget:
            continue
        replacement.append(candidate)
        lines += candidate.line_count
        if lines >= line_budget // 2:
            break
    if lines < line_budget // 2:
        return chosen
    for candidate in chosen:
        if candidate.file == replacement[0].file or candidate in replacement:
            continue
        if lines + candidate.line_count <= line_budget:
            replacement.append(candidate)
            lines += candidate.line_count
    return replacement


def apply_retrieval_floor(
    chosen: list[Candidate], pool: list[Candidate], line_budget: int, top_k: int = 2
) -> tuple[list[Candidate], bool]:
    anchors = [
        candidate
        for candidate in pool
        if candidate.kind.lower() in {"function", "method"}
        and candidate.line_count <= line_budget
        and not is_test_path(candidate.file)
    ][:top_k]
    if len(anchors) < top_k or len({candidate.file for candidate in anchors}) != 1:
        return chosen, False
    if any(candidate in chosen for candidate in anchors):
        return chosen, False
    grounded: list[Candidate] = []
    lines = 0
    for candidate in [*anchors, *chosen]:
        if candidate in grounded or lines + candidate.line_count > line_budget:
            continue
        grounded.append(candidate)
        lines += candidate.line_count
    return grounded, True


def span_map(candidates: Iterable[Candidate]) -> dict[str, list[dict[str, int]]]:
    result: dict[str, list[dict[str, int]]] = {}
    for candidate in candidates:
        result.setdefault(candidate.file, []).append(
            {"type": "line", "start": candidate.start, "end": candidate.end}
        )
    return result


def observation_step(candidates: Iterable[Candidate]) -> dict[str, Any] | None:
    observed = dedupe_candidates(candidates)
    if not observed:
        return None
    return {
        "files": sorted({item.file for item in observed}),
        "spans": span_map(observed),
        "symbols": {},
    }


def exact_identifier_candidates(
    exact_queries: Iterable[str],
    search_queries: list[str],
    searches: list[dict[str, Any]],
    repo_root: Path,
) -> list[Candidate]:
    by_query = dict(zip(search_queries, searches))
    anchors: list[Candidate] = []
    for query in exact_queries:
        leaf = query.rsplit(".", 1)[-1]
        search = by_query.get(query, {})
        for row in search.get("results", []):
            if str(row.get("name") or "") != leaf:
                continue
            candidate = candidates_from_rows([row], repo_root)
            if candidate and not is_test_path(candidate[0].file):
                anchors.extend(candidate)
                break
    return dedupe_candidates(anchors)


def apply_exact_identifier_floor(
    chosen: Iterable[Candidate], anchors: Iterable[Candidate], line_budget: int
) -> tuple[list[Candidate], bool]:
    selected = dedupe_candidates(chosen)
    exact = dedupe_candidates(anchors)
    selected_keys = {(candidate.file, candidate.start, candidate.end) for candidate in selected}
    grounded: list[Candidate] = []
    lines = 0
    # `selected` (the selector's own deliberate picks) MUST be greedily
    # filled first, `exact` (identifier-match floor candidates) only fills
    # whatever budget remains. Regression for a real ContextBench forensic
    # finding (prettier/prettier, SWE-PolyBench javascript 3b6e6f3a, guarded
    # mode): GPT-5 explicitly selected only `createParse` (42 lines) with a
    # detailed, correct justification for why the printer-phase exact-
    # identifier matches (printPathNoParens, genericPrint, etc. -- 8
    # candidates totaling 3665 lines, several individually small enough to
    # fit) should NOT be included for this bug. With `[*exact, *selected]`
    # ordering, those smaller exact-identifier candidates greedily consumed
    # nearly the entire 200-line budget before the loop ever reached
    # `createParse`, so the selector's real pick silently never made it into
    # the final prediction while an explicitly-rejected file did. Same
    # underlying principle as the `apply_lane_file_floor` fix above: the
    # selector's own output is the highest-trust signal in this pipeline and
    # must never be starved of budget by a floor-forcing mechanism.
    for candidate in dedupe_candidates([*selected, *exact]):
        if candidate in grounded or lines + candidate.line_count > line_budget:
            continue
        grounded.append(candidate)
        lines += candidate.line_count
    applied = any(
        (anchor.file, anchor.start, anchor.end) not in selected_keys
        and any(
            (candidate.file, candidate.start, candidate.end)
            == (anchor.file, anchor.start, anchor.end)
            for candidate in grounded
        )
        for anchor in exact
    )
    return grounded, applied


def response_usage(response_data: dict[str, Any]) -> dict[str, int]:
    usage = response_data.get("usage", {})
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    return {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "cached_input_tokens": int(input_details.get("cached_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "reasoning_tokens": int(output_details.get("reasoning_tokens", 0) or 0),
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


def response_incomplete_reason(response_data: dict[str, Any]) -> str | None:
    details = response_data.get("incomplete_details") or {}
    if not isinstance(details, dict) or not details.get("reason"):
        return None
    return str(details["reason"])


def response_output_text(response_data: dict[str, Any]) -> str:
    return "".join(
        content.get("text", "")
        for output in response_data.get("output", [])
        if output.get("type") == "message"
        for content in output.get("content", [])
        if content.get("type") == "output_text"
    )


def pack_final_context(
    candidates: Iterable[Candidate], query: str, line_budget: int
) -> list[Candidate]:
    ranked = sorted(
        dedupe_candidates(candidates),
        key=lambda item: (-candidate_score(item, query), item.line_count, item.file, item.start),
    )
    relevant = [item for item in ranked if candidate_score(item, query) >= 0.2]
    if relevant:
        ranked = relevant
    selected: list[Candidate] = []
    lines = 0
    for candidate in ranked:
        if candidate.line_count > line_budget or lines + candidate.line_count > line_budget:
            continue
        selected.append(candidate)
        lines += candidate.line_count
        if lines >= line_budget:
            break
    if not selected and ranked:
        selected.append(ranked[0])
    return selected


def candidate_line_set(candidates: Iterable[Candidate]) -> set[tuple[str, int]]:
    lines: set[tuple[str, int]] = set()
    for candidate in candidates:
        lines.update(
            (candidate.file, line) for line in range(candidate.start, candidate.end + 1)
        )
    return lines


def lane_file_support(
    lane_searches: Iterable[dict[str, Any]], repo_root: Path
) -> tuple[Counter, Counter]:
    """Cross-lane reciprocal-rank support and lane-hit counts per production file."""
    rrf: Counter[str] = Counter()
    lane_hits: Counter[str] = Counter()
    for search in lane_searches:
        seen: set[str] = set()
        for rank, row in enumerate(search.get("results", [])):
            file_path = resolve_repo_path(repo_root, str(row.get("file_path") or ""))
            if not file_path or is_junk_context_path(file_path):
                continue
            rrf[file_path] += 1.0 / (rank + 1)
            if file_path not in seen:
                lane_hits[file_path] += 1
                seen.add(file_path)
    return rrf, lane_hits


def pack_final_context_v2(
    candidates: Iterable[Candidate],
    query: str,
    line_budget: int,
    lane_searches: Iterable[dict[str, Any]] = (),
    repo_root: Path | None = None,
    pack_policy: str = "v2",
) -> list[Candidate]:
    """Adaptive packer (pack-policy v2/v3), replay-validated on the cb-r0 slice.

    Differences from pack_final_context: S1-gated scoring plus a small
    cross-lane consensus blend, junk paths never packed, novel-line
    accounting (overlapping spans stop double-billing the budget), a TIGHT
    anchor regime that stops filling once a dominant anchor file's adjacent
    context is packed, and lane-supported file consolidation otherwise.

    pack_policy="v3" additionally applies a chain relative-score floor to
    the CONSOLIDATED regime's same-file pass (see
    V3_CONSOLIDATION_CHAIN_REL_FLOOR) so low-relevance same-file siblings
    stop diluting precision on single-method gold spans inside large files
    (the cb-r1 django/gson regression). v2 is unchanged and kept available
    for comparison.

    IMPORTANT for the v3 floor: it is computed from the RAW (unblended)
    candidate_score_v2, not from score_of()/`scores`. `scores` includes the
    cross-lane consensus bonus (V2_CONSENSUS_WEIGHT * rrf[file] / max_rrf),
    which is a per-FILE constant added identically to every candidate in
    that file. A relative floor computed against the blended score is
    diluted by that constant offset (verified live on the django task: raw
    scores ranged 0.32-0.61 across loaddata.py's methods, all identical
    +0.5 bonus, so `0.5 * blended_top` never excluded anything -- the
    intended v3 fix silently no-opped). The floor must compare candidates
    on the dimension that actually varies between same-file siblings.
    """
    pool = dedupe_candidates(candidates)
    production = [item for item in pool if not is_junk_context_path(item.file)]
    if production:
        pool = production
    rrf: Counter[str] = Counter()
    lane_hits: Counter[str] = Counter()
    if repo_root is not None:
        rrf, lane_hits = lane_file_support(lane_searches, repo_root)
    max_rrf = max(rrf.values(), default=0.0)

    raw_scores: dict[tuple[str, int, int], float] = {}
    scores: dict[tuple[str, int, int], float] = {}
    for item in pool:
        raw = candidate_score_v2(item, query)
        key = (item.file, item.start, item.end)
        raw_scores[key] = raw
        score = raw
        if max_rrf > 0:
            score += V2_CONSENSUS_WEIGHT * rrf.get(item.file, 0.0) / max_rrf
        scores[key] = score

    def score_of(item: Candidate) -> float:
        return scores[(item.file, item.start, item.end)]

    def raw_score_of(item: Candidate) -> float:
        return raw_scores[(item.file, item.start, item.end)]

    ranked = sorted(
        pool, key=lambda item: (-score_of(item), item.line_count, item.file, item.start)
    )
    if not ranked:
        return []
    relevant = [item for item in ranked if score_of(item) >= V2_RELEVANCE_FLOOR]
    if relevant:
        ranked = relevant
    top = ranked[0]
    file_best: dict[str, float] = {}
    for item in ranked:
        file_best[item.file] = max(file_best.get(item.file, float("-inf")), score_of(item))
    rival = max(
        (best for file, best in file_best.items() if file != top.file), default=0.0
    )
    tight = score_of(top) >= V2_TIGHT_TOP_SCORE and (
        rival <= 0 or score_of(top) >= V2_TIGHT_FILE_DOMINANCE * rival
    )
    if tight:
        return pack_tight_anchor(ranked, top, score_of, line_budget)
    if pack_policy in ("v4", "v5"):
        return pack_anchor_consolidated_v4(ranked, top, score_of, line_budget)
    if pack_policy == "v3":
        return pack_anchor_consolidated(
            ranked, top, score_of, line_budget,
            sibling_rel_floor=V3_CONSOLIDATION_CHAIN_REL_FLOOR,
            sibling_floor_score_of=raw_score_of,
        )
    return pack_anchor_consolidated(ranked, top, score_of, line_budget)


def pack_tight_anchor(
    ranked: list[Candidate],
    top: Candidate,
    score_of: Any,
    line_budget: int,
) -> list[Candidate]:
    """Dominant-anchor regime: pack the top candidate plus same-file siblings
    scoring above a third of the anchor, then deliberately stop instead of
    filling the budget — tight golds lose precision on every extra span.
    A windowed symbol may contribute at most two windows (v1 semantics) so a
    large near-tied class cannot flood the precision regime."""
    selected = [top]
    covered = candidate_line_set(selected)
    floor = score_of(top) * V2_TIGHT_CHAIN_REL_FLOOR
    per_symbol: Counter[str] = Counter({top.name: 1})
    siblings = sorted(
        (item for item in ranked if item.file == top.file and item is not top),
        key=lambda item: (-score_of(item), item.start),
    )
    for item in siblings:
        if score_of(item) < floor or item.line_count > V2_SIBLING_MAX_LINES:
            continue
        if per_symbol[item.name] >= V2_TIGHT_WINDOWS_PER_SYMBOL:
            continue
        novel = candidate_line_set([item]) - covered
        if not novel or len(covered) + len(novel) > line_budget:
            continue
        selected.append(item)
        covered |= novel
        per_symbol[item.name] += 1
    return selected


def pack_anchor_consolidated(
    ranked: list[Candidate],
    top: Candidate,
    score_of: Any,
    line_budget: int,
    sibling_rel_floor: float | None = None,
    sibling_floor_score_of: Any = None,
) -> list[Candidate]:
    """Consolidation regime: spend the budget inside the top-scored file first
    (novel lines only), then spill the remainder onto globally ranked spans.

    sibling_rel_floor (pack-policy v3 only; None reproduces v2 byte-for-byte):
    same-file candidates below sibling_floor_score_of(top) * sibling_rel_floor
    are excluded from BOTH the same-file pass and the global spillover pass,
    so they can never silently consume the budget as low-relevance filler
    ahead of a genuinely cross-file spillover candidate. Cross-file
    candidates are never floor-restricted here (they already cleared the
    caller's global V2_RELEVANCE_FLOOR).

    sibling_floor_score_of defaults to score_of when not given, but callers
    should pass a RAW (unblended) scorer: if score_of includes a per-file
    constant bonus (e.g. pack_final_context_v2's cross-lane consensus term),
    that constant dilutes every same-file candidate identically and the
    floor stops discriminating between them (this is exactly how the first
    v3 attempt silently no-oped on the django task -- see
    pack_final_context_v2's docstring).
    """
    selected: list[Candidate] = []
    covered: set[tuple[str, int]] = set()
    floor_score_of = sibling_floor_score_of if sibling_floor_score_of is not None else score_of
    floor = floor_score_of(top) * sibling_rel_floor if sibling_rel_floor is not None else None

    def below_sibling_floor(item: Candidate) -> bool:
        return floor is not None and item.file == top.file and floor_score_of(item) < floor

    for item in sorted(
        (item for item in ranked if item.file == top.file),
        key=lambda item: (-score_of(item), item.start),
    ):
        if below_sibling_floor(item):
            continue
        novel = candidate_line_set([item]) - covered
        if not novel or len(covered) + len(novel) > line_budget:
            continue
        selected.append(item)
        covered |= novel
    for item in ranked:
        if below_sibling_floor(item):
            continue
        if len(covered) >= line_budget:
            break
        novel = candidate_line_set([item]) - covered
        if not novel or len(covered) + len(novel) > line_budget:
            continue
        selected.append(item)
        covered |= novel
    if not selected and ranked:
        selected.append(ranked[0])
    return selected


def pack_anchor_consolidated_v4(
    ranked: list[Candidate],
    top: Candidate,
    score_of: Any,
    line_budget: int,
) -> list[Candidate]:
    """Consolidation regime v4: structural filler cap (see V4_* constants'
    module comment for why v3's score-threshold approach measurably no-oped
    on live data). Replaces "keep adding same-file/cross-file candidates
    while budget remains and score clears some floor" with a hard, fixed,
    small COUNT cap on additional spans -- same discipline as Agentless's
    fixed window around a hit, applied to symbol-level candidates instead
    of raw line windows.

    Same-file pass: at most V4_MAX_SAME_FILE_SIBLINGS additional DISTINCT
    symbols beyond the anchor. Multiple windows of a symbol already
    selected (oversized-symbol windowing) are extra resolution on an
    accepted hit, not new filler, so they don't consume the cap.

    Spillover pass: at most V4_MAX_SPILLOVER_ITEMS cross-file candidates,
    each still required to score within V4_SPILLOVER_REL_FLOOR of the
    anchor -- a genuinely strong second anchor elsewhere, not marginal
    filler one file over. Stops the instant either cap is hit, regardless
    of remaining line_budget.
    """
    selected: list[Candidate] = [top]
    covered: set[tuple[str, int]] = candidate_line_set([top])
    selected_names: set[str] = {top.name}

    siblings = sorted(
        (item for item in ranked if item.file == top.file and item is not top),
        key=lambda item: (-score_of(item), item.start),
    )
    added_siblings = 0
    for item in siblings:
        is_new_symbol = item.name not in selected_names
        if is_new_symbol and added_siblings >= V4_MAX_SAME_FILE_SIBLINGS:
            continue
        novel = candidate_line_set([item]) - covered
        if not novel or len(covered) + len(novel) > line_budget:
            continue
        selected.append(item)
        covered |= novel
        if is_new_symbol:
            selected_names.add(item.name)
            added_siblings += 1

    spillover_floor = score_of(top) * V4_SPILLOVER_REL_FLOOR
    spilled = 0
    for item in ranked:
        if item.file == top.file:
            continue
        if spilled >= V4_MAX_SPILLOVER_ITEMS:
            break
        if score_of(item) < spillover_floor:
            continue
        if len(covered) >= line_budget:
            break
        novel = candidate_line_set([item]) - covered
        if not novel or len(covered) + len(novel) > line_budget:
            continue
        selected.append(item)
        covered |= novel
        spilled += 1

    return selected


def build_selector_pool(
    candidates: Iterable[Candidate],
    query: str,
    priority_candidates: Iterable[Candidate] = (),
    pool_size: int = DEFAULT_SELECTOR_CANDIDATES,
) -> list[Candidate]:
    if pool_size <= 0:
        return []
    ranking_key = lambda item: (
        -candidate_score(item, query),
        item.line_count,
        item.file,
        item.start,
    )
    priority_groups: dict[tuple[str, ...], list[Candidate]] = {}
    for candidate in dedupe_candidates(priority_candidates):
        priority_groups.setdefault(candidate.symbol_identity, []).append(candidate)

    # Priority is a hard floor for distinct symbols, not an exemption from the
    # per-symbol variant cap. Give every priority symbol one slot before any
    # second variant, preferring its exact span even when widened windows score
    # higher. At most one best widened/windowed variant follows that exact span.
    priority_primary: list[Candidate] = []
    priority_variants: list[Candidate] = []
    for group in priority_groups.values():
        exact_candidates = [candidate for candidate in group if candidate.is_exact_symbol]
        variant_candidates = [candidate for candidate in group if not candidate.is_exact_symbol]
        exact = min(exact_candidates, key=ranking_key) if exact_candidates else None
        best_variant = min(variant_candidates, key=ranking_key) if variant_candidates else None
        primary = exact or best_variant
        if primary is not None:
            priority_primary.append(primary)
        if exact is not None and best_variant is not None:
            priority_variants.append(best_variant)
    priority = [*priority_primary, *priority_variants]
    if len(priority) >= pool_size:
        return priority[:pool_size]
    ranked = sorted(
        dedupe_candidates(candidates),
        key=ranking_key,
    )
    ordered = dedupe_candidates([*priority, *ranked])
    selected = ordered[: len(priority)]
    remaining = ordered[len(priority) :]

    # Insert an exact symbol span immediately before that symbol's strongest
    # widened/windowed variant. This keeps global relevance order intact while
    # guaranteeing that two stronger windows cannot consume both variant slots
    # before the exact candidate is considered.
    exact_by_symbol: dict[tuple[str, ...], Candidate] = {}
    for candidate in remaining:
        if candidate.is_exact_symbol:
            exact_by_symbol.setdefault(candidate.symbol_identity, candidate)
    exact_emitted: set[tuple[str, ...]] = set()
    exact_first: list[Candidate] = []
    for candidate in remaining:
        identity = candidate.symbol_identity
        exact = exact_by_symbol.get(identity)
        if exact is not None and identity not in exact_emitted:
            exact_first.append(exact)
            exact_emitted.add(identity)
        if candidate != exact:
            exact_first.append(candidate)

    file_cap = max(
        REFINEMENT_FILE_PRIORITY_CAP,
        (pool_size + SELECTOR_POOL_FILE_SHARE_DENOMINATOR - 1)
        // SELECTOR_POOL_FILE_SHARE_DENOMINATOR,
    )
    deferred_for_file_diversity: list[Candidate] = []
    symbol_counts: Counter[tuple[str, ...]] = Counter(
        item.symbol_identity for item in selected
    )
    file_counts: Counter[str] = Counter(item.file for item in selected)

    def add_if_symbol_has_room(candidate: Candidate) -> bool:
        if symbol_counts[candidate.symbol_identity] >= SELECTOR_POOL_SYMBOL_VARIANT_CAP:
            return False
        selected.append(candidate)
        symbol_counts[candidate.symbol_identity] += 1
        file_counts[candidate.file] += 1
        return True

    for candidate in exact_first:
        if file_counts[candidate.file] >= file_cap:
            deferred_for_file_diversity.append(candidate)
            continue
        add_if_symbol_has_room(candidate)
        if len(selected) >= pool_size:
            return selected

    # The per-file share is a diversity preference, not a hard loss of
    # coverage. If there were too few alternatives, relax only that cap while
    # retaining the per-symbol bound that prevents overlapping-window floods.
    for candidate in deferred_for_file_diversity:
        add_if_symbol_has_room(candidate)
        if len(selected) >= pool_size:
            break
    return selected


def cross_lane_consensus_priority(
    candidates: Iterable[Candidate],
    semantic_queries: Iterable[str],
    file_query_families: dict[str, Iterable[str]],
    root_lanes: Iterable[str] = ROOT_RETRIEVAL_LANES,
    *,
    line_budget: int,
    max_files: int = CONSENSUS_PRIORITY_MAX_FILES,
    per_file: int = CONSENSUS_PRIORITY_PER_FILE,
) -> list[Candidate]:
    """Floor cross-lane-confirmed production files into the selector pool.

    The product reranker is intentionally allowed to disagree with lexical
    retrieval.  A stable failure mode is therefore a correct file appearing
    deep in two or three independent searches but never reaching the bounded
    selector pool.  File-level query-family agreement is the task-agnostic
    recovery signal; candidate relevance only decides which small spans from
    that already-confirmed file get the reserved slots.
    """
    if max_files <= 0 or per_file <= 0:
        return []
    queries = [query for query in semantic_queries if query]
    roots = set(root_lanes)
    families_by_file = {
        file: set(families) & roots
        for file, families in file_query_families.items()
    }
    eligible_files = {
        file: families
        for file, families in families_by_file.items()
        if len(families) >= CONSENSUS_PRIORITY_MIN_ROOT_LANES
        and not is_junk_context_path(file)
    }
    if not eligible_files:
        return []

    def relevance(candidate: Candidate) -> float:
        return max(
            (candidate_score_v2(candidate, query) for query in queries),
            default=0.0,
        )

    by_file: dict[str, list[Candidate]] = {}
    for candidate in dedupe_candidates(candidates):
        if candidate.file not in eligible_files or candidate.line_count > line_budget:
            continue
        by_file.setdefault(candidate.file, []).append(candidate)
    ranked_files = sorted(
        by_file,
        key=lambda file: (
            -len(eligible_files[file]),
            -max((relevance(item) for item in by_file[file]), default=0.0),
            min((item.source_rank for item in by_file[file]), default=10**9),
            file,
        ),
    )[:max_files]
    priority: list[Candidate] = []
    for file in ranked_files:
        priority.extend(
            sorted(
                by_file[file],
                key=lambda item: (
                    -relevance(item),
                    0 if item.is_exact_symbol else 1,
                    item.line_count,
                    item.source_rank,
                    item.start,
                ),
            )[:per_file]
        )
    return dedupe_candidates(priority)


CAUSAL_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{7,}\b")
CAUSAL_IDENTIFIER_STOP_WORDS = {
    "arguments", "candidate", "candidates", "configuration", "context",
    "exception", "function", "identifier", "instance", "location",
    "parameter", "parameters", "response", "selected", "selection",
}


def causal_identifiers(content: str) -> set[str]:
    return {
        token
        for token in CAUSAL_IDENTIFIER_RE.findall(content)
        # Store/cache/marker keys are a strong producer-consumer signal across
        # languages.  Generic shared parameters (field_name, lookup_type) and
        # module flags are not: replay showed those create unrelated sibling
        # closure and dilute precision.
        if token.islower()
        and token.endswith("_key")
        and token.casefold() not in CAUSAL_IDENTIFIER_STOP_WORDS
        and not token.startswith("__")
    }


def assigned_causal_identifiers(content: str) -> set[str]:
    """Long snake-case names occurring on an assignment's producer side."""
    assigned: set[str] = set()
    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].split("//", 1)[0]
        assignment = re.search(r"(?<![=!<>])=(?!=|>)", line)
        if assignment is None:
            continue
        assigned.update(causal_identifiers(line[: assignment.start()]))
    return assigned


def expand_causal_same_file_producers(
    chosen: Iterable[Candidate],
    pool: Iterable[Candidate],
    line_budget: int,
    *,
    max_candidates: int = CAUSAL_CLOSURE_MAX_CANDIDATES,
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Add at most one compact same-file producer for a selected data key."""
    selected = dedupe_candidates(chosen)
    if max_candidates <= 0 or not selected:
        return selected, []
    covered = candidate_line_set(selected)
    total_lines = len(covered)
    support_cap = max(1, line_budget // CAUSAL_CLOSURE_LINE_DIVISOR)
    selected_by_file: dict[str, list[Candidate]] = {}
    for candidate in selected:
        selected_by_file.setdefault(candidate.file, []).append(candidate)

    ranked: list[tuple[tuple[Any, ...], Candidate, list[str]]] = []
    for candidate in dedupe_candidates(pool):
        anchors = selected_by_file.get(candidate.file)
        if (
            not anchors
            or candidate in selected
            or is_junk_context_path(candidate.file)
            or candidate.line_count > support_cap
        ):
            continue
        produced = assigned_causal_identifiers(candidate.content)
        if not produced:
            continue
        for anchor in anchors:
            shared = sorted(produced & causal_identifiers(anchor.content))
            if not shared:
                continue
            novel = candidate_line_set([candidate]) - covered
            if not novel or len(novel) > support_cap:
                continue
            distance = min(
                abs(candidate.end - anchor.start),
                abs(anchor.end - candidate.start),
            )
            ranked.append(
                (
                    (
                        -len(shared),
                        distance,
                        candidate.line_count,
                        candidate.source_rank,
                        candidate.start,
                    ),
                    candidate,
                    shared,
                )
            )
            break

    events: list[dict[str, Any]] = []
    for _rank, candidate, shared in sorted(ranked, key=lambda row: row[0]):
        if len(events) >= max_candidates:
            break
        novel = candidate_line_set([candidate]) - covered
        if total_lines + len(novel) > line_budget:
            continue
        selected.append(candidate)
        covered |= novel
        total_lines += len(novel)
        events.append(
            {
                "file": candidate.file,
                "start": candidate.start,
                "end": candidate.end,
                "name": candidate.name,
                "shared_identifiers": shared,
                "novel_lines": len(novel),
            }
        )
    return dedupe_candidates(selected), events


def lane_top_candidates(
    lane_searches: Iterable[dict[str, Any]],
    pool: list[Candidate],
    repo_root: Path,
    line_budget: int,
    per_lane_limit: int = 2,
) -> list[Candidate]:
    """Top cross-encoder-scored candidates from each retrieval lane.

    For every lane -- a planned-query search, a file-refinement search, or a
    graph-expansion context wrapped as a pseudo-search -- resolve up to
    ``per_lane_limit`` distinct-file non-test result rows to their pool
    representative: the exact span when present, else the best-ranked
    candidate from the same file that fits the budget on its own.

    A lane's first hit repeating a file another lane already floored does
    not stop the scan (unlike the prior top-1-only behavior): the lane keeps
    looking for its next-best distinct file, since siblings that share a
    lane profile with an already-selected file (e.g. a sibling config/cache
    pair surfaced by the same query) are exactly the candidates this floor
    exists to rescue.
    """
    tops: list[Candidate] = []
    seen_files: set[str] = set()
    for search in lane_searches:
        lane_files: set[str] = set()
        for row in search.get("results", []):
            if len(lane_files) >= per_lane_limit:
                break
            resolved = candidates_from_rows([row], repo_root)
            if not resolved or is_test_path(resolved[0].file):
                continue
            anchor = resolved[0]
            if anchor.file in seen_files or anchor.file in lane_files:
                continue
            match = next(
                (
                    item
                    for item in pool
                    if (item.file, item.start, item.end)
                    == (anchor.file, anchor.start, anchor.end)
                ),
                None,
            )
            if match is None:
                in_file = [
                    item
                    for item in pool
                    if item.file == anchor.file and item.line_count <= line_budget
                ]
                match = min(in_file, key=lambda item: item.source_rank, default=None)
            if match is not None:
                seen_files.add(match.file)
                lane_files.add(match.file)
                tops.append(match)
    return dedupe_candidates(tops)


def apply_lane_file_floor(
    chosen: list[Candidate],
    floor_candidates: list[Candidate],
    protected: list[Candidate],
    line_budget: int,
    query: str,
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Force-include lane-top candidates whose files are absent from the selection.

    Budget permitting; evicts the lowest-scored unprotected selections when the
    budget is exhausted. Floor and exact-anchor candidates are never evicted.
    """
    selected = list(chosen)
    protected_keys = {
        (item.file, item.start, item.end)
        for item in [*protected, *floor_candidates]
    }
    events: list[dict[str, Any]] = []
    for anchor in floor_candidates:
        if anchor.file in {item.file for item in selected}:
            continue
        if anchor.line_count > line_budget:
            continue
        lines = sum(item.line_count for item in selected)
        evictable = sorted(
            (
                item
                for item in selected
                if (item.file, item.start, item.end) not in protected_keys
            ),
            key=lambda item: candidate_score(item, query),
        )
        evicted: list[Candidate] = []
        while lines + anchor.line_count > line_budget and evictable:
            victim = evictable.pop(0)
            selected.remove(victim)
            evicted.append(victim)
            lines -= victim.line_count
        if lines + anchor.line_count > line_budget:
            selected.extend(evicted)
            continue
        selected.append(anchor)
        events.append(
            {
                "file": anchor.file,
                "start": anchor.start,
                "end": anchor.end,
                "evicted": [
                    {"file": item.file, "start": item.start, "end": item.end}
                    for item in evicted
                ],
            }
        )
    return selected, events


def select_context_with_gpt(
    candidates: Iterable[Candidate],
    query: str,
    line_budget: int,
    model: str,
    priority_candidates: Iterable[Candidate] = (),
    candidate_lanes: dict[tuple[str, int, int], list[str]] | None = None,
    selector_mode: str = "default",
    exact_match_files: set[str] | None = None,
    selector_policy: str = DEFAULT_SELECTOR_POLICY,
    concept_queries: Iterable[str] = (),
    file_query_families: dict[str, Iterable[str]] | None = None,
    ranking_query: str | None = None,
) -> tuple[list[Candidate], dict[str, Any]]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required when --selector-model is set")
    if selector_policy not in SELECTOR_POLICY_CHOICES:
        raise ValueError(
            f"unknown selector policy {selector_policy!r}; "
            f"expected one of {SELECTOR_POLICY_CHOICES}"
        )

    guarded = selector_mode == "guarded"
    concept_queries = tuple(str(value) for value in concept_queries)
    pool_size = GUARDED_SELECTOR_CANDIDATES if guarded else DEFAULT_SELECTOR_CANDIDATES
    pool = build_selector_pool(
        candidates, ranking_query or query, priority_candidates, pool_size
    )
    max_items = GUARDED_SELECTOR_MAX_ITEMS
    if guarded:
        # Scale the selector's item cap up when the pool is dominated by
        # candidates from files already confirmed relevant by cross-lane
        # agreement (the "file_refinement" lane) or exact-identifier match.
        # A flat cap starves legitimately concentrated, symbol-dense change
        # surfaces (many small sibling functions in one or two files); the
        # final selection is still bounded by line_budget regardless of how
        # many candidate_ids the schema allows, so this mainly costs prompt
        # size, not budget overrun.
        established_files = set(exact_match_files or set()) | {
            file
            for (file, _start, _end), lanes in (candidate_lanes or {}).items()
            if "file_refinement" in lanes
        }
        established_pool_count = sum(1 for item in pool if item.file in established_files)
        max_items = min(
            GUARDED_SELECTOR_MAX_ITEMS_CAP,
            max(GUARDED_SELECTOR_MAX_ITEMS, established_pool_count),
        )
    candidate_payload = [
        {
            "id": index,
            "file": item.file,
            "start": item.start,
            "end": item.end,
            "lines": item.line_count,
            "name": item.name,
            "kind": item.kind,
            "content": item.content[:1600],
            "retrieval_lanes": (candidate_lanes or {}).get(
                (item.file, item.start, item.end), []
            ),
            **(
                {"exact_identifier_match": item.file in (exact_match_files or set())}
                if guarded
                else {}
            ),
        }
        for index, item in enumerate(pool)
    ]
    concept_requirements = (
        selector_concept_requirements(
            pool,
            query,
            concept_queries,
            line_budget,
            candidate_lanes=candidate_lanes,
            file_query_families=file_query_families,
        )
        if guarded
        else []
    )
    prompt = {
        "issue": query,
        "line_budget": line_budget,
        "candidates": candidate_payload,
    }
    instructions = (
        "Select a compact but complete code change surface for resolving the repository issue. "
        "Include every candidate directly likely to require modification or needed to trace the "
        "causal value flow. For multi-function bugs, include the relevant producer, transformer, "
        "and consumer instead of selecting only one symptom-level method. Respect the total line "
        "budget, prefer exact functions or methods over whole classes, and avoid tests unless they "
        "define essential behavior. Use retrieval_lanes as provenance, not proof. When the issue "
        "is underspecified and several small input_transform candidates remain plausible, retain "
        "the competing transforms within budget instead of prematurely selecting one."
    )
    if guarded:
        instructions += (
            " Guarded mode: a candidate marked exact_identifier_match=true whose file you leave "
            "entirely unselected may only be omitted if you state why in omission_justification; "
            "when unsure, include it. The true change surface frequently spans multiple files. "
            "Before finalizing, re-scan the candidate list for any unselected candidate that "
            "shares 2 or more retrieval_lanes with an already-selected candidate but lives in a "
            "different file; include it unless you can state a specific, concrete reason the "
            "underlying commit would not touch that file -- do not omit it merely because your "
            "current explanation already covers the bug without it. Do not exclude a candidate "
            "solely because it is a test file: real fixes commonly also update an existing test "
            "that encodes the old or changed behavior. Exclude a test candidate only if it "
            "clearly does not exercise the changed behavior."
        )
    schema_properties: dict[str, Any] = {
        "candidate_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "maxItems": max_items if guarded else 10,
        }
    }
    schema_required = ["candidate_ids"]
    if guarded:
        schema_properties["omission_justification"] = {"type": "string"}
        schema_required.append("omission_justification")
    request_body = {
        "model": model,
        # Guarded mode pays for a 3x larger candidate pool specifically because
        # the cross-file inclusion judgment is harder; match reasoning effort
        # to that premise instead of leaving it at the default-mode setting.
        "reasoning": {"effort": "medium" if guarded else "low"},
        "instructions": instructions,
        "input": json.dumps(prompt, separators=(",", ":")),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "context_selection",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": schema_properties,
                    "required": schema_required,
                    "additionalProperties": False,
                },
            }
        },
        "max_output_tokens": 4000,
    }
    candidate_pool_sha256 = hashlib.sha256(
        json.dumps(candidate_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    attempt_usages: list[dict[str, int]] = []
    logical_attempts: list[dict[str, Any]] = []

    def send_selector_attempt(
        body: dict[str, Any],
        strategy: str,
        full_candidate_payload: bool,
        previous_response_id: str | None = None,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        result = send_openai_request(request, "OpenAI Responses API")
        usage = response_usage(result)
        attempt_usages.append(usage)
        output_text = response_output_text(result)
        logical_attempts.append(
            {
                "sequence": len(logical_attempts) + 1,
                "strategy": strategy,
                "response_id": result.get("id"),
                "previous_response_id": previous_response_id,
                "response_status": result.get("status"),
                "incomplete_reason": response_incomplete_reason(result),
                "max_output_tokens": int(body["max_output_tokens"]),
                "full_candidate_payload": full_candidate_payload,
                "input_sha256": hashlib.sha256(
                    str(body.get("input", "")).encode("utf-8")
                ).hexdigest(),
                "instructions_sha256": hashlib.sha256(
                    str(body.get("instructions", "")).encode("utf-8")
                ).hexdigest(),
                "schema_sha256": hashlib.sha256(
                    json.dumps(
                        body.get("text", {}).get("format", {}),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "candidate_pool_sha256": candidate_pool_sha256,
                "output_text_sha256": (
                    hashlib.sha256(output_text.encode("utf-8")).hexdigest()
                    if output_text
                    else None
                ),
                "parse_status": (
                    "pending" if result.get("status") == "completed" else "incomplete"
                ),
                "selected_candidate_ids": None,
                "usage": usage,
            }
        )
        return result

    # Guarded mode's wider candidate pool (up to GUARDED_SELECTOR_CANDIDATES)
    # and higher reasoning effort both consume more of the shared
    # input+reasoning+output token budget before any structured output is
    # emitted; scale headroom with pool size instead of a fixed token cap.
    base_tokens = 4000 + (max(0, len(pool) - 40) * 20 if guarded else 0)
    response_data: dict[str, Any] = {}
    request_fallback_reason: str | None = None
    if selector_policy == "continuation-v2":
        initial_body = {**request_body, "max_output_tokens": base_tokens * 2}
        response_data = send_selector_attempt(initial_body, "initial", True)
        incomplete_reason = response_incomplete_reason(response_data)
        if response_data.get("status") != "completed":
            response_id = response_data.get("id")
            if (
                incomplete_reason == "max_output_tokens"
                and isinstance(response_id, str)
                and response_id
            ):
                continuation_body = {
                    "model": model,
                    "reasoning": request_body["reasoning"],
                    # Instructions are not inherited with previous_response_id.
                    "instructions": instructions,
                    "input": (
                        "Finish the prior context selection now. Return only the final "
                        "JSON object required by the context_selection schema."
                    ),
                    "text": request_body["text"],
                    "max_output_tokens": base_tokens * 2,
                    "previous_response_id": response_id,
                }
                response_data = send_selector_attempt(
                    continuation_body,
                    "continuation",
                    False,
                    previous_response_id=response_id,
                )
                if response_data.get("status") != "completed":
                    reason = response_incomplete_reason(response_data)
                    request_fallback_reason = (
                        "continuation_incomplete:"
                        f"{reason or response_data.get('status') or 'unknown'}"
                    )
            elif incomplete_reason == "max_output_tokens":
                request_fallback_reason = "missing_response_id"
            else:
                request_fallback_reason = (
                    "non_retryable_incomplete:"
                    f"{incomplete_reason or response_data.get('status') or 'unknown'}"
                )
    else:
        # replay-v1 intentionally preserves the original live policy exactly.
        max_attempts = 3 if guarded else 2
        for attempt in range(max_attempts):
            attempt_body = {
                **request_body,
                "max_output_tokens": base_tokens * (attempt + 1),
            }
            response_data = send_selector_attempt(
                attempt_body,
                "initial" if attempt == 0 else "full_replay",
                True,
            )
            if response_data.get("status") == "completed":
                break
        if response_data.get("status") != "completed":
            reason = response_incomplete_reason(response_data)
            request_fallback_reason = (
                f"selector_incomplete:{reason or response_data.get('status') or 'unknown'}"
            )

    output_text = response_output_text(response_data)
    # A non-"completed" status (or genuinely malformed/truncated JSON from a
    # completed-but-clipped response) must never crash the whole retrieval:
    # falling through to an empty selection routes into the existing
    # fallback_used=True path below (deterministic pack_final_context over
    # the pool), producing a real, present-but-imperfect answer instead of
    # zero prediction. Previously this raised RuntimeError and the instance
    # produced NO prediction at all -- worse than any fallback answer.
    selection_parse_status = "empty_output"
    selection: dict[str, Any] = {"candidate_ids": []}
    if selector_policy == "continuation-v2" and response_data.get("status") != "completed":
        selection_parse_status = "incomplete"
    elif output_text:
        try:
            parsed_selection = json.loads(output_text)
        except json.JSONDecodeError:
            selection_parse_status = "malformed_json"
        else:
            if isinstance(parsed_selection, dict):
                selection = parsed_selection
                selection_parse_status = "parsed"
            else:
                selection_parse_status = "invalid_shape"
    raw_ids = selection.get("candidate_ids", [])
    if not isinstance(raw_ids, list):
        raw_ids = []
        selection_parse_status = "invalid_candidate_ids"
    if logical_attempts:
        logical_attempts[-1]["parse_status"] = selection_parse_status
        logical_attempts[-1]["selected_candidate_ids"] = raw_ids
    concept_seed_ids, concept_added_ids, model_surfaces = selector_concept_seed_ids(
        concept_requirements,
        pool,
        raw_ids,
        line_budget,
    )
    concept_support_cap = max(
        (
            int(requirement.get("reserved_line_budget", 0))
            for requirement in concept_requirements
        ),
        default=0,
    )
    concept_anchor_lines = sum(pool[value].line_count for value in concept_added_ids)
    boundary_reserve = (
        max(0, concept_support_cap - concept_anchor_lines)
        if concept_added_ids
        else 0
    )
    selection_line_budget = max(1, line_budget - boundary_reserve)
    ordered_ids = [*concept_seed_ids, *raw_ids]
    chosen: list[Candidate] = []
    lines = 0
    for candidate_id in ordered_ids:
        if (
            isinstance(candidate_id, bool)
            or not isinstance(candidate_id, int)
            or not 0 <= candidate_id < len(pool)
        ):
            continue
        candidate = pool[candidate_id]
        if candidate in chosen or candidate.line_count > selection_line_budget:
            continue
        if lines + candidate.line_count > selection_line_budget:
            continue
        chosen.append(candidate)
        lines += candidate.line_count
    fallback_used = not bool(chosen)
    fallback_reason: str | None = None
    retrieval_floor_applied = False
    if fallback_used:
        if request_fallback_reason:
            fallback_reason = request_fallback_reason
        elif selection_parse_status != "parsed":
            fallback_reason = selection_parse_status
        elif not raw_ids:
            fallback_reason = "empty_selection"
        else:
            fallback_reason = "no_valid_candidate_ids"
        chosen = pack_final_context(pool, query, line_budget)
    elif guarded:
        # Guarded mode keeps cross-file selections: no single-scope
        # normalization and no single-file retrieval floor.
        chosen = complete_scoped_context(chosen, pool, selection_line_budget)
    else:
        chosen = complete_scoped_context(chosen, pool, line_budget)
        chosen = normalize_scoped_selection(chosen, pool, line_budget)
        chosen, retrieval_floor_applied = apply_retrieval_floor(
            chosen, pool, line_budget
        )

    # The post-selector policy protects this list. Record candidates that
    # actually survived validation, budget accounting, deterministic fallback,
    # and scope completion -- never raw model IDs that were invalid or did not
    # fit. This keeps live replay faithful and prevents an over-budget raw
    # response from turning an otherwise recoverable task into a policy error.
    pool_ids = {candidate: index for index, candidate in enumerate(pool)}
    accepted_ids = list(
        dict.fromkeys(
            pool_ids[candidate]
            for candidate in chosen
            if candidate in pool_ids
        )
    )

    total_usage = {
        key: sum(usage[key] for usage in attempt_usages)
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
        )
    }
    audit = {
        "model": model,
        "selector_mode": selector_mode,
        "selector_policy": selector_policy,
        "response_id": response_data.get("id"),
        "response_status": response_data.get("status"),
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "retrieval_floor_applied": retrieval_floor_applied,
        "omission_justification": selection.get("omission_justification"),
        "model_candidate_ids": raw_ids,
        "selected_candidate_ids": accepted_ids,
        "concept_coverage": {
            "policy_version": SELECTOR_CONCEPT_POLICY_VERSION,
            "requirements": concept_requirements,
            "model_surfaces": model_surfaces,
            "seeded_candidate_ids": concept_seed_ids,
            "added_candidate_ids": concept_added_ids,
            "boundary_line_reserve": boundary_reserve,
            "applied": bool(concept_added_ids),
        },
        "candidate_pool": [
            {
                "id": index,
                "file": candidate.file,
                "start": candidate.start,
                "end": candidate.end,
                "name": candidate.name,
                "retrieval_lanes": (candidate_lanes or {}).get(
                    (candidate.file, candidate.start, candidate.end), []
                ),
            }
            for index, candidate in enumerate(pool)
        ],
        "attempts": len(attempt_usages),
        "attempt_count_semantics": "logical_selector_requests",
        "logical_attempts": logical_attempts,
        **total_usage,
    }
    return chosen, audit


def plan_search_queries_with_gpt(issue: str, model: str) -> tuple[list[str], dict[str, Any]]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for GPT search planning")
    request_body = {
        "model": model,
        "reasoning": {"effort": "low"},
        "instructions": (
            "Turn a repository issue into exactly three distinct code-search queries, each "
            "under 60 words. symptom_query searches the reported behavior and errors without "
            "committing to a cause. data_flow_query traces the likely caller/callee path from "
            "input to failure. input_transform_query searches parsing, normalization, validation, "
            "serialization, or state transformation that could corrupt the relevant input. Name "
            "code concepts when justified, but do not invent repository-specific symbol names "
            "that are absent from the issue. Use plain semantic phrases, never Boolean operators, "
            "file filters, or search-engine syntax. Do not repeat one hypothesis across all lanes. "
            "Omit environment dumps, reproduction boilerplate, and speculative fixes. Do not "
            "assume access to the patch or answer."
        ),
        "input": issue,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "search_queries",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "symptom_query": {"type": "string"},
                        "data_flow_query": {"type": "string"},
                        "input_transform_query": {"type": "string"},
                    },
                    "required": [
                        "symptom_query",
                        "data_flow_query",
                        "input_transform_query",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "max_output_tokens": 1200,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    response_data = send_openai_request(request, "OpenAI search planning")
    output_text = "".join(
        content.get("text", "")
        for output in response_data.get("output", [])
        if output.get("type") == "message"
        for content in output.get("content", [])
        if content.get("type") == "output_text"
    )
    # A malformed/truncated JSON response (e.g. "Unterminated string" from a
    # response clipped mid-generation) must never crash the whole retrieval
    # -- same principle as the guarded-selector fix above (fallback_used
    # path). Real ContextBench forensic case (prettier/prettier): this raised
    # json.decoder.JSONDecodeError and the instance produced NO prediction at
    # all. Fall back to the raw issue text as a single query so the pipeline
    # can still search for SOMETHING instead of failing outright -- worse
    # than one imperfect query is zero queries.
    try:
        planned = json.loads(output_text)
    except json.JSONDecodeError:
        planned = {}
    queries = [
        planned[key].strip()
        for key in ("symptom_query", "data_flow_query", "input_transform_query")
        if isinstance(planned.get(key), str) and planned[key].strip()
    ]
    if not queries:
        fallback_query = issue.strip()[:400]
        if not fallback_query:
            raise RuntimeError("GPT search planning returned no queries")
        queries = [fallback_query]
    return queries, {
        "model": model,
        "response_id": response_data.get("id"),
        **response_usage(response_data),
    }


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip("'\""))


def graph_cache_key(
    repo_url: str, base_commit: str, namespace: str = "contextbench-v1"
) -> str:
    payload = json.dumps(
        {
            "namespace": namespace,
            "repo_url": repo_url.rstrip("/"),
            "base_commit": base_commit,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def graph_cache_manifest(
    instance_id: str,
    repo_url: str,
    base_commit: str,
    namespace: str,
    repo_root: Path,
) -> dict[str, str]:
    return {
        "instance_id": instance_id,
        "repo_url": repo_url.rstrip("/"),
        "base_commit": base_commit,
        "namespace": namespace,
        "workspace_root": str(repo_root.resolve()),
    }


def graph_cache_is_reusable(cache_entry: Path, expected: dict[str, str]) -> bool:
    manifest_path = cache_entry / "complete.json"
    if not manifest_path.is_file():
        return False
    try:
        actual = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return all(actual.get(key) == value for key, value in expected.items())


def write_graph_cache_manifest(cache_entry: Path, manifest: dict[str, str]) -> None:
    cache_entry.mkdir(parents=True, exist_ok=True)
    temporary = cache_entry / f"complete.json.tmp-{os.getpid()}"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, cache_entry / "complete.json")


def memtrace_env(
    repo_root: Path,
    work_dir: Path,
    rerank_model_dir: Path | None = None,
    graph_cache_dir: Path | None = None,
    walker_include_dirs: Iterable[str] = (),
) -> dict[str, str]:
    model_dir = rerank_model_dir or Path(
        os.environ.get(
            "MEMTRACE_RERANK_MODEL_DIR",
            Path.home() / ".memtrace/rerank-models/ms-marco-MiniLM-L-12-v2",
        )
    )
    return {
        **os.environ,
        "CI": "1",
        "MEMTRACE_HEADLESS": "1",
        # Cortex isolation (universal-on since 0.8.3). "off" is the ONLY value
        # read_persisted_opt_out() accepts, and it only gates sidecar SPAWN;
        # the client proxies gate purely on the socket path, so the store dir
        # must ALSO point away from the user's live ~/.memtrace/cortex-store.
        "MEMTRACE_CORTEX": "off",
        "MEMCORTEX_STORE_DIR": str((graph_cache_dir or work_dir) / "cortex-store"),
        "MEMTRACE_MEMDB_MODE": "embedded",
        "MEMTRACE_MEMDB_DATA_DIR": str((graph_cache_dir or work_dir) / "memdb"),
        "MEMTRACE_MEMDB_LOOPBACK_PORT": "0",
        "MEMTRACE_DATA_DIR": str((graph_cache_dir or work_dir) / "state"),
        "MEMTRACE_NO_RTK_INIT": "1",
        "MEMTRACE_NO_RTK_PROMPT": "1",
        "MEMTRACE_RERANK": "on",
        "MEMTRACE_RERANK_MODEL_DIR": str(model_dir),
        "MEMTRACE_WORKSPACE_ROOT": str(repo_root),
        # ContextBench contains authored TypeScript declaration surfaces
        # (notably MUI and VS Code). Memtrace deliberately makes these opt-in
        # because generated .d.ts files can be noisy in ordinary repos.
        "MEMTRACE_INDEX_DECLARATION_FILES": "1",
        # Hard EXCLUDED_DIRS are pruned before .memtraceignore matching. The
        # product's explicit override is the only effective way to recover
        # tracked first-party source under names such as bin/, out/, or pkg/.
        "MEMTRACE_WALKER_INCLUDE_DIRS": ",".join(sorted(set(walker_include_dirs))),
    }


def write_tracked_dir_reincludes(
    repo_root: Path, min_tracked_sources: int = 1
) -> list[str]:
    """Re-include default-excluded directory names holding tracked source.

    Memtrace's built-in exclusions assume names like pkg/, bin/, out/ are build
    artifacts. Hard EXCLUDED_DIRS are reversed through the returned names and
    MEMTRACE_WALKER_INCLUDE_DIRS; DEFAULT_NOISE_PATTERNS (notably build/) are
    reversed through `.memtraceignore`. Every directory component is counted,
    not only the top level, because real source lives under paths such as
    `src/bin` and `src/build`. Repo-agnostic: keyed only on git-tracked source
    files — with no task- or gold-derived input. Clearly generated/dependency
    roots (`dist`, `vendor`) stay excluded even when tracked.
    """
    tracked = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    counts: Counter[str] = Counter()
    for path in tracked:
        parts = Path(path).parts
        if len(parts) < 2 or Path(path).suffix not in REINCLUDE_SOURCE_EXTENSIONS:
            continue
        for directory in set(parts[:-1]) & set(DEFAULT_EXCLUDED_DIR_COLLISIONS):
            counts[directory] += 1
    reincluded = sorted(name for name, n in counts.items() if n >= min_tracked_sources)
    if reincluded:
        ignore_path = repo_root / ".memtraceignore"
        existing = ignore_path.read_text(encoding="utf-8") if ignore_path.is_file() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        patterns = [
            pattern
            for name in reincluded
            for pattern in (f"!**/{name}/", f"!**/{name}/**")
        ]
        ignore_path.write_text(existing + "\n".join(patterns) + "\n", encoding="utf-8")
    return reincluded


def ensure_checkout(repo_url: str, base_commit: str, repo_root: Path) -> None:
    if not (repo_root / ".git").exists():
        repo_root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", repo_url, str(repo_root)], check=True)
    subprocess.run(["git", "-C", str(repo_root), "fetch", "--quiet", "origin", base_commit], check=True)
    subprocess.run(["git", "-C", str(repo_root), "checkout", "--quiet", "--detach", base_commit], check=True)


def validate_rerank_model(model_dir: Path) -> None:
    if not (model_dir / "tokenizer.json").is_file() or not any(
        (model_dir / name).is_file() for name in ("model_int8.onnx", "model.onnx")
    ):
        raise RuntimeError(
            f"real cross-encoder model is missing from {model_dir}; refusing lexical fallback"
        )


def apply_post_selector_policy(
    prediction: dict[str, Any],
    audit: dict[str, Any],
    *,
    policy_name: str,
    line_budget: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply the explicit next-run policy after raw prediction/audit creation."""
    if policy_name == "off":
        return prediction, audit
    projected, evidence = apply_runtime_policy(
        prediction,
        audit,
        policy_name=policy_name,
        line_budget=line_budget,
    )
    return projected, {**audit, "post_selector_policy": evidence}


def retrieve_instance(
    row: dict[str, Any],
    repo_root: Path,
    env: dict[str, str],
    line_budget: int,
    selector_model: str | None = None,
    planned_search_queries: list[str] | None = None,
    reuse_index: bool = False,
    selector_mode: str = "default",
    selector_policy: str = DEFAULT_SELECTOR_POLICY,
    pack_policy: str = "v1",
    query_strategy: str = "head",
    normalize_issue: bool = False,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
    post_selector_policy: str = "off",
) -> tuple[dict[str, Any], dict[str, Any]]:
    client = McpClient(repo_root, env)
    try:
        repos = client.call_tool("list_indexed_repositories", {})
        cache_hit = reuse_index and isinstance(repos, list) and len(repos) == 1
        if cache_hit:
            index_result = {"cache_hit": True, "embeddings_reused": True}
            index_seconds = 0.0
        else:
            index_started = time.monotonic()
            index_result = client.call_tool(
                "index_directory",
                {
                    "path": str(repo_root),
                    "clear_existing": True,
                    "defer_replay": True,
                    "skip_embed": False,
                },
            )
            index_seconds = time.monotonic() - index_started
            if not isinstance(index_result, dict):
                raise RuntimeError(f"index completed without embeddings: {index_result}")
            embed_stats = index_result.get("embedding")
            if isinstance(embed_stats, dict):
                # Newer binaries report truthful embed-stage stats; require real writes.
                if embed_stats.get("written", 0) <= 0:
                    raise RuntimeError(
                        f"index completed without embeddings: {index_result}"
                    )
            elif index_result.get("embeddings_created", 0) <= 0:
                # Older binaries only expose embeddings_created.
                raise RuntimeError(f"index completed without embeddings: {index_result}")
            repos = client.call_tool("list_indexed_repositories", {})
        if not isinstance(repos, list) or len(repos) != 1:
            raise RuntimeError(f"expected one isolated indexed repo, got: {repos}")
        repo_id = repos[0]["repo_id"]
        issue = str(row["problem_statement"])
        advanced_query_strategy = query_strategy in ("v3", "v4", "v5")
        if normalize_issue or advanced_query_strategy:
            issue = normalize_issue_text(issue)
        if advanced_query_strategy:
            issue = strip_issue_boilerplate(issue)
        if planned_search_queries:
            planned_queries = planned_search_queries
            search_planner_audit = {"source": "locked_query_plan"}
            exact_queries = identifier_queries("\n".join(planned_queries))
        elif selector_model:
            planned_queries, search_planner_audit = plan_search_queries_with_gpt(
                issue, selector_model
            )
            exact_queries = identifier_queries("\n".join(planned_queries))
        elif query_strategy in ("v2", "v3", "v4", "v5"):
            planned_queries, exact_queries, _ = synthesize_queries_v2(issue)
            search_planner_audit = {"source": "static_query_plan_v2"}
        else:
            planned_queries = [issue[:700]]
            search_planner_audit = None
            exact_queries = identifier_queries("\n".join(planned_queries))
        if advanced_query_strategy:
            _, issue_exact_queries, _ = synthesize_queries_v2(issue)
            exact_queries = bounded_exact_queries(
                [
                    *explicit_issue_queries(issue),
                    *exact_queries,
                    *issue_exact_queries,
                ]
            )
        root_retrieval_lanes = ROOT_RETRIEVAL_LANES
        if query_strategy == "v4":
            head_query = legacy_head_query(issue)
            planned_queries = list(
                dict.fromkeys([head_query, *planned_queries])
            )
            root_retrieval_lanes = V4_ROOT_RETRIEVAL_LANES
        ranking_query = (
            legacy_head_query(issue)
            if query_strategy == "v4"
            else compact_ranking_query(issue, planned_queries, exact_queries)
            if query_strategy in ("v3", "v5")
            else issue
        )
        search_queries = list(dict.fromkeys([*planned_queries, *exact_queries]))
        if search_planner_audit is not None:
            search_planner_audit["identifier_queries"] = exact_queries
        search_view = "live" if advanced_query_strategy else "committed"
        search_line_budget = min(40, line_budget) if advanced_query_strategy else None
        searches = []
        for query in search_queries:
            arguments: dict[str, Any] = {
                    "repo_id": repo_id,
                    "query": query,
                    "limit": search_limit,
                    "include_diagnostics": True,
                    "view": search_view,
                }
            if search_line_budget is not None:
                arguments["line_budget"] = search_line_budget
            searches.append(client.call_tool("find_code", arguments))
        refinement_files = rank_refinement_files(
            searches[: len(planned_queries)],
            repo_root,
            extended_test_filter=(
                pack_policy in WINDOW_AWARE_PACK_POLICIES
                or query_strategy in ("v2", "v3", "v4", "v5")
            ),
        )
        refinement_searches = refine_within_files(
            client,
            repo_id,
            planned_queries[: len(root_retrieval_lanes)],
            refinement_files,
            view=search_view,
            line_budget=search_line_budget,
        )
        observation_groups = [
            candidates_from_rows(search.get("results", []), repo_root) for search in searches
        ]
        refined_rows = dedupe_search_rows(
            row
            for search in refinement_searches
            for row in search.get("results", [])
        )
        refinement_candidates = candidates_from_rows(refined_rows, repo_root)
        if refined_rows:
            observation_groups.append(refinement_candidates)
        for search in searches:
            scores = [row.get("score") for row in search.get("results", [])]
            numeric = [score for score in scores if isinstance(score, (int, float))]
            if len(numeric) > 1 and max(numeric) - min(numeric) < 1e-7:
                raise RuntimeError(
                    "cross-encoder produced identical scores; query was likely truncated "
                    "or the real reranker did not execute"
                )
        search_rows = dedupe_search_rows(
            row
            for search in [*searches, *refinement_searches]
            for row in search.get("results", [])
        )
        initial = candidates_from_rows(search_rows, repo_root)

        expanded: list[Candidate] = []
        graph_contexts: list[dict[str, Any]] = []
        graph_queue = select_expansion_rows([*searches, *refinement_searches])
        expanded_anchors: set[tuple[str, str]] = set()
        while graph_queue and len(graph_contexts) < DEFAULT_MAX_GRAPH_CONTEXTS:
            anchor = graph_queue.pop(0)
            anchor_key = (
                str(anchor.get("file_path") or ""),
                str(anchor.get("scope_path") or anchor.get("name") or ""),
            )
            if anchor_key in expanded_anchors:
                continue
            expanded_anchors.add(anchor_key)
            graph_context = client.call_tool(
                "get_symbol_context",
                {
                    "repo_id": repo_id,
                    "symbol": anchor["name"],
                    "file_path": anchor.get("file_path"),
                },
            )
            if isinstance(graph_context, dict):
                graph_contexts.append({"anchor": anchor, "context": graph_context})
                graph_observation = graph_candidates(graph_context, repo_root)
                expanded.extend(graph_observation)
                observation_groups.append(graph_observation)
                parent_anchor = parent_class_anchor(graph_context)
                if parent_anchor:
                    graph_queue.append(parent_anchor)

        hydrated = hydrate_candidate_content(
            dedupe_candidates([*initial, *expanded]), repo_root
        )
        if pack_policy in WINDOW_AWARE_PACK_POLICIES:
            all_candidates = compact_oversized_candidates(
                hydrated,
                ranking_query,
                line_budget,
                oversize_threshold=V2_OVERSIZE_THRESHOLD,
                max_windows=None,
                scorer=candidate_score_v2,
                window_ranker=rank_windows_bm25 if pack_policy == "v5" else None,
            )
        else:
            all_candidates = compact_oversized_candidates(hydrated, issue, line_budget)
        exact_anchors = exact_identifier_candidates(
            exact_queries, search_queries, searches, repo_root
        )
        selector_audit = None
        if selector_model:
            candidate_lane_sets: dict[tuple[str, int, int], set[str]] = {}
            file_query_family_sets: dict[str, set[str]] = {}
            for lane, group in zip(
                root_retrieval_lanes, observation_groups
            ):
                for candidate in group:
                    key = (candidate.file, candidate.start, candidate.end)
                    candidate_lane_sets.setdefault(key, set()).add(lane)
                    file_query_family_sets.setdefault(candidate.file, set()).add(lane)
            for candidate in refinement_candidates:
                key = (candidate.file, candidate.start, candidate.end)
                candidate_lane_sets.setdefault(key, set()).add("file_refinement")
            planned_lane_by_query = {
                query: lane
                for query, lane in zip(planned_queries, root_retrieval_lanes)
            }
            for refinement_search in refinement_searches:
                lane = planned_lane_by_query.get(str(refinement_search.get("query") or ""))
                refinement_file = str(
                    refinement_search.get("refinement_file") or ""
                )
                if lane and refinement_file and refinement_search.get("results"):
                    file_query_family_sets.setdefault(refinement_file, set()).add(lane)
            lane_order = {
                lane: index
                for index, lane in enumerate((*root_retrieval_lanes, "file_refinement"))
            }
            candidate_lanes = {
                key: sorted(lanes, key=lambda lane: lane_order.get(lane, len(lane_order)))
                for key, lanes in candidate_lane_sets.items()
            }
            file_query_families = {
                file: sorted(
                    families,
                    key=lambda lane: lane_order.get(lane, len(lane_order)),
                )
                for file, families in file_query_family_sets.items()
            }
            consensus_priority: list[Candidate] = []
            if query_strategy == "v5":
                consensus_priority = cross_lane_consensus_priority(
                    all_candidates,
                    [issue, *planned_queries],
                    file_query_families,
                    root_retrieval_lanes,
                    line_budget=line_budget,
                )
            initial_priority = [
                candidate
                for group in observation_groups[: len(root_retrieval_lanes)]
                for anchor in group[:10]
                for candidate in all_candidates
                if candidate.file == anchor.file
                and candidate.start >= anchor.start
                and candidate.end <= anchor.end
            ]
            selector_pool_size = (
                GUARDED_SELECTOR_CANDIDATES
                if selector_mode == "guarded"
                else DEFAULT_SELECTOR_CANDIDATES
            )
            refinement_priority = [
                candidate
                for file_path in refinement_files
                for candidate in sorted(
                    (item for item in all_candidates if item.file == file_path),
                    key=lambda item: -candidate_score(item, issue),
                )[:REFINEMENT_FILE_PRIORITY_CAP]
            ][: selector_pool_size - 30]
            final, selector_audit = select_context_with_gpt(
                all_candidates,
                issue,
                line_budget,
                selector_model,
                priority_candidates=[
                    *initial_priority,
                    *refinement_priority,
                    *consensus_priority,
                ],
                candidate_lanes=candidate_lanes,
                selector_mode=selector_mode,
                exact_match_files={anchor.file for anchor in exact_anchors},
                selector_policy=selector_policy,
                concept_queries=planned_queries,
                file_query_families=file_query_families,
                ranking_query=ranking_query,
            )
            before_boundary = set(final)
            final, boundary_graph_contexts = expand_concept_boundaries(
                client,
                repo_id,
                repo_root,
                final,
                selector_audit,
                [issue, *planned_queries],
                line_budget,
            )
            boundary_candidates = [
                candidate for candidate in final if candidate not in before_boundary
            ]
            if boundary_candidates:
                all_candidates = dedupe_candidates(
                    [*all_candidates, *boundary_candidates]
                )
                for candidate in boundary_candidates:
                    candidate_lanes[(
                        candidate.file,
                        candidate.start,
                        candidate.end,
                    )] = ["concept_boundary"]
            graph_contexts.extend(boundary_graph_contexts)
            causal_closure_events: list[dict[str, Any]] = []
            if query_strategy == "v5":
                final, causal_closure_events = expand_causal_same_file_producers(
                    final,
                    all_candidates,
                    line_budget,
                )
        elif pack_policy in WINDOW_AWARE_PACK_POLICIES:
            consensus_priority = []
            causal_closure_events = []
            final = pack_final_context_v2(
                all_candidates,
                ranking_query,
                line_budget,
                lane_searches=searches,
                repo_root=repo_root,
                pack_policy=pack_policy,
            )
        else:
            consensus_priority = []
            causal_closure_events = []
            final = pack_final_context(all_candidates, issue, line_budget)
        final, exact_floor_applied = apply_exact_identifier_floor(
            final, exact_anchors, line_budget
        )
        lane_floor_events: list[dict[str, Any]] = []
        if selector_mode == "guarded" and selector_model:
            graph_lane_searches = graph_lane_searches_for_floor(graph_contexts)
            lane_tops = lane_top_candidates(
                [
                    *searches[: len(planned_queries)],
                    *refinement_searches,
                    *graph_lane_searches,
                ],
                all_candidates,
                repo_root,
                line_budget,
            )
            final, lane_floor_events = apply_lane_file_floor(
                final,
                lane_tops,
                # `final` itself (the selector's own deliberate picks) MUST be
                # protected here, not just exact_anchors/lane_tops. Forensics
                # on real ContextBench regressions (cli/cli, clap-rs, both
                # guarded mode) found gold files with in_selected=true but
                # in_final_prediction=false -- apply_lane_file_floor was
                # silently evicting a candidate GPT-5 explicitly selected to
                # make room for some OTHER lane's forced top candidate, since
                # only exact_anchors/lane_tops were ever protected from
                # eviction. The selector's own output is the highest-trust
                # signal in this pipeline and must never be sacrificed to
                # force in a lower-confidence lane-floor candidate.
                dedupe_candidates([*exact_anchors, *lane_tops, *final]),
                line_budget,
                ranking_query,
            )
        validate_concept_boundary_cap(final, selector_audit, line_budget)
        steps = [step for group in observation_groups if (step := observation_step(group))]

        prediction = {
            "instance_id": row["instance_id"],
            "traj_data": {
                "pred_steps": steps,
                "pred_files": sorted({item.file for item in final}),
                "pred_spans": span_map(final),
                "pred_symbols": {},
            },
            "model_patch": "",
        }
        audit = {
            "repo_id": repo_id,
            "index_result": index_result,
            "index_seconds": index_seconds,
            "search_queries": search_queries,
            "search_planner": search_planner_audit,
            "searches": searches,
            "refinement_files": refinement_files,
            "refinement_searches": refinement_searches,
            "graph_contexts": graph_contexts,
            "final_candidates": [candidate.__dict__ for candidate in final],
            "exact_identifier_candidates": [candidate.__dict__ for candidate in exact_anchors],
            "exact_identifier_floor_applied": exact_floor_applied,
            "selector_mode": selector_mode,
            "selector_policy": selector_policy,
            "pack_policy": pack_policy,
            "query_strategy": query_strategy,
            "normalize_issue": normalize_issue,
            "ranking_query": ranking_query,
            "root_retrieval_lanes": list(root_retrieval_lanes),
            "consensus_priority": [
                candidate.__dict__ for candidate in consensus_priority
            ],
            "causal_closure": causal_closure_events,
            "search_view": search_view,
            "search_line_budget": search_line_budget,
            "search_limit": search_limit,
            "lane_floor": lane_floor_events,
            "selector": selector_audit,
        }
        return apply_post_selector_policy(
            prediction,
            audit,
            policy_name=post_selector_policy,
            line_budget=line_budget,
        )
    finally:
        client.close()


def dedupe_search_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("file_path") or ""),
            str(row.get("name") or ""),
            int(row.get("start_line") or 0),
            int(row.get("end_line") or 0),
        )
        existing = best.get(key)
        if existing is None or float(row.get("score") or 0) > float(existing.get("score") or 0):
            best[key] = row
    return sorted(best.values(), key=lambda row: float(row.get("score") or 0), reverse=True)


def load_rows(dataset: Path, instance_id: str | None, limit: int | None) -> list[dict[str, Any]]:
    import pandas as pd

    frame = pd.read_parquet(dataset)
    if "repo_url" not in frame.columns:
        full_dataset = dataset.with_name("full.parquet")
        if not full_dataset.is_file():
            raise RuntimeError(
                f"{dataset} has no repo_url column and sibling full.parquet was not found"
            )
        metadata = pd.read_parquet(full_dataset, columns=["instance_id", "repo", "repo_url"])
        frame = frame.drop(columns=["repo"], errors="ignore").merge(
            metadata, on="instance_id", how="left", validate="one_to_one"
        )
        if frame["repo_url"].isna().any():
            missing = frame.loc[frame["repo_url"].isna(), "instance_id"].head(5).tolist()
            raise RuntimeError(f"repo metadata missing from full.parquet for: {missing}")
    if instance_id:
        frame = frame[frame["instance_id"] == instance_id]
    if limit is not None:
        frame = frame.head(limit)
    return frame.to_dict(orient="records")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, default=Path("work/contextbench-memtrace"))
    parser.add_argument("--instance-id")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--line-budget", type=int, default=DEFAULT_LINE_BUDGET)
    parser.add_argument("--selector-model")
    parser.add_argument(
        "--selector-mode",
        choices=("default", "guarded"),
        default="default",
        help="guarded: 120-candidate pool, exact-identifier omission "
             "justification, multi-file selection, per-lane file floors",
    )
    parser.add_argument(
        "--selector-policy",
        choices=SELECTOR_POLICY_CHOICES,
        default=os.environ.get("CB_SELECTOR_POLICY", DEFAULT_SELECTOR_POLICY),
        help="continuation-v2: start with doubled output headroom and use at "
             "most one compact previous-response continuation after token "
             "exhaustion (default replay-v1 reproduces prior runs)",
    )
    parser.add_argument("--rerank-model-dir", type=Path)
    parser.add_argument(
        "--pack-policy",
        choices=PACK_POLICY_CHOICES,
        default=os.environ.get("CB_PACK_POLICY", "v1"),
        help="v2: S1-gated scoring, pack-time junk filter, novel-line "
             "accounting, adaptive tight/consolidation regimes, budget-aware "
             "windowing; v3: v2 plus a chain relative-score floor on the "
             "consolidated regime's same-file pass, fixing the django/gson "
             "same-file-filler precision regression seen in v2 "
             "v5: v4 plus BM25 ranking inside oversized symbols "
             "(default v1 reproduces prior runs)",
    )
    parser.add_argument(
        "--query-strategy",
        choices=QUERY_STRATEGY_CHOICES,
        default=os.environ.get("CB_QUERY_STRATEGY", "head"),
        help="v2: boilerplate-stripped section lanes over the whole issue, "
             "stack-trace frame queries, tail lane, full-text identifiers "
             "v3: v2 plus cleaned GPT/locked plans, inline-code exact lanes, "
             "and a compact ranking query; v4: rejected legacy-head dual "
             "lane retained for sealed reproduction; v5: v3 plus cross-lane "
             "selector consensus and bounded causal producer closure "
             "(default 'head' reproduces prior runs)",
    )
    parser.add_argument(
        "--normalize-issue",
        action="store_true",
        default=os.environ.get("CB_NORMALIZE_ISSUE", "") == "1",
        help="decode JSON-string-encoded problem statements before querying",
    )
    parser.add_argument(
        "--search-limit",
        type=int,
        default=int(os.environ.get("CB_SEARCH_LIMIT", DEFAULT_SEARCH_LIMIT)),
        help="find_code result depth per lane (default 20; 100 recommended "
             "with --pack-policy v2)",
    )
    parser.add_argument(
        "--post-selector-policy",
        choices=POST_SELECTOR_POLICY_CHOICES,
        default=os.environ.get("CB_POST_SELECTOR_POLICY", "off"),
        help="offline-packing-v2: replay the fixed sealed packing policy "
             "after raw selection and preserve its evidence in the audit "
             "(default off reproduces prior output)",
    )
    parser.add_argument(
        "--reinclude-tracked-dirs",
        action="store_true",
        help="overlay .memtraceignore re-includes for default-excluded dirs "
             "that contain git-tracked source files (e.g. Go pkg/)",
    )
    parser.add_argument("--query-plan-file", type=Path)
    parser.add_argument("--keep-index", action="store_true")
    parser.add_argument("--graph-cache-dir", type=Path)
    parser.add_argument("--cache-namespace", default="contextbench-v1")
    args = parser.parse_args()
    load_env_file(Path(".env"))
    query_plans: dict[str, list[str]] = {}
    if args.query_plan_file and args.query_plan_file.is_file():
        query_plans = json.loads(args.query_plan_file.read_text(encoding="utf-8"))

    rows = load_rows(args.dataset, args.instance_id, args.limit)
    if not rows:
        raise SystemExit("no matching ContextBench rows")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    audit_dir = args.output.parent / f"{args.output.stem}-audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    with args.output.open("w", encoding="utf-8") as output:
        for index, row in enumerate(rows, 1):
            instance_id = str(row["instance_id"])
            slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", instance_id)
            instance_work = args.work_dir / slug
            repo_root = instance_work / "repo"
            print(f"[{index}/{len(rows)}] {instance_id}", file=sys.stderr)
            ensure_checkout(str(row["repo_url"]), str(row["base_commit"]), repo_root)
            reincluded_dirs: list[str] = []
            if args.reinclude_tracked_dirs:
                reincluded_dirs = write_tracked_dir_reincludes(repo_root)
                if reincluded_dirs:
                    print(f"  re-included tracked dirs: {reincluded_dirs}", file=sys.stderr)
            model_dir = args.rerank_model_dir or Path(
                os.environ.get(
                    "MEMTRACE_RERANK_MODEL_DIR",
                    Path.home() / ".memtrace/rerank-models/ms-marco-MiniLM-L-12-v2",
                )
            )
            validate_rerank_model(model_dir)
            cache_entry = None
            cache_complete = False
            if args.graph_cache_dir:
                cache_manifest = graph_cache_manifest(
                    instance_id,
                    str(row["repo_url"]),
                    str(row["base_commit"]),
                    args.cache_namespace,
                    repo_root,
                )
                cache_entry = args.graph_cache_dir / graph_cache_key(
                    str(row["repo_url"]),
                    str(row["base_commit"]),
                    args.cache_namespace,
                )
                cache_complete = graph_cache_is_reusable(cache_entry, cache_manifest)
                if not cache_complete:
                    shutil.rmtree(cache_entry, ignore_errors=True)
                    cache_entry.mkdir(parents=True, exist_ok=True)
            else:
                shutil.rmtree(instance_work / "memdb", ignore_errors=True)
                shutil.rmtree(instance_work / "state", ignore_errors=True)
            env = memtrace_env(
                repo_root,
                instance_work,
                model_dir,
                cache_entry,
                walker_include_dirs=reincluded_dirs,
            )
            started = time.monotonic()
            prediction, audit = retrieve_instance(
                row,
                repo_root,
                env,
                args.line_budget,
                args.selector_model,
                query_plans.get(instance_id),
                reuse_index=cache_complete,
                selector_mode=args.selector_mode,
                selector_policy=args.selector_policy,
                pack_policy=args.pack_policy,
                query_strategy=args.query_strategy,
                normalize_issue=args.normalize_issue,
                search_limit=args.search_limit,
                post_selector_policy=args.post_selector_policy,
            )
            if cache_entry and not cache_complete:
                write_graph_cache_manifest(cache_entry, cache_manifest)
            if args.query_plan_file and instance_id not in query_plans:
                query_plans[instance_id] = audit["search_queries"]
                args.query_plan_file.parent.mkdir(parents=True, exist_ok=True)
                args.query_plan_file.write_text(
                    json.dumps(query_plans, indent=2) + "\n", encoding="utf-8"
                )
            audit["retrieval_seconds"] = time.monotonic() - started
            if args.reinclude_tracked_dirs:
                audit["reincluded_dirs"] = reincluded_dirs
            output.write(json.dumps(prediction, separators=(",", ":")) + "\n")
            output.flush()
            (audit_dir / f"{slug}.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
            if not args.keep_index and not args.graph_cache_dir:
                shutil.rmtree(instance_work / "memdb", ignore_errors=True)
                shutil.rmtree(instance_work / "state", ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
