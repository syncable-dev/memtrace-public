---
name: memtrace-style-fingerprint
description: "Pull the codebase's empirical style norm from Memtrace and match it when writing or editing source code in an indexed repo. Use when choosing between competing idioms (ternary vs if-else, arrow vs function declaration, const vs let, await vs .then, early-return vs nested-return), matching naming case, or when the user asks what the convention here is. Do not re-derive style from training priors or maintain a markdown style guide for the project; the fingerprint is sampled live from the actual code."
allowed-tools:
  - mcp__memtrace__get_style_fingerprint
  - mcp__memtrace__get_codebase_briefing
  - mcp__memtrace__list_indexed_repositories
metadata:
  author: "Syncable <support@syncable.dev>"
  version: "1.0.0"
  category: development
---

## Overview

Every indexed Memtrace repository carries an empirical **style fingerprint** — descriptive histograms of competing AST idioms (ternary vs if-else, arrow vs function declaration, const vs let, await vs `.then`, early-return vs nested-return depth) computed at parse time and rolled up to Repository + Community level. This workflow tells you when and how to consult it so your edits match the codebase's existing idioms instead of drifting on stylistic choices the linter doesn't catch.

The fingerprint is **descriptive, not prescriptive**. It reports what the codebase actually does, not what a style guide says it should do. If the codebase deviates from a popular convention for some reason, the fingerprint captures the deviation and you should match the deviation — that's the whole point. For prescriptive bug/security/perf rules, use `find_code_review_issues` instead.

## Best Fit

| Situation | Action |
|---|---|
| About to write new code (function, component, module) in an indexed repo | Step 1 + Step 2 with `file_path=<the file you'll create>` |
| About to edit an existing file | Step 1 + Step 2 with `file_path=<that file>` |
| Asked "what's the convention here for X?" | Step 1 only — repo-mode fingerprint answers it |
| Deciding between two equivalent idioms (ternary vs if, arrow vs fn-decl, await vs then) | Step 2 with `file_path` — `delta_from_codebase_norm` tells you which idiom matches the norm |
| Reviewing a diff | Step 2 on each modified file — flag any new idioms that diverge from the file's language norm |
| Session start in a multi-day project | Read the `style:` line in `get_codebase_briefing` (auto-included) |

If the repo is not indexed in Memtrace, this workflow does not apply — fall back to your default behavior.

Full parameter spec for every Memtrace tool: [references/mcp-parameters.md](../../references/mcp-parameters.md).

## Steps

### 1. Get the codebase's overall norm

Call `get_style_fingerprint(repo_id)` with no `file_path` (`repo_id` comes from `list_indexed_repositories` if not already known). The response includes:

- `histogram` — raw counts (e.g. `ternary_count: 1005, if_stmt_count: 8087`)
- `ratios` — computed shares for each competing pair (e.g. `ternary_share: 0.11`)
- `dominant_idioms` — top-3 dimensions sorted by `|ratio - 0.5|` (strongest preferences first), each with a `dimension`, `ratio`, and human-readable `interpretation`
- `function_count` — sample size at the repo level
- `sample_threshold` — minimum observations before a ratio is committed (currently 20)

A `ratio` of `null` means the codebase doesn't have enough observations for that dimension to commit a norm — treat it as "no signal", do not assume one.

### 2. Get the file's deviation from the norm (when about to edit a specific file)

Call `get_style_fingerprint(repo_id, file_path=<file>)`. The response adds:

- `file_fingerprint` — the same shape as `histogram`/`ratios`/`function_count` but computed over just the functions in that file
- `codebase_fingerprint` — repo aggregate for comparison
- `delta_from_codebase_norm` — array of dimensions where the file diverges ≥0.15 from the codebase, sorted by absolute delta, capped at top 5. Each entry has `dimension`, `file_ratio`, `codebase_ratio`, `abs_delta`, and a `note` describing the direction

