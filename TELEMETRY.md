# Memtrace Telemetry

Memtrace ships with opt-out telemetry that helps us spot crashes, regressions,
and performance issues across the user base — the kind of problems that
otherwise only show up when someone takes the time to file an issue or DM
us. The telemetry pipeline exists for one reason: **to make the product
better for the people running it**.

This document covers what we collect, what we don't, where the data goes,
and how to turn it off.

> **TL;DR**
> - We never collect source code, file contents, file paths beyond what's
>   needed for crash fingerprints, symbol names, or embeddings.
> - We collect: app starts, **the name and duration of each Memtrace tool
>   call** (which tool you invoked — never its arguments or results), PR
>   workflow counters, panic reports, and `WARN`/`ERROR` log lines from our
>   own crates.
> - We also collect content-free **Rail routing-quality** buckets (was the
>   result relevant — never the search text or matched files), measured
>   **asynchronously** by the background daemon so it never slows a search.
> - Set `MEMTRACE_TELEMETRY=off` to disable everything (or
>   `MEMTRACE_RAIL_SHADOW=off` for just the Rail buckets).
> - Default is **on for crashes, errors, usage events, and the content-free
>   Rail routing-quality buckets**. Opt-out is one env var or one config-file
>   line.

---

## Authenticated Account Heartbeat

The account heartbeat is separate from the opt-out telemetry streams below.
It keeps an authenticated install connected to its license and quota. Because
it is part of account operation, `MEMTRACE_TELEMETRY=off` does not disable it.

The heartbeat currently sends:

- Aggregate node, edge, and episode counters
- Billable query usage since the previous heartbeat
- The authenticated organization and seat identity for organization installs
- For MemDB deployments, the configured endpoint, deployment mode, and an
  aggregate record count

It does not send repository names, repository paths, remote URLs, branch
names, source code, file or symbol names, query text, decision content, or an
activity-event feed.

---

## What We Collect

There are four streams (the fourth, Rail shadow, on by default in `observe`
and measured asynchronously off your critical path). Each one ships to
`https://memtrace.io/api/telemetry/ingest` over HTTPS, authenticated with
the same Bearer session token your install already uses for the heartbeat.

### 1. Usage events (`telemetry_events`)

One row per discrete signal the binary emits. The complete list of events:

