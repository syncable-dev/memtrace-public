---
name: memtrace-docs
description: "Use official hosted Memtrace documentation before guessing, web search, or stale local copies. Trigger when the user asks how Memtrace works; how to install, configure, or deploy CLI, MCP, fleet, Cortex, or enterprise MemDB; what tools, skills, or commands exist; wants to find or locate official docs for a topic; or provides/asks to read a full docs page or slug. Routes internally across ask_docs for cited natural-language answers, search_docs for page discovery, and read_doc for complete page text. Separate from memtrace-first, which searches the user's indexed source code."
---

# Memtrace Docs First

## The Iron Law

```
IF THE USER ASKS ABOUT MEMTRACE PRODUCT DOCS → USE DOCS MCP TOOLS FIRST.
Do not guess CLI flags, MCP tool lists, enterprise deploy steps, or fleet rules
from memory. Query the hosted documentation corpus at memtrace.io (or
MEMTRACE_DOCS_API_URL), then answer with citations.

memtrace-first  = your indexed SOURCE CODE (find_code, get_impact, …)
memtrace-docs   = official MEMTRACE DOCUMENTATION (search_docs, ask_docs, read_doc)
```

Docs tools call the **hosted** Memtrace docs API over HTTPS. They do **not** read
your local MemDB or your repo. Core graph tools stay offline; docs tools degrade
gracefully when the network is down (`ok: false` + hint).

## Server check (once per session)

Confirm the docs tools are available on your `memtrace` MCP server:

- `search_docs` — ranked chunks (slug, title, H2, excerpt)
- `ask_docs` — grounded Q&A `{ answer, citations[], refused }`
- `read_doc` — full page text by slug
- Resources: `memtrace://docs/<slug>` via `read_resource` (optional)

If none of these exist, the MCP build may be outdated — tell the user to update
Memtrace and run `npx -y memtrace-skills@latest install`.

Override API host: `MEMTRACE_DOCS_API_URL` (default `https://memtrace.io`).

## The decision rule

| User is asking | Right tool |
|---|---|---|
| "How do I install / configure / deploy X in Memtrace?" | `ask_docs(question=…)` |
| "What MCP tools / skills / CLI commands exist?" | `ask_docs` or `search_docs` then `read_doc` on hit slugs |
| "What does `memtrace rail enable` do?" | `ask_docs` |
| "Find docs about fleet coordination" | `search_docs(query=…)` |
| "Read the full getting-started page" | `read_doc(slug="getting-started")` |
| "Read enterprise MemDB deploy guide" | `read_doc(slug="enterprise/memdb-deploy")` |
| Need several related sections | `search_docs` → `read_doc` on top slugs |

**Default for natural-language questions:** `ask_docs` — it retrieves context and
returns a cited answer in one call.

**Default for "find the doc about…":** `search_docs` — scan chunks, then `read_doc`
if you need the full page.

## Standard workflows

### "How does X work in Memtrace?" (most common)

1. `ask_docs(question="<user question verbatim>")`
2. If `refused: true` or `ok: false` → `search_docs` with shorter keywords → `read_doc` on best slug
3. Quote the answer; link slugs as `/docs/<slug>` when helpful
4. If docs say "not found", say so — do not invent flags or behavior

### "What tools / commands / skills are available?"

1. `ask_docs(question="What MCP tools are available?")` or fleet/skills variant
2. If the answer lists categories but user wants exhaustive detail → `read_doc(slug="mcp/tools")`
3. For agent skills → `read_doc(slug="mcp/skills")`

### "Read up on X before we implement"

1. `search_docs(query="X", limit=8)`
2. `read_doc(slug=<top hit>)` for each page you will rely on
3. Summarize with citations; use `memtrace-first` only when switching to **their repo's code**

### Enterprise / self-hosted MemDB

1. `ask_docs(question="How do I deploy MemDB with Helm on Azure?")` or user wording
2. Follow-up `read_doc(slug="enterprise/memdb-deploy")` for operator steps
3. Engineer connect → `read_doc(slug="enterprise/connect")` or `cli/connect`

## Tool procedures

### ask_docs — cited answers

Pass the user's question verbatim when it is already clear:

```json
{ "question": "How do I deploy MemDB with Docker Compose?" }
```

The response is `{ ok, answer, citations[], refused, refusalReason? }`. If
`refused: true` with `no_context`, retry `search_docs` with shorter keywords and
then `read_doc` on the best slug. `ask_docs` sends only the question string to
memtrace.io's hosted RAG service; do not include secrets or repo source.

### search_docs — page discovery

```json
{ "query": "deploy MemDB helm azure", "limit": 8 }
```

Results are ranked chunks with `slug`, `pageTitle`, `h2Title`, `excerpt`, and
`distance` (lower is a better match). They are not full pages. Follow with
`read_doc` when you need the complete reference.

### read_doc — complete page text

```json
{ "slug": "enterprise/memdb-deploy" }
```

Use a slug from a user URL, `ask_docs` citations, or `search_docs` results. The
response is `{ ok, slug, title, body }`. For multi-page topics, read each slug you
will rely on rather than extrapolating from one page.

## Red flags — STOP, use docs tools

| Thought | Reality |
|---|---|
| "I know how Memtrace fleet works from training data" | Product docs change — `ask_docs` first |
| "I'll grep the repo for README" | User repo ≠ official docs — use `search_docs` / `read_doc` |
| "I'll web-search Memtrace" | Use hosted docs API — same corpus as memtrace.io/docs |
| `ask_docs` returned `refused: true` | Docs corpus had no match — say so; try `search_docs` with different terms |
| `ok: false` network error | Report offline; core Memtrace graph tools still work locally |

## Relationship to other skills

| Skill | When |
|---|---|
| `memtrace-docs` | Questions about **Memtrace product** (install, MCP, fleet, Cortex, enterprise) |
| `memtrace-first` | Questions about **the user's indexed source code** |
| `memtrace-decision-memory` | **Why** code exists (Cortex decisions) — not product docs |

Use both when needed: docs for "how is Memtrace supposed to work?", graph tools for
"how does *this repo* implement it?"

## Output

Prefer citing doc slugs returned in `citations` or `search_docs` results:

```
ask_docs → { ok: true, answer: "…", citations: ["cli/rail", "mcp/skills"], refused: false }
search_docs → { ok: true, results: [{ slug, pageTitle, h2Title, excerpt }] }
read_doc → { ok: true, slug, title, body }
```

When `refused: true`, tell the user the docs did not cover it and suggest browsing
https://memtrace.io/docs or rephrasing.
