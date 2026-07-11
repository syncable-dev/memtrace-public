# Workflows

You ask your agent — Claude Code, Cursor, Codex, whatever — questions in plain
English. When Memtrace is wired in, the agent automatically routes the
right ones through Memtrace's knowledge graph instead of grepping
through files. **You don't call any tools yourself.** This page shows
the kinds of questions Memtrace is good at, and what the agent gives
back.

If you're curious which tools the agent picks under the hood, look at
[`tools.md`](tools.md). You'll never need to memorise it.

## Pull request code review

When you want a branch, PR, and review loop, Memtrace can do the whole
flow from the CLI or through the `memtrace-code-review` agent skill.

**Ask your agent:**

> "Create a feature branch for this change, push a PR, run Memtrace
> code review, and post the findings."

Or run it yourself:

```bash
memtrace code-review setup

memtrace code-review \
  --pr https://github.com/OWNER/REPO/pull/123 \
  --post \
  --watch \
  --repo-root "$PWD"
```

Then use GitHub comments like `@memtrace explain`, `@memtrace fix this`,
or `@memtrace rerun` on the PR. The full workflow is in
[`code-reviewer.md`](code-reviewer.md).

## Onboarding to an unfamiliar codebase

You just cloned someone else's repo and you have no idea what it does.

**Ask your agent things like:**

> "Give me a quick tour of this codebase."
>
> "What does this project do, what are the main modules?"
>
> "Which functions or classes are most central — where should I start
> reading?"
>
> "What are the main flows — like, signup, request handling, that
> kind of thing?"

**What you get back:**

A short prose summary of the project, a list of major modules with
brief descriptions, the most-called / most-imported symbols (your
"start reading here" list), and the named execution flows. The agent
gets all of that in a handful of structured queries — typically a few
thousand tokens, not the tens of thousands an unguided exploration
would burn.

You can keep going from there. "Tell me more about the auth module."
"How does the request flow through the middleware?" The agent drills
in symbol-by-symbol, file-and-line accurate, without re-reading whole
files.

## "Where is X?" / "How does Y work?"

The bread-and-butter case.

**Ask:**

> "Where's the user-login function?"
>
> "How does the rate limiter work in this codebase?"
>
> "Find the function that uses STRIPE_API_KEY."
>
> "Where's the error message 'connection refused for tenant'?"

**What happens:**

The agent calls Memtrace's hybrid search. Symbol names, signatures,
function bodies, string literals inside function bodies — all
indexed. The agent gets back ranked `file:line` matches and reads
only the lines it needs to answer you.

The string-literal case is worth highlighting: Memtrace embeds the
first ~1500 chars of every function body, so an error message,
constant, or magic value baked into code is findable through plain
language search. Your agent doesn't need to grep.

## "What breaks if I change X?" — before any refactor

The most useful question a human can ask before touching shared code.

**Ask:**

> "I want to change the signature of `process_order`. What else
> calls it?"
>
> "If I rename `User.email` to `User.contact_email`, what's the blast
> radius?"
>
> "Show me everything that depends on this function transitively."

**What you get:**

A list of every caller (direct and transitive) with file:line
locations, grouped by module. Often something like:

> "47 callers across 12 files in 4 modules. The most-affected modules
> are `payments/` (15 callers) and `notifications/` (9). Critical
> bridges: `notify_customer` is on the path between order processing
> and email sending."

Now you know whether your refactor is "rename in one place" or
"month-long migration with a deprecation shim". The agent can
size the work before you start.

## "Why is this code like this?" — historical investigation

Useful for debugging "this looks weird, who wrote it and why?"
without git blame archaeology.

**Ask:**

> "When was this function added, and what's it changed to over time?"
>
> "What changed in `auth.ts` last week?"
>
> "Show me how this class evolved over the last 30 days."

**What you get:**

A timeline of every version of the symbol or file, with the commit
(or in-progress save) that introduced each change. The agent can
quote the actual prior content without you checking out an old
branch.

This is the bi-temporal layer doing its thing — every symbol has
`valid_from` / `valid_to` timestamps tied to either a real git
commit or a working-tree save (the live file watcher). You can ask
"what did this look like 3 weeks ago" and get a concrete answer.

## Debugging an incident

Production breaks. You need to know what changed and what's
affected, fast.

**Ask:**

> "Something broke this morning around 9 AM. What changed in the auth
> path between yesterday and now?"
>
> "We're seeing failures in `process_payment`. What does it depend
> on, and has any of that changed recently?"
>
> "Are there any chokepoint functions that were touched recently? I
> think a recent change cascaded."

**What you get:**

The recently-changed files in the relevant subsystem, the symbols
inside them that moved, the blast radius of those changes, and a
flag if any high-betweenness "bridge" symbols were among them.
Bridge symbols are the chokepoints — when one of those gets a bad
edit, it cascades.

Then drill into the suspicious commit:

> "Walk me through the change to `validate_session` from yesterday."

The agent quotes the before/after directly from the bi-temporal
graph — no `git diff` reconstruction needed.

## "I want to remove a feature"

Removing code is harder than adding it. Memtrace makes the search
pass exhaustive.

**Ask:**

