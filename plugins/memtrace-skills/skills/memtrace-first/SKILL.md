---
name: memtrace-first
description: "Route code discovery, debugging, flow tracing, and how-code-works questions in indexed source-code repos to Memtrace graph tools. Use first — before searching files, reading code for discovery, debugging, tracing flows, finding implementations, understanding behavior, or answering how code works. Do not use Grep, Glob, rg, find, or manual file browsing for code discovery when Memtrace is indexed. Zero results, missing languages, or partial-looking stats are not permission to grep; diagnose/reindex with Memtrace."
---

# Memtrace First

## The Iron Law

```
IF THE REPO IS INDEXED IN MEMTRACE → USE MEMTRACE TOOLS FIRST.
After a search hit, route to GRAPH tools (get_symbol_context, get_impact,
analyze_relationships) — that's what Memtrace uniquely provides. Read source
ONLY when you're about to edit or quote, and read only the bounded span
returned by Memtrace (start_line .. end_line + small context). Do not
Grep/Glob/Find to "locate" anything already in the graph, and do not read
the whole file when Memtrace has given you exact lines.
```

Memtrace is the **memory layer** of the codebase, not a search engine that returns code. It has the full knowledge graph — every symbol, call, import, community, process, and API — with a time dimension. The point is to navigate that graph: who calls this, what's the blast radius, when did this change, what community is it part of. File tools are blind to all of that.

**No exceptions for what's in the graph.**

## Value Tracking

Do not print usage receipts in normal answers. Memtrace records tool usage, graph facts, file references, and estimated context avoided internally. Users can inspect that in the local UI's Value panel.

## What Memtrace actually indexes

Memtrace's hybrid search = **BM25 over symbol metadata** (name, signature, file_path, kind) **+ semantic vector search over embedded code bodies** (first ~1500 chars of every Function / Method / Class / Struct / Interface body), fused via Reciprocal Rank Fusion.

The semantic side means **string literals, error messages, magic constants, log strings, and any text inside an indexed symbol's body are findable through `find_code`**. The body got embedded; the embedding catches it. You do NOT need `Grep` to hunt for `STRIPE_KEY_FOO_BAR` if it lives inside a function in your indexed codebase.

## Zero results are not a grep license

If Memtrace returns 0 results, or repository stats look incomplete, do **not**
infer that a source subdirectory is outside the index. Diagnose through
Memtrace:

1. Call `list_indexed_repositories` and identify the repo root/repo_id.
2. If the path is under that indexed repo root, keep using Memtrace.
3. Retry with broader `find_code` terms and, when available, `file_path` filters
   such as `ui/`, `memtrace-ui/`, `src/`, or the framework directory.
4. If the language/path still appears missing, run `index_directory` on the repo
   root with `incremental: true` (or ask before `clear_existing: true`).
5. Report the indexing coverage problem instead of silently switching to grep.

**Never say "the index only covers X, so grep is right" when the target path is
inside the indexed repository.** That is an indexing freshness/coverage issue,
not permission to abandon Memtrace.

## The narrow exceptions where grep/glob are still right

These are the ONLY cases where file tools beat memtrace:

- **Files outside every indexed repo root.** Confirm this with
  `list_indexed_repositories`; 0 search results or missing language stats do not
  prove it. Vendored deps, system headers, and excluded dirs
  (`.git`, `node_modules`, `target`, `dist`) are examples Memtrace cannot see.
