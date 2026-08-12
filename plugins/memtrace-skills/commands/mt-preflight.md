---
description: Shortcut to the canonical Memtrace preflight skill
argument-hint: "<symbol> [repo_id]"
disable-model-invocation: true
---

Use the `Skill` tool to invoke `memtrace-skills:memtrace-preflight` exactly once,
passing `$ARGUMENTS` unchanged. Treat that skill as the sole source of truth for
repository resolution, current MCP schemas, safety rules, and output. Do not
call MCP tools before loading it or reproduce its workflow here.