| Event | When it fires | Data attached |
|---|---|---|
| `start` | Every `memtrace start` / `memtrace mcp` boot that establishes a license session | subcommand (`start` / `mcp`); transport mode on the `mcp` path |
| `start_degraded` | A boot that continued *without* a license session (expired install token, network blip, headless box) | subcommand, transport mode, and a failure **class** from a fixed enum (e.g. `session_expired`, `offline_license_expired`) — the auth error's message is classified on your machine and the original string discarded |
| `mcp_call` | **Every MCP tool call**, on both the success and the error path | the **name** of the Memtrace tool invoked (e.g. `find_code`); status (`ok` / `degraded` / `error`); result count (how many results, never which); the tool's function-class tone (`cyan` discovery, `magenta` inspection, `lime` analysis, `amber` history, `slate` generic); and duration |
| `rest_tool_call` | Every tool call over the local REST surface (the dashboard's HTTP API) | Same fields as `mcp_call` |
| `pr_review_completed` | After `memtrace code-review` finishes a PR review run | posted/dry-run boolean, watch boolean, comment count, finding count, graph mode, review mode, minimum severity, severity-count buckets, source-count buckets |
| `code_review_completed` | After a local `memtrace code-review` run (diff, stdin, or git range — nothing posted) | input kind (`github_pr` / `diff` / `git_range`) plus the same count and mode fields as above |
| `pr_watch_registered` | When `memtrace code-review --post --watch` registers a local PR watch | comment count, graph mode, local watch status |
| `pr_watch_synced` | When `memtrace start`, `memtrace mcp`, or `memtrace pr sync` polls watched PRs | aggregate watch counts by status: awaiting response, human replied, approved, changes requested, stale after push, merged, closed, poll errors |
| `pr_watch_poll_error` | The first time a watched PR poll fails, and again whenever the failure kind changes | coarse error kind only: `rate_limited`, `token`, `github`, `parse`, or `unknown` |
| `drift_fallback` | When the incremental watcher path gives up and falls back to a full re-index | the call site (`live_pull` / `live_save` / `startup_drift` / `startup_reconcile` / `hosted_push`), a reason **class** from a fixed enum (the raw reason is classified on your machine and the original string discarded), plus optional changed-file and propagation counts |

Duration is only recorded for `mcp_call` and `rest_tool_call`; every other
event leaves it null.

Each row also carries: a stable per-machine `device_id` (the same one you
see in your `~/.memtrace/credentials.json`), the binary version (e.g.
`0.3.17`), the OS string (e.g. `macos-aarch64`), and the host-tier score
the resource detector picked. Nothing else.

**What this lets us see:** how many people run Memtrace each day, which
tools actually get used, whether a specific tool got slower or started
failing in a recent release, how often incremental indexing falls back to a
full rebuild, and whether the auto-tuned `light/standard/heavy` tiers are
landing in the right buckets on real hardware.

**What it deliberately doesn't tell us:** what you asked a tool *for*, or
what it gave back. `find_code` and `get_impact` report only that they ran,
how long they took, and how many results they returned — never the query,
the matched symbols, or the repo. The result IDs behind that count go to
the local dashboard over your machine's own WebSocket; only the count
leaves.

For PR review telemetry, the local watcher may store PR coordinates in
`~/.memtrace/pr-watches.json` so it can poll GitHub later, but the telemetry
payload deliberately does not include those coordinates. PR URLs,
repository owner/name, branch names, commit SHAs, file paths, comment bodies,
reviewer identities, and discussion text stay local.

### 2. Errors (`telemetry_errors`)

The binary uses `tracing` for all internal logging. Anything we log at
`WARN` or `ERROR` level inside our own crates is mirrored to the
telemetry queue.

Before a row reaches the queue we run it through a sanitiser:

- Absolute paths under `$HOME` collapse to `~`
- Strings that look like API tokens / session tokens / GitHub PATs match
  a regex (`[A-Za-z0-9_+/=-]{40,}`) and get replaced with
  `<redacted-token>`
- Email addresses get replaced with `<redacted-email>`

Then the row gets a content fingerprint
(`sha256(version || target || level || first 6 message tokens)`).
Recurring errors with the same fingerprint **don't fan out into hundreds
of rows** — they bump an `occurrences` counter on a single row.

**What this lets us see:** "v0.3.16 introduced a new WARN that didn't
exist in v0.3.15", or "23% of macOS-aarch64 users hit this fastembed
init warning". Those are the signals that drive bug fixes.

### 3. Crash reports (`telemetry_crashes`)

If the binary panics, the panic hook captures:

- The panic message (sanitised the same way as errors)
- The crash location as `file:line` (e.g. `src/main.rs:42`)
- The Rust backtrace, capped at 16 KB and run through the same sanitiser

These get written to a local file at
`~/.memtrace/telemetry/queue.jsonl` synchronously inside the panic hook,
so even a hard crash that exits the process gets captured. They flush
to memtrace.io on the next successful run.

**What this lets us see:** the regressions that nobody bothered to file
an issue about. Pre-telemetry, the M3 Air "stuck on Loading embedding
model" hang and the Windows MSVC build failures were each visible to us
only after a user took the time to DM us — for every user who told us,
several others probably hit the same thing and quietly uninstalled.

### 4. Rail shadow telemetry (`rail_shadow`)

Memtrace Rail is the optional router that can intercept code-discovery
searches (grep/ripgrep/find for source symbols) and answer them from
Memtrace's graph instead of raw text search. Before we ever consider making
Rail active by default, we need evidence that its answers can be trusted — so
when Rail is active it records a **content-free** measurement of what it
*would* have returned, without ever capturing your search.

One row per Memtrace-owned code search, carrying only categories and buckets:

| Field | Example | What it is |
|---|---|---|
| `mode` | `observe` / `nudge` / `rail` / `strict` | Rail's operating mode |
| `surface` | `memtrace_owned` | the search was for source symbols in an indexed repo |
| `would_route` | `true` | whether Rail would route this to Memtrace |
| `shape` | `identifier` / `alternation` / `phrase` / `regex` / `empty` | the **shape** of the pattern — never the text itself |
| `retrieval` | `hit` / `miss` / `unavailable` | did Memtrace have a confident answer |
| `score_bucket` | `lt10` / `b10` / `b25` / `gte50` | bucketed relevance score (never the raw number) |
| `relevance_proxy` | `true` / `false` | computed **on your machine**: did the top result's name/path contain a token from the search? Only this yes/no leaves — the strings are compared locally and discarded |
| `latency_bucket` | `fast` / `mid` / `slow` | how quickly Memtrace answered |

Plus the same `device_id` / `version` / `os` envelope as the other streams.

**When it sends:** by default, in `observe` mode (every install). Crucially it
is measured **asynchronously, off your critical path**: the search hook records
a request and returns **instantly** — it never queries the backend — and the
long-running daemon performs the measurement in the background. So it **adds no
latency to any search** (the `grep`/`find` runs exactly as before). Enforcing
modes (`memtrace rail enable nudge|rail|strict`) additionally measure inline,
since Rail already has to query in order to route. Opt out with
`MEMTRACE_TELEMETRY=off` (all telemetry) or `MEMTRACE_RAIL_SHADOW=off` (Rail
only); `MEMTRACE_RAIL_SHADOW_SAMPLE=0..1` bounds the background work on busy
machines.

**What this lets us see:** whether Rail's answers are actually relevant
(precision), how often it can help (coverage), which query shapes it handles
well, and the right confidence threshold — so any decision to make Rail
active by default is backed by real evidence, not guesswork. What it never
tells us: *what* you searched for, or *which* files/symbols a search matched.

---

## What We Don't Collect

We don't have to manage tradeoffs here because the categories are clean:
**none of the following ever leaves your machine via telemetry**, and the
data model on the receiving end has no column to put them in.

- ❌ Source code or file contents
- ❌ The text of your `grep`/`find`/search commands — Rail records only the
  pattern *shape* (`identifier`/`regex`/…), never the query you typed
- ❌ Which files, symbols, or results a search matched — only a local yes/no
  relevance bucket, computed on your machine
- ❌ Symbol names from your codebase
- ❌ Embeddings, BM25 indices, or any derived data from your code
- ❌ Repository names, paths, or remote URLs
- ❌ GitHub PR URLs, issue/review/comment bodies, reviewer identities, or
  pull request discussion text
- ❌ Branch names, commit messages, or git history
- ❌ Any path that points inside the indexed repo
- ❌ Environment variables (the sanitiser strips token-shaped strings,
  but we never read env values directly into telemetry payloads)
- ❌ IP addresses (we don't log them server-side; standard request logs
  are kept for 7 days for abuse mitigation only)

**One thing that *is* collected and worth stating plainly:** the `mcp_call`
and `rest_tool_call` events record the **name of each Memtrace tool you
invoke**, along with its status, duration, and result count. That name comes
from a fixed set of Memtrace-owned identifiers (`find_code`, `get_impact`,
…) and is not derived from your code — but it does mean we can see *which
Memtrace features you use and how often*. What we cannot see is what you
asked for or what came back.

Note also that the sanitiser above runs over `WARN`/`ERROR` log lines and
panic reports. Usage-event properties are **not** passed through it — they
are built field-by-field from enums, booleans and counters, so there is no
free text to strip. That is enforced rather than assumed: the last
free-form string in the stream was `start_degraded`'s reason, which carried
the license/auth error's display text verbatim. It is gone, replaced by a
`reason_class` from a fixed enum, derived on your machine by matching on
the error *variant* rather than its message.

If a panic backtrace happens to include a path inside one of your
repositories — say a tree-sitter library hit an assertion while parsing
your code — the path component still gets sanitised (home dir → `~`)
but the backtrace is otherwise verbatim. If you'd rather opt out of that
risk completely, set `MEMTRACE_TELEMETRY=off`. We'd rather you stay opted
in and tell us if you find a backtrace that looks too revealing.

---

## Where the Data Goes

- **Transport**: HTTPS to `https://memtrace.io/api/telemetry/ingest`,
  authenticated with the same Bearer session token your install uses for
  the existing license heartbeat. No third-party analytics SDK is
  embedded — every byte of the pipeline is in this repo at
  `crates/memtrace-mcp/src/telemetry.rs`.
- **Storage**: four Postgres tables on the memtrace.io infrastructure
  (`telemetry_events`, `telemetry_errors`, `telemetry_crashes`, and
  `rail_shadow`), schema in `memtrace-ui/drizzle/0002_telemetry.sql` and
  `memtrace-ui/drizzle/0018_rail_shadow.sql`. Retention is unlimited today;
  we'll publish a retention policy before exceeding 90 days of data.
- **Access**: the admin analytics dashboard at
  `https://memtrace.io/admin/analytics` is gated to `@syncable.dev`
  email accounts only. We do not share or sell this data, and we don't
  publish anonymised aggregates without notice.

---

## How to Turn It Off

### Environment variable (per process)

```bash
MEMTRACE_TELEMETRY=off memtrace start
```

Accepted off-values: `off`, `0`, `false`, `disabled`, `no`. Anything
else (including unset) keeps telemetry on.

When disabled:

- The panic hook still installs (so a crash in a disabled-telemetry
  session still leaves a local breadcrumb in `~/.memtrace/telemetry/`),
  but the file never gets shipped.
- The tracing layer becomes a no-op — no in-memory aggregation, no
  queue writes for `WARN`/`ERROR`.
- The flusher goroutine exits immediately — no network calls.
- Usage events from `record_event()` short-circuit.

### Make it permanent

Add this to your shell profile:

```bash
# ~/.zshrc / ~/.bashrc
export MEMTRACE_TELEMETRY=off
```

Or set it in your editor's MCP config so the daemon mode picks it up:

```json
{
  "command": "memtrace",
  "args": ["mcp"],
  "env": { "MEMTRACE_TELEMETRY": "off" }
}
```

---

## Verifying What's in the Queue

Telemetry sits on disk before being shipped. You can read it directly:

```bash
cat ~/.memtrace/telemetry/queue.jsonl | head -5
```

Each line is one record. The `kind` field marks it as `event`, `error`,
or `crash`. There is no separate "raw" buffer — what you see here is
everything.

If you want to inspect what *would* have been shipped without actually
shipping it, set `MEMTRACE_TELEMETRY=off` (the queue still won't be
written) and then read the JSONL on a fresh run after un-setting it.

---

## Changes to This Policy

Material changes to what we collect, where it's stored, or how long it's
kept will be announced in:

- The release notes of the version that introduces the change
- This file (with a `## Changelog` section at the bottom)
- The `memtrace-public/PRIVACY.md` summary

If you have questions or spot something that should be sanitised but
isn't, open an issue at
[github.com/syncable-dev/memtrace-public](https://github.com/syncable-dev/memtrace-public)
or email [support@syncable.dev](mailto:support@syncable.dev).