- **Non-source artifacts.** `.env`, `package.json`, build scripts, top-level `README.md`, raw config files. Memtrace indexes parseable code, not configuration text.
- **Pure file-inventory questions.** "How many `*.test.ts` files exist", "list every Markdown file in `docs/`". You're asking for a file count, not a symbol search.
- **Reading at a known path outside Memtrace.** For configs, docs, or non-source artifacts that Memtrace cannot index, file `Read` is fine. For source-code spans returned by Memtrace, read the precise line range (your harness's `Read` with offset/limit, or `get_source_window` if your harness lacks bounded reads). Do not whole-file Read when you have a span.

For everything else inside the indexed repo, memtrace is the right tool.

## The decision rule

| Question Claude is asking | Right tool |
|---|---|
| "Where is symbol `foo` defined?" | `find_symbol(name="foo")` → then `get_symbol_context` for callers/callees/community, NOT a source read unless you're editing. |
| "What calls `foo`?" | `get_symbol_context(repo_id, symbol="foo")` → callers with file:line. |
| "How does authentication work?" | `find_code(query="authentication")` → `get_symbol_context` on the top hit, NOT a source read. |
| "Find behavior X" with multi-word phrase (3+ words) | `find_code(verbatim)` first; if low confidence, fan out with identifier-shaped reshapes (camelCase / snake_case). |
| "Find the function that uses `STRIPE_KEY_FOO_BAR`" | `find_code(query="STRIPE_KEY_FOO_BAR")` → semantic finds it inside any embedded body. |
| "Where's that error message `'connection refused for tenant'`?" | `find_code(query="connection refused for tenant")` → semantic catches it. |
| "What breaks if I change `foo`?" | `get_impact(repo_id, target="foo")` → blast radius. |
| "What changed in `auth.ts` last week?" | `get_evolution(repo_id, from="7d ago", mode="recent", file_path="auth.ts")`. |
| "List all `*.test.ts` files." | `Glob` (file inventory, not symbol search). |
| "Find this string in my `.env`." | `Grep` (non-source artifact). |
| "I'm about to edit `foo` — show me its source." | Bounded `Read(file_path, offset=start_line, limit=end_line-start_line+8)`, or `get_source_window` if your harness lacks bounded reads. Never whole-file. |
| "Read config/doc file I already have the path of." | `Read` (non-source artifact, path is known). |

## Parameter Types — Read This Before Calling Any Tool

All memtrace MCP tools are **strictly typed**. Pass JSON numbers (not strings) for integer parameters.

| Parameter | Correct | WRONG (fails with MCP error -32602) |
|---|---|---|
| `limit`, `min_size`, `depth`, `max_depth`, `last_n` | `limit: 20` | `limit: "20"` |
| `repo_id`, `branch`, `name`, `symbol_name`, `query` | `repo_id: "my-repo"` | `repo_id: my-repo` (unquoted) |
| `fuzzy`, `include_tests`, `invalidate` | `fuzzy: true` | `fuzzy: "true"` |
| `get_evolution.from` | `from: "90d ago"` | `days: 90` (wrong param — use `from`, not `days`) |
| `get_changes_since.since` | `since: "2026-04-13T10:43:00Z"` | `last_episode_id: "..."` (wrong param) |
| `get_impact.target` / `get_symbol_context.symbol` | `target: "foo"` / `symbol: "foo"` | `symbol_id: "..."` (wrong — use name) |
| `find_most_complex_functions` | `top_n: 10` | `limit: 10` (wrong param name) |
| `get_cochange_context` | `target: "execute"` | `symbol: "execute"` (wrong param name) |

If you see `failed to deserialize parameters: invalid type: string "N", expected usize`, remove the quotes from the number and retry.

If you see `missing field 'from'`, you called `get_evolution` without `from` — pass e.g. `"90d ago"`, never `days`.

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

## Check Indexing First (Once Per Session)

```
mcp__memtrace__list_indexed_repositories
```

If the current repo appears → Memtrace is active. Follow this skill for ALL code tasks.
If not indexed → offer to index with `mcp__memtrace__index_directory`, then follow this skill.

## Task → Tool Map

| What you need | Use instead of Grep/Glob/Read |
|---|---|
| Find a function / class / symbol | `find_symbol` or `find_code` |
| Understand how something works | `get_symbol_context` (the default next step) |
| Find all callers of a function | `get_symbol_context` (callers field) |
| Find all callees / dependencies | `get_symbol_context` (callees field) |
| Trace a request / execution path | `get_process_flow` |
| Understand module structure | `list_communities` |
| Find the most important symbols | `find_central_symbols` |
| Find API endpoints | `find_api_endpoints` |
| Find where an API is called | `find_api_calls` |
| Debug a problem | `get_symbol_context` → `get_impact` → `get_evolution` |
| What changed recently? | `get_changes_since` or `get_evolution` |
| What breaks if I change X? | `get_impact` |
| Cross-service / cross-repo calls | `get_service_diagram` or `get_api_topology` |
| Dependency between two symbols | `find_dependency_path` |
| What files change together? | `get_cochange_context` |
| Architecture overview | `list_communities` + `find_central_symbols` |
| About to edit / quote — need exact lines | Bounded `Read(file, offset=start_line, limit=N)` (preferred), or `get_source_window` for path-resolution parity |
| About to choose between competing idioms (ternary vs if-else, arrow vs fn-decl, const vs let, await vs `.then`) | `get_style_fingerprint(repo_id, file_path)` — empirical codebase norm; see `memtrace-style-fingerprint` workflow |

## Standard Workflows

### "How does X work?" / "Explain X"
1. `find_symbol` or `find_code` → locate the symbol
2. `get_symbol_context` → callers, callees, community, processes (this usually answers "how it works")
3. `get_process_flow` (if it's a process/request path)
4. Only if you need to quote source: bounded `Read` at start_line..end_line, or `get_source_window`

### Debugging "X is broken"
1. `find_symbol` → locate the broken thing
2. `get_symbol_context` → understand its role
3. `get_impact` → blast radius (what else breaks)
4. `get_evolution(from=<lookback>, mode: recent)` → per-episode changelog near the incident
5. `get_changes_since(since=<anchor>)` → catch-up since last session (requires stored `since` timestamp)

### "Where is X defined / called?"
1. `find_symbol` with `fuzzy: true`
2. `get_symbol_context` for full caller/callee map
3. Only if you need source text: bounded `Read` at start_line..end_line, or `get_source_window`

### Before any code modification
1. `find_symbol` → confirm you have the right target
2. `get_symbol_context` → understand full context
3. `get_impact` → know blast radius before touching anything
4. `get_style_fingerprint(repo_id, file_path=<file>)` → match the codebase's empirical idiom (ternary vs if-else, arrow vs fn-decl, etc.) — see `memtrace-style-fingerprint` workflow for the full decision rule

## Red Flags — STOP, Use Memtrace Instead

You are violating this skill if you think:

| Thought | Reality |
|---|---|
| "Let me grep for this" | `find_code` or `find_symbol` is faster and structurally aware |
| "Let me glob for the file" | `find_symbol` returns exact location with context |
| "Let me read the whole file" | `get_symbol_context` for the WHY (callers/callees/community); a bounded source read at start_line..end_line for the WHAT |
| "It's just a quick search" | Grep has no understanding of call graphs, communities, or time |
| "I don't know if it's indexed" | Check with `list_indexed_repositories` first — takes 1 second |
| "Memtrace returned 0 results" | Broaden the Memtrace query, check repo_id/path coverage, then reindex if needed |
| "Stats only show Rust, but I need `ui/` or `memtrace-ui/`" | That is a coverage diagnostic. Reindex the repo root; do not grep source code. |
| "The user didn't say to use Memtrace" | User asked about the code. Repo is indexed. Use Memtrace. |
| "This is a simple question" | Simple questions benefit most — one `find_symbol` vs 20 file reads |

## When File Tools Are Still Correct

Use Grep/Glob/Read ONLY for:
- Non-source files or paths outside every indexed source repo
- Files that are config, data, or docs (not source code symbols)
- Repos or paths confirmed outside every Memtrace indexed root
- **Official Memtrace product documentation** — use `memtrace-docs` (`ask_docs` / `search_docs` / `read_doc`), not file tools or web search

For source-code spans already located by Memtrace, use a **bounded** read —
your harness's `Read(file, offset, limit)` with the returned `start_line` /
`end_line`, or `get_source_window` if your harness lacks bounded reads. Do
not read the whole file.

Never use file tools as a **discovery** mechanism when Memtrace is available.

## Skill Priority

This skill is a **process skill** — it runs BEFORE any implementation or search skill.

When this skill applies, it overrides default file-search behavior. Use the specific Memtrace sub-skills for deep detail on each tool:

- Discovery → `memtrace-search`
- Impact analysis → `memtrace-impact`
- Temporal / change analysis → `memtrace-evolution`
- Incident investigation → `memtrace-incident-investigation`
- Architecture overview → `memtrace-codebase-exploration`
- Refactoring → `memtrace-refactoring-guide`
- Memtrace **product docs** (install, CLI, MCP, enterprise) → `memtrace-docs`

## Output

`find_symbol` / `find_code` return ranked symbol entries (`score` only with `include_diagnostics: true`):

```json
{ "name": "handleAuth", "kind": "Function", "file_path": "src/auth.ts",
  "start_line": 42, "end_line": 87 }
```

`get_symbol_context` returns the graph neighborhood: `symbol`, `callers`, `callees`, `type_references`, `community`, `processes`, `api_callers_cross_repo`. Feed `start_line`/`end_line` into a bounded `Read` or `get_source_window` — never a whole-file read.

## Success criteria

- The answer is grounded in Memtrace graph results (search hit → `get_symbol_context` / `get_impact`), not file-tool discovery.
- Grep/Glob/Read appear only via the documented narrow exceptions (non-source artifacts, paths outside every indexed root, file inventory, bounded span reads).
- Zero-result queries were diagnosed (`list_indexed_repositories` → broaden → reindex), not bypassed to grep.
- Any source read was bounded to the span Memtrace returned.
