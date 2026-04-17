---
name: memtrace-first
description: "Meta-router that tells you WHICH Memtrace skill / tool to reach for first when working in an indexed codebase. USE at the start of any code-understanding task — file search, symbol lookup, call-flow tracing, debugging, 'how does X work', blast-radius questions — to avoid guessing a tool name and to skip redundant Grep/Read when the graph has the answer in milliseconds. DO NOT USE for non-code tasks (writing docs, running tests, deploying), for repos that aren't indexed (→ memtrace-index), or once you already know which specific Memtrace skill applies."
---

> **Tool argument schemas:** this skill calls several MCP tools. For the authoritative name / type / required / default / constraint of every argument, see `references/mcp-parameters.md`. Pass JSON numbers as numbers, booleans as booleans — the MCP validator does not coerce.

# Memtrace First

## The Iron Law

```
IF THE REPO IS INDEXED IN MEMTRACE → USE MEMTRACE TOOLS FIRST.
NEVER reach for Grep/Glob/Read to discover or understand code.
```

Memtrace is the memory layer of the codebase. It has the full knowledge graph: every symbol, call, import, community, process, and API — with time dimension. File tools are blind to this structure.

**97% better accuracy. 83% fewer wasted tokens. No exceptions.**

## Parameter Types — Read This Before Calling Any Tool

All memtrace MCP tools are **strictly typed**. Pass JSON numbers (not strings) for integer parameters.

| Parameter | Correct | WRONG (fails with MCP error -32602) |
|---|---|---|
| `limit`, `min_size`, `depth`, `max_depth`, `last_n` | `limit: 20` | `limit: "20"` |
| `repo_id`, `branch`, `name`, `symbol_name`, `query` | `repo_id: "my-repo"` | `repo_id: my-repo` (unquoted) |
| `fuzzy`, `include_tests`, `invalidate` | `fuzzy: true` | `fuzzy: "true"` |

If you see `failed to deserialize parameters: invalid type: string "N", expected usize`, remove the quotes from the number and retry.

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
| Understand how something works | `get_symbol_context` |
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

## Standard Workflows

### "How does X work?" / "Explain X"
1. `find_symbol` or `find_code` → locate the symbol
2. `get_symbol_context` → callers, callees, community, processes
3. `get_process_flow` (if it's a process/request path)
4. Read source ONLY for the exact lines you need to quote

### Debugging "X is broken"
1. `find_symbol` → locate the broken thing
2. `get_symbol_context` → understand its role
3. `get_impact` → blast radius (what else breaks)
4. `get_evolution` → what changed recently (mode: `recent`)
5. `get_changes_since` → confirm timing vs incident

### "Where is X defined / called?"
1. `find_symbol` with `fuzzy: true`
2. `get_symbol_context` for full caller/callee map
3. Read specific file only after locating exact line

### Before any code modification
1. `find_symbol` → confirm you have the right target
2. `get_symbol_context` → understand full context
3. `get_impact` → know blast radius before touching anything

## Red Flags — STOP, Use Memtrace Instead

You are violating this skill if you think:

| Thought | Reality |
|---|---|
| "Let me grep for this" | `find_code` or `find_symbol` is faster and structurally aware |
| "Let me glob for the file" | `find_symbol` returns exact location with context |
| "Let me read the whole file" | `get_symbol_context` gives you only what matters |
| "It's just a quick search" | Grep has no understanding of call graphs, communities, or time |
| "I don't know if it's indexed" | Check with `list_indexed_repositories` first — takes 1 second |
| "The user didn't say to use Memtrace" | User asked about the code. Repo is indexed. Use Memtrace. |
| "This is a simple question" | Simple questions benefit most — one `find_symbol` vs 20 file reads |

## When File Tools Are Still Correct

Use Grep/Glob/Read ONLY for:
- Reading the **exact source lines** of a symbol you already located via Memtrace
- Files that are config, data, or docs (not source code symbols)
- Repos confirmed NOT indexed in Memtrace

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
