# Privacy and telemetry

A practical summary of what stays on your machine, what's sent to us,
and how to turn anything off. The exhaustive machine-readable version
lives in [`PRIVACY.md`](../PRIVACY.md), [`TELEMETRY.md`](../TELEMETRY.md),
and [`telemetry-compliance-datasheet.md`](telemetry-compliance-datasheet.md)
— those are the contractual versions for legal / compliance reviews. This
doc is the user-friendly explainer.

## What never leaves your machine

- **Your source code.** Indexing, parsing, embedding, ranking — all
  local. No file content is uploaded anywhere.
- **File paths and symbol names.** Stays on disk in your
  `.memdb/` and `~/.memtrace/embed-cache/`. Never transmitted.
- **Repo structure.** Module names, community detection, call graphs
  — all in MemDB on your disk.
- **Secrets and `.env` files.** Memtrace doesn't even index `.env`
  by default (it's not parseable code). Pattern-matched detection
  during Terraform emission flags secret-looking properties.
- **Search queries.** When the agent calls `find_code(query="...")`,
  the query is processed locally. We never see it.
- **Agent conversation history.** That's between you and your AI
  client. Memtrace is invisible to both directions of that channel.

## What does cross the network

Four categories. License validation and the authenticated usage heartbeat
are required for account operation; product telemetry is on by default with
a one-env-var kill switch; the model download is inbound only on first run.

### 1. License validation (required)

On startup and roughly every hour, Memtrace pings our license
service to confirm your session token is still valid. The request
contains:

- A device hash (SHA-256 of stable hardware id + a salt — not
  reversible to your machine identity)
- The product version
- The session token issued at first-run device-flow login

It does NOT contain:

- Repo paths
- File or symbol names
- Query content
- Code

If your machine is offline, Memtrace runs in a grace-period mode
for 24 hours before requiring re-validation. CI / sandboxed
environments use `MEMTRACE_LICENSE_KEY=<key>` instead of device flow.

### 2. Product telemetry (on by default — opt-out)

Memtrace ships with telemetry enabled. It's there to catch crashes,
regressions, and performance issues across the user base — the kind
of bugs that otherwise only surface when someone takes the time to
file an issue. One environment variable turns it off completely.

Four streams flow through a single endpoint
(`https://memtrace.io/api/telemetry/ingest`, HTTPS, authenticated
with the same Bearer session token your install already uses):

| Stream | When | What's in it |
|---|---|---|
| **Usage events** | `memtrace start` / `memtrace mcp` invocation, end of indexing, end of embedding, PR review/watch lifecycle | Subcommand, transport mode, `duration_ms`, integer counts, graph/review mode enums, PR watch status counters. No names, no content, no PR URLs, no comments. |
| **Error reports** | Any `WARN` / `ERROR` log line from Memtrace's own crates | Sanitised log message, tracing target, level, content fingerprint. Recurring errors collapse to one row with an `occurrences` counter. |
| **Crash reports** | Panic hook captures, written synchronously so even a hard exit leaves a breadcrumb | Sanitised panic message, `file:line` inside the Memtrace binary, sanitised Rust backtrace capped at 16 KB. |
| **Rail shadow** | One row per Memtrace-owned code search — **on by default** in `observe`, measured **asynchronously** by the background daemon (the search hook returns instantly and never queries, so zero added latency) | Content-free routing-quality buckets only: `mode`, pattern `shape` enum, `hit`/`miss`, a bucketed score, an on-device `relevance_proxy` yes/no, and a latency bucket. **Never** the search text or which files/symbols matched. Opt out via `MEMTRACE_RAIL_SHADOW=off`. |

Every payload also carries: a stable per-machine `device_id`, the
binary version, OS string, and host-tier score. Nothing else.

**Sanitisation before anything ships.** Error messages, panic
messages, and backtraces pass through a sanitiser that:

- Collapses any absolute path under `$HOME` to `~`
- Replaces token-shaped strings (regex `[A-Za-z0-9_+/=-]{40,}`) with
  `<redacted-token>` — catches API tokens, session tokens, JWTs,
  GitHub PATs, base64-encoded secrets
- Replaces email addresses with `<redacted-email>`

The sanitiser source is public at
[`crates/memtrace-mcp/src/telemetry.rs`](https://github.com/syncable-dev/memtrace-public/blob/main/crates/memtrace-mcp/src/telemetry.rs).
The sanitiser is conservative but not magic — it doesn't strip
directory structure below `$HOME`, and it doesn't semantically
classify content. If your data classification policy treats *any*
path component below `$HOME` as sensitive (for example, client
codenames in directory names), set `MEMTRACE_TELEMETRY=off`. See
the [compliance datasheet](telemetry-compliance-datasheet.md) §4.1
for the full discussion of the sanitiser's limits.

**Specifically NOT collected**, ever, under any configuration:

- Source code or file contents
- The text of your `grep`/`find`/search commands — Rail records only the
  pattern *shape*, never the query
- Which files, symbols, or results a search matched — only an on-device
  relevance yes/no
- Symbol names from your codebase
- Embeddings, BM25 indices, or any derived data from your code
- Repository names, paths, or remote URLs
- Branch names, commit messages, or git history
- Any path that points inside the indexed repo (except where it
  appears in a sanitised crash backtrace — see above)
- Environment variable values directly
- IP addresses on the server side (request logs kept 7 days for
  abuse mitigation only)

### 3. Authenticated usage heartbeat (required)

While Memtrace is running, an authenticated heartbeat keeps license and quota
state current. It sends aggregate node, edge, and episode counts plus billable
query usage. Organization installs also send their organization and seat
identity. MemDB deployments can send the configured endpoint, deployment mode,
and an aggregate record count.

The heartbeat does not send repository names, paths, remote URLs, branches,
source code, file or symbol names, query text, decision content, or an activity
feed. It is an account operation, so `MEMTRACE_TELEMETRY=off` does not disable
it.

### 4. Embedding model download (first run only, inbound)

The first time you run Memtrace, it downloads the embedding model
(~340 MB ONNX) and the cross-encoder reranker (~75 MB int8) from
HuggingFace Hub via the `fastembed` library. This is an **inbound
download only** — nothing about your machine is uploaded. Cached
to `~/.cache/fastembed/` after first run.

## How to control telemetry

### Disable for the current process

```bash
MEMTRACE_TELEMETRY=off memtrace start
```

Accepted off-values: `off`, `0`, `false`, `disabled`, `no` (case
insensitive). Anything else (including unset) keeps telemetry on.

### Disable permanently (shell profile)

```bash
# ~/.zshrc / ~/.bashrc
export MEMTRACE_TELEMETRY=off
```

### Disable permanently (MCP client config)

```json
{
  "command": "memtrace",
  "args": ["mcp"],
  "env": { "MEMTRACE_TELEMETRY": "off" }
}
```

Applies to Claude Code, Cursor, Codex, Windsurf, and any other MCP
client that honours the `env` block.

### What "disabled" actually does

When `MEMTRACE_TELEMETRY=off` is set:

- The panic hook still installs locally — so a crash in a disabled
  session still leaves a `~/.memtrace/telemetry/queue.jsonl`
  breadcrumb for your own debugging — but the flusher never ships
  the file.
- The `tracing` layer becomes a no-op for telemetry: `WARN`/`ERROR`
  lines still print to stderr but are not queued.
- The flusher exits immediately on startup. Zero network calls to
  the telemetry endpoint.
- Usage event callsites short-circuit before any data is
  constructed.

License validation and the heartbeat continue to run — they're
required for the product to function. Only the telemetry endpoint
goes quiet.

## How to inspect what's in the queue before it ships

Telemetry sits on disk before being flushed every 60 seconds. You
can read it directly:

```bash
cat ~/.memtrace/telemetry/queue.jsonl | head -5
```

Each line is one record. The `kind` field is `event`, `error`, or
`crash`. There is no separate raw buffer — what you see here is
everything. If telemetry is off, the file isn't written.

## Default state

| Item | Default | Controllable? |
|---|---|---|
| License validation | ON | Required — no opt-out (use offline grace period if needed) |
| Product telemetry (events / errors / crashes) | ON | `MEMTRACE_TELEMETRY=off` disables completely |
| Model download | ON (first run only) | Block `huggingface.co` after first run if needed |

If you want telemetry disabled before the binary ever ships its
first event, set `MEMTRACE_TELEMETRY=off` in your shell profile
before running `memtrace start` for the first time.

## Where the data goes

All telemetry endpoints terminate on our own infrastructure
(`*.memtrace.io`), operated by Syncable ApS in Denmark / EU. No
third-party analytics SDKs are embedded in the binary — every byte
of the pipeline is in the public repo at
[`crates/memtrace-mcp/src/telemetry.rs`](https://github.com/syncable-dev/memtrace-public/blob/main/crates/memtrace-mcp/src/telemetry.rs).
Storage: four Postgres tables (`telemetry_events`,
`telemetry_errors`, `telemetry_crashes`, and `rail_shadow` — the last
holding content-free Rail routing-quality buckets, opt-out via
`MEMTRACE_RAIL_SHADOW=off`). Access: admin dashboard
at `https://memtrace.io/admin/analytics`, gated to `@syncable.dev`
accounts only. We do not sell, share, or publish anonymised
aggregates without notice.

Right of erasure: email `support@syncable.dev` with the `device_id`
visible in `~/.memtrace/credentials.json`. Erasure processed within
30 days.

If your organisation has compliance requirements (SOC 2
questionnaire, DPA, GDPR Article 30 record), email
`support@syncable.dev` and we'll provide the documentation. The
formal version is [`telemetry-compliance-datasheet.md`](telemetry-compliance-datasheet.md).

## Network egress summary

If you want to firewall Memtrace, the outbound destinations are:

- `*.memtrace.io` (license + telemetry)
- `huggingface.co` and `cdn-lfs*.huggingface.co` (model downloads,
  first run only — cached locally after)
- `registry.npmjs.org` (only when running `memtrace install` to
  upgrade)

Block any of these and the product still runs (offline grace
period for license, no auto-upgrades, no model updates), it just
gets slowly less functional.

## TL;DR

- We never see your code, queries, or repo data.
- License validation needs an hourly-ish ping with no content.
- Product telemetry is **on by default** — sanitised crash, error,
  usage events, and aggregate PR review/watch counters. One env var
  disables it.
- `MEMTRACE_TELEMETRY=off` is the kill switch (also accepts `0`,
  `false`, `disabled`, `no`).
- You can `cat ~/.memtrace/telemetry/queue.jsonl` any time to see
  exactly what's queued before it ships.

For the formal versions, see [`PRIVACY.md`](../PRIVACY.md),
[`TELEMETRY.md`](../TELEMETRY.md), and the compliance datasheet at
[`telemetry-compliance-datasheet.md`](telemetry-compliance-datasheet.md).
