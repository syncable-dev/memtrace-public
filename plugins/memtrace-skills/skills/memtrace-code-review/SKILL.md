---
name: memtrace-code-review
description: "Review GitHub pull requests with Memtrace's local graph-backed review engine. Use when the user asks to review a GitHub pull request, run Memtrace code review, post Memtrace review comments, create a PR with a review step, or publish local graph-backed review findings to GitHub. Prefer the review_github_pr MCP tool over manual diff inspection. Do not use for local working-tree diffs — that is the built-in /code-review; this skill is for GitHub PRs via review_github_pr."
---

## Overview

Use Memtrace's local-first PR review workflow. The agent should call the `review_github_pr` MCP tool so review runs against the developer's local indexed graph, AST detectors, YAML rule pack, and review policy. GitHub is used for PR context and optional comment publication; source code analysis stays local.

## Default Flow

1. If the user gives a GitHub PR URL and asks to inspect or review it, call `review_github_pr` with `post: false`.
2. If the user explicitly asks to publish, post comments, or complete the PR review, call `review_github_pr` with `post: true`.
3. Use `graphMode: "strict"` by default. Use `graphMode: "off"` only when the user asks to benchmark non-graph behavior or the local graph is unavailable.
4. Default to `minSeverity: "high"` and `maxComments: 5` when posting. For previews, `maxComments: 10` is acceptable.
5. Pass `repoRoot` when the PR checkout is not the current working directory. Pass `repoId` when the indexed repository id is known.

Full parameter spec for every Memtrace tool: `references/mcp-parameters.md` (bundled at the memtrace-skills plugin root).

## Example User Prompts

- "Review this PR with Memtrace: https://github.com/OWNER/REPO/pull/123"
- "Use Memtrace to review this pull request and post the findings: https://github.com/OWNER/REPO/pull/123"
- "Create the PR, then run Memtrace code review and publish the review comments."

## Guardrails

- Do not start with generic grep, rg, or manual diff review when `review_github_pr` is available.
- Do not post comments unless the user explicitly requested publication.
- After posting comments (`post: true`), record the PR URL and the posted comment IDs in the session output as the audit trail.
- Do not create benchmark-specific or PR-specific findings. The review must come from general Memtrace detectors, graph evidence, and policy ranking.
- If the tool reports missing auth, tell the user to run `memtrace auth login`.
- If the tool reports missing GitHub App installation, tell the user to install Memtrace Code Reviewer on that repository.
- If the tool reports missing local graph context, tell the user to run
  `memtrace index .` from the actual repo root. If the open folder is a parent
  containing multiple independent repos, do not index the parent unless the user
  explicitly wants a shared workspace.

## Output

Summarize the `review_github_pr` result with these fields:

| Field | Preview (`post: false`) | Posted (`post: true`) |
|---|---|---|
| PR URL + repository | yes | yes |
| Graph state | yes | yes |
| Findings | count of candidate comments, plus file, line, severity, message per finding | number of comments posted |
| Posted comment IDs | — | yes — record with the PR URL as the audit trail |
