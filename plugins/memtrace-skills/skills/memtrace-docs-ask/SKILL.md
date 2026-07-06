---
name: memtrace-docs-ask
description: "Answer questions from official Memtrace documentation only — installation, CLI, MCP tools, fleet, Cortex, enterprise MemDB deploy, skills, configuration. Use when the user asks how Memtrace works or wants a cited explanation. Calls ask_docs on memtrace.io (RAG + guardrails). Do not guess product behavior from training data."
---

## Overview

`ask_docs` sends the user's question to the hosted docs RAG backend. The server
retrieves relevant doc chunks, generates an answer **only from that context**, and
returns citations. Off-topic or ungrounded questions return `refused: true`.

This is the **default** tool for "how does Memtrace X work?" questions.

Umbrella routing: `memtrace-docs` workflow.

## `ask_docs` parameters

| Param | Required | Notes |
|---|---|---|
| `question` | yes | Pass the user's question verbatim when possible |

```json
{ "question": "How do I deploy MemDB with Docker Compose?" }
```

## Response shape

```json
{
  "ok": true,
  "answer": "…markdown with /docs/… links…",
  "citations": ["enterprise/memdb-deploy", "cli/connect"],
  "refused": false
}
```

| Field | Meaning |
|---|---|
| `answer` | Grounded response text |
| `citations` | Doc slugs used as context |
| `refused` | `true` when no context or injection detected |
| `refusalReason` | `no_context` or `injection` when refused |

## Steps

### 1. Ask

Use the user's exact wording when it is already a clear question:

```json
{ "question": "What MCP tools are available?" }
```

### 2. Handle refusal

If `refused: true` with `no_context`:

1. Retry `search_docs` with shorter keywords
2. `read_doc` on the best slug
3. If still empty — tell the user docs did not cover it; do not invent

### 3. Deepen with read_doc

When the user needs exhaustive tables (e.g. all MCP tools):

```json
{ "slug": "mcp/tools" }
```

After `ask_docs` gave a summary.

## Example questions → ask_docs

| User asks | ask_docs question |
|---|---|
| How to install Memtrace? | `"How do I install Memtrace?"` |
| What is Rail? | `"What is Memtrace Rail and how do I enable it?"` |
| Enterprise MemDB on Azure | `"How do I deploy self-hosted MemDB on Azure?"` |
| How to connect local memtrace to shared MemDB | `"How do engineers connect with memtrace connect?"` |
| What skills exist? | `"What agent skills does Memtrace ship?"` |

## Privacy note

`ask_docs` sends the **question string** to memtrace.io, which runs retrieval and
calls a hosted LLM (DeepSeek) server-side. No repo source code is transmitted.
Do not paste secrets into questions.

## Offline behavior

`ok: false` → network or API error. Quote the `error` and `hint` fields; suggest
checking connectivity or `MEMTRACE_DOCS_API_URL`.