> "I'm trying to delete the legacy export-CSV feature. Find every
> reference."
>
> "We're sunsetting the `BETA_PROFILE_V1` feature flag. Where is it
> used?"

**What you get:**

A complete list of references — including ones that appear inside
function bodies as string literals, comments, or test fixtures. The
agent doesn't miss things grep would miss.

After your changes:

> "Here's my removal diff. Did I get everything?"

The agent compares the diff against the call graph and flags any
remaining references.

## Worktrees / parallel agents

If you're running multiple Claude Code (or Codex / Gemini CLI)
sessions concurrently against the same repo, this just works — but:

1. Memtrace **automatically skips** `.claude/worktrees/` so worktrees
   don't double-index your codebase. If your worktree directory has
   a non-standard name, add it to `.memtraceignore`.
2. If you deliberately register sibling worktrees with
   `watch_directory`, use the same `repo_id` that
   `list_indexed_repositories` returns. A made-up or stale id is rejected
   instead of quietly creating a watch that cannot index anywhere useful.
   `list_watched_paths` is the quick way to check the live list.
3. `git worktree remove` does not need a manual restart. The watcher drops a
   vanished root from memory and `~/.memtrace/watches.json` within 30 seconds.
   Call `unwatch_directory` first if you want it gone immediately.
4. After merging a long-running worktree, ask your agent:

   > "Some files were removed from this branch. Can you have Memtrace
   > clean up the orphan symbols?"

   The agent will run a dry-run cleanup, show you what would be
   removed, and proceed if you confirm.

For full orchestration platforms (running fleets of agents), see
[`mcp-and-transports.md`](mcp-and-transports.md).

## Cross-service tracing

If you've indexed multiple repos that talk to each other over HTTP
(microservices, frontend + backend, etc.):

**Ask:**

> "Where is `/users/:id` defined, and who calls it?"
>
> "Draw me a diagram of which services call which others."
>
> "If I change the response shape of `/api/orders`, what consumers
> would break?"

**What you get:**

Cross-repo HTTP call graph. Memtrace auto-detects endpoints in
~12 frameworks (Express, FastAPI, Axum, Spring Boot, Gin, NestJS,
Encore, Flask, …) and the call sites that hit them, including from
*other indexed repos*. The diagram is Mermaid-ready — paste it into
your docs.

For this to work the repos must share one knowledge graph — index
them under a single **workspace**. See [`workspaces.md`](workspaces.md)
for the `.memtrace-workspace` marker and the `--workspace` flag.

## "How am I doing on token cost?"

Memtrace estimates how many tokens it saved you by answering
structurally instead of via Read/Grep/Glob. The local dashboard at
**http://localhost:3030** has a "Value Ledger" panel that breaks
this down per session.

You can also ask the agent:

> "How big is this codebase in the index? What languages?"

and get a high-level summary.

## When you should NOT lean on Memtrace

A few things genuinely need the file tools, not Memtrace:

- **Config and data files** (`.env`, `package.json`, `pyproject.toml`,
  raw YAML / JSON / TOML). Memtrace indexes parseable code, not
  config. Tell your agent "read `package.json` and tell me…" — it
  uses the right tool automatically.
- **File-inventory questions.** "How many `*.test.ts` files exist?"
  is a glob, not a knowledge-graph question.
- **Searches outside the indexed repo.** Memtrace doesn't see
  `node_modules/`, `target/`, or system headers. The agent will
  fall back to grep for those.

You don't need to remember any of this — your agent's
`memtrace-first` skill handles the routing. These are listed here
just so you understand when Memtrace is and isn't pulling its weight.

## Tips for getting better answers

- **Be concrete about scope.** "What are the most important
  authentication functions?" gets a better answer than "what's
  important?". The narrower the scope, the more useful the
  structural ranking is.
- **Ask "why" questions, not just "where".** Memtrace's evolution
  + co-change tools shine on questions like "why does this code
  look this way" or "what historically changes alongside this
  function".
- **Pair impact + evolution before refactors.** "What does X depend
  on, and has any of it changed recently?" is one of the
  highest-leverage questions you can ask.
- **For unfamiliar repos, start broad and zoom in.** Begin with
  "what does this codebase do" and follow up on whichever module
  the answer surfaces. Don't try to ask about a specific function
  before you have the structural map.

## Tips for skeptical agents

If your agent is doing 20 file reads instead of asking Memtrace,
something's off — the skills aren't loaded, the daemon isn't
running, or the MCP isn't wired. See
[`troubleshooting.md`](troubleshooting.md#agent-isnt-using-the-mcp).

A working setup looks like the agent answers in seconds with exact
file:line citations and visibly *doesn't* spam Read/Grep tool calls.
A broken setup looks like normal grep-driven exploration. The
difference is dramatic; you'll notice.

## What's documented elsewhere

- The full list of MCP tools the agent has access to is in
  [`tools.md`](tools.md). You don't need to know it, but it's there
  for completeness.
- Recipes for orchestration platforms (Orbit-style) and HTTP
  multiplexing are in
  [`mcp-and-transports.md`](mcp-and-transports.md).
- If something doesn't work, [`troubleshooting.md`](troubleshooting.md)
  has the fixes.