**Match the file's `language_fingerprint`, not the repo aggregate.** In file mode the response carries `language` (the file's language), `language_fingerprint` (that language's slice — the primary comparator), and `delta_from_language_norm` (divergence vs the language, not the repo). Per-language slicing prevents cross-applying Python norms to JS code or vice versa — read the `language_fingerprint`. (`delta_from_codebase_norm` is retained as a deprecated alias and is removed in 0.5.14.)

If `delta_from_language_norm` is empty, the file is already aligned — proceed without style adjustments. If it has entries, your edits should not amplify the divergence (e.g. don't add more ternaries to a file that's already above the language's ternary norm).

### 3. Read the briefing line at session start (passive)

`get_codebase_briefing(repo_id)` auto-includes a `style:` line in its summary when the sample threshold is met and at least one ratio is outside the 0.4..0.6 no-preference band. Format:

```
style: <interpretation 1> (<%>); <interpretation 2> (<%>); <interpretation 3> (<%>)
```

Example on a TS/JS-heavy codebase:

```
style: strongly prefers arrow functions (98%); strongly prefers async/await over .then chains (88%); strongly prefers const over let (94%)
```

You should already be reading the briefing at session start (per `memtrace-codebase-exploration`). The `style:` line lands in your context for free — no extra call needed.

## Decision points

| Condition | Action |
|---|---|
| `dominant_idioms[0].ratio >= 0.85` or `<= 0.15` | Treat as a hard preference — match it unless there's a specific structural reason not to |
| `dominant_idioms[0].ratio` in `0.65..0.85` or `0.15..0.35` | Treat as a soft preference — match it for new code, leave existing patterns alone |
| `ratio` in `0.4..0.6` | No clear preference — use your own judgment, match local file context |
| `ratio` is `null` (below sample threshold) | No signal — don't assume a norm; pick the idiom that fits the immediate context |
| `delta_from_codebase_norm` shows your target file already diverges from the norm | Don't amplify the divergence with your edits — match the codebase, not the outlier file |
| `file_fingerprint` is empty (file has no functions yet, or is a config file) | Use the language slice or repo aggregate; this is creation territory |

## Output

Repo-mode `get_style_fingerprint` (field meanings in Steps 1–2):

```json
{
  "histogram": { "ternary_count": 1005, "if_stmt_count": 8087 },
  "ratios": { "ternary_share": 0.11, "naming_variables_snake_share": 0.89 },
  "dominant_idioms": [
    { "dimension": "ternary_share", "ratio": 0.11, "interpretation": "strongly prefers if-else over ternaries" }
  ],
  "function_count": 4211,
  "sample_threshold": 20
}
```

File mode adds `file_fingerprint`, `codebase_fingerprint`, `language`, `language_fingerprint`, and `delta_from_language_norm` (entries: `dimension`, `file_ratio`, `codebase_ratio`, `abs_delta`, `note`).

## Anti-patterns — do not do these

- **Reading the repo aggregate when editing a single-language file.** If the codebase is 70% TS / 30% Python, the repo aggregate mixes both. The `language_fingerprint` (when available) or `codebase_fingerprint.ratios` filtered by the file's language is what you should match. Cross-applying TS norms to a Python file is the failure mode this whole tool exists to prevent.
- **Treating the fingerprint as prescriptive.** It tells you what the codebase does. If the codebase consistently does something unusual, match that — don't override with "this isn't best practice" arguments. The whole point is to stop drifting from the codebase's own choices.
- **Calling `get_style_fingerprint` for every line of code.** Once per file at the start of an edit session is enough. The norm doesn't change between your edits within the same session.
- **Ignoring the briefing line.** It auto-loads into your context. If you act on style choices in an edit session without reading it first, you're spending tool calls to re-derive something you already had.
- **Adding a markdown style guide to the project.** The fingerprint is the style guide. It refreshes on every reindex. A markdown file would either duplicate it or contradict it.

## Naming-case dimensions (shipped in 0.5.13)

Beyond the AST idioms, the fingerprint also reports identifier **naming-case** per scope, across all 13 source languages:

- **Variables / functions / types / constants / files** — the share of camelCase / snake_case / PascalCase / SCREAMING_SNAKE / kebab. Read e.g. `ratios.naming_variables_snake_share` or the `dominant_idioms` entry that names the scope's winning case ("vars are snake_case (89%)").
- **Config keys** for YAML / JSON / TOML / HCL / SQL — `ratios.config_keys_*_share`.
- **Enforcement-aware:** languages that compiler-enforce a scope return `null` for it (Rust vars/fns/types/consts, Ruby constants). A `null` there means "the compiler decides, not the codebase" — don't try to match a non-signal.
- **Go** reports `go_exported_share` (PascalCase = exported) instead of per-case naming, because case encodes visibility in Go.

When editing, match the naming case the file's `language_fingerprint` reports for the scope you're touching — same per-language rule as the idiom dimensions.

## What this workflow does NOT cover

- **Security / bug / performance rules** — use `find_code_review_issues` (prescriptive deterministic review).
- **Comment / docstring style** — not in scope; use file-local examples.
- **Architecture / module-organization decisions** — see `memtrace-codebase-exploration` and `memtrace-refactoring-guide`.

## Why this exists

Without this workflow, LLM editors drift session-to-session on style choices that aren't enforced by linters — one session uses ternaries, the next doesn't; one uses arrow functions, the next uses `function`. The fix isn't a manually-maintained style guide (stale by week 2). The fix is sampling the codebase's actual idioms at parse time and reading them before each edit. The fingerprint is computed in the same parse pass as cyclomatic + cognitive complexity at sub-1% overhead, so the cost of providing the answer is near zero.
