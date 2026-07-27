# Memtrace Telemetry — Compliance Datasheet

A formal, field-level specification of every byte Memtrace transmits from a customer machine. Intended for security, privacy, and audit functions reviewing whether Memtrace can run with telemetry enabled inside a regulated environment.

This document is the authoritative reference. Companion docs ([`PRIVACY.md`](../PRIVACY.md), [`TELEMETRY.md`](../TELEMETRY.md), [`docs/privacy-and-telemetry.md`](privacy-and-telemetry.md)) provide narrative summaries. Where any of those conflict with this datasheet, this datasheet is correct.

Version reference: Memtrace v0.4.62 (current at time of writing). The pipeline has been stable since v0.3.17, with PR workflow telemetry added in v0.4.62. Material changes are announced in the release notes and reflected in this file.

---

## 1. Executive summary

Memtrace's code graph runs on the customer's machine. Source code, file contents, embeddings, search queries, and symbol names are never transmitted as product data. The authenticated heartbeat carries only the account metadata and aggregate counters listed in §6.2. Product telemetry can include sanitised error and crash text; the path-handling limits are documented in §4.1.

The product makes three categories of network call:

1. **License validation + usage heartbeat** — required account metadata and aggregate usage only. No source content.
2. **Product telemetry** — on by default, can be disabled with one environment variable. Contains sanitised crash, error, and lightweight usage events. No source or query content.
3. **One-time model download** — inbound only, first run, from HuggingFace.

For a regulated environment, the recommendation is to keep product telemetry enabled unless the organisation's policy prohibits outbound diagnostic data. In that case, set `MEMTRACE_TELEMETRY=off`.

---

## 2. Data flow

```
Customer machine                                 Memtrace infrastructure
┌──────────────────────────┐                     ┌──────────────────────────┐
│  memtrace start runtime  │                     │                          │
│  ┌────────────────────┐  │   HTTPS (TLS 1.3)   │  api.memtrace.io         │
│  │ AST parser         │  │ ──────────────────▶ │  /api/device/auth        │
│  │ MemDB (local)      │  │   License + heart-  │  /api/device/heartbeat   │
│  │ Embedding (ONNX)   │  │   beat              │  /api/telemetry/ingest   │
│  │ Reranker           │  │                     │                          │
│  │ Sanitiser          │  │                     │  3 Postgres tables       │
│  │ Telemetry queue    │  │                     │  Admin dashboard         │
│  └────────────────────┘  │                     │  (@syncable.dev only)    │
│         │                │                     │                          │
│         ▼                │                     │                          │
│  ~/.memtrace/            │                     └──────────────────────────┘
│    telemetry/queue.jsonl │
│    embed-cache/          │
│    credentials.json      │
│  <project>/.memdb/       │
└──────────────────────────┘
```

All AST parsing, embedding, ranking, and graph storage runs locally. The customer machine's only outbound calls are to `*.memtrace.io` for the three endpoints above (plus a one-time inbound model download from HuggingFace).

---

## 3. Data classification matrix

The following is the complete inventory of fields that leave a customer machine via telemetry. Every field is enumerated; nothing is collected outside this list.

### 3.1 Identity fields (present in every telemetry payload)

| Field | Type | Source | Example | Customer-content? |
|---|---|---|---|---|
| `device_id` | string (UUID) | Generated locally at first run, stored in `~/.memtrace/credentials.json` | `a1b2c3d4-e5f6-...` | No |
| `version` | string | Compiled into the binary | `0.3.89` | No |
| `target` | string | Compile-time platform triple | `aarch64-apple-darwin` | No |
| `os` | string | Runtime detection | `macos-aarch64` | No |
| `tier` | string | Runtime resource detection | `standard` / `light` / `heavy` | No |

`device_id` is not reversible to the machine's hardware identity, hostname, IP, or user account. It is a randomly generated UUID stored in the credentials file. Deleting `~/.memtrace/credentials.json` and re-authenticating issues a new one.

### 3.2 Usage events (`telemetry_events` table)

One row per discrete signal the binary emits. The complete list of event types:

| Event name | When it fires | Fields beyond §3.1 identity |
|---|---|---|
| `start` | Every `memtrace start` / `memtrace mcp` boot that establishes a license session | `subcommand` (string, e.g. `start`, `mcp`), `transport` (string, e.g. `stdio`, `streamable-http`; `mcp` path only) |
| `start_degraded` | A boot that proceeded without a license session (expired install token, network failure, headless host) | `subcommand` (string), `transport` (string, `mcp` path only), `reason_class` (string enum from a fixed table, e.g. `session_expired`, `enterprise_enrollment`, `offline_license_expired` — the auth error is classified on the client and its display message discarded; see §3.2.1) |
| `mcp_call` | Every MCP tool call, on both the success and error paths | `tool` (string — the **name of the Memtrace tool invoked**, from a fixed Memtrace-owned set, e.g. `find_code`, `get_impact`), `status` (string enum: `ok` / `degraded` / `error`), `rid_count` (integer — number of results returned, never their identifiers), `tone` (string enum: `cyan` / `magenta` / `lime` / `amber` / `slate`, the tool's function-class), `duration_ms` (integer) |
| `rest_tool_call` | Every tool call served over the local REST/HTTP tool surface | Same fields as `mcp_call` |
| `pr_review_completed` | After `memtrace code-review` completes a GitHub PR review run | `posted` (boolean), `watch` (boolean), `comment_count` (integer), `finding_count` (integer), `graph_mode` (string, e.g. `strict`/`off`), `review_mode` (string), `min_severity` (string), `severity_counts` (JSON object with `low`/`medium`/`high`/`critical` integer buckets), `source_counts` (JSON object of numeric source buckets only) |
| `code_review_completed` | After a local `memtrace code-review` run with no PR post (diff file, stdin, or git range) | `input_kind` (string enum: `github_pr` / `diff` / `git_range`), `posted` (boolean, always `false`), `watch` (boolean, always `false`), plus the same count and mode fields as `pr_review_completed` |
| `pr_watch_registered` | When `memtrace code-review --post --watch` registers a local PR watch | `comment_count` (integer), `graph_mode` (string), `status` (string enum, initially `awaiting_response`) |
| `pr_watch_synced` | When watched PRs are polled by `memtrace start`, `memtrace mcp`, or `memtrace pr sync` | `watch_count`, `changed_count`, `awaiting_response_count`, `human_replied_count`, `approved_count`, `changes_requested_count`, `stale_after_push_count`, `merged_count`, `closed_count`, `poll_error_count` (all integers) |
| `pr_watch_poll_error` | The first time polling one watched PR fails, and again when the failure kind changes | `error_kind` (string enum: `rate_limited`, `token`, `github`, `parse`, `unknown`) |
| `drift_fallback` | When the incremental indexing path falls back to a full re-index | `site` (string enum: `live_pull` / `live_save` / `startup_drift` / `startup_reconcile` / `hosted_push`), `reason_class` (string enum from a fixed table, e.g. `dirty_tree`, `cold_start`, `oversize_commit_range` — the raw reason string is classified on the client and discarded), `changed_files` (integer, optional), `propagation_count` / `propagation_cap` (integers, optional) |

`duration_ms` is populated only for `mcp_call` and `rest_tool_call`; all other event types record it as null.

No event payload contains file paths, symbol names, repository names, PR URLs, owner names, branch names, commit hashes, reviewer identities, comment bodies, discussion text, query content, or any other customer-derived data. Counts are integers only, except for low-cardinality mode/status/error enums and the `tool` field described below.

**Tool names are collected.** The `tool` field on `mcp_call` and `rest_tool_call` records which Memtrace tool was invoked. Its value space is a fixed, finite set of Memtrace-owned identifiers compiled into the binary — it is not derived from customer code, repositories, or queries. It does constitute feature-usage data about the operator: it reveals *which* Memtrace capabilities an install exercises and how frequently. It does not reveal tool arguments, tool results, or the identity of any code the tool operated on.

#### 3.2.1 Sanitiser scope for usage events

The sanitiser in §4 applies to error rows (§3.3) and crash rows (§3.4). Usage-event properties are not passed through it, because they are constructed field-by-field from enums, booleans, and integers rather than from free-form strings — there is no free text to strip.

This holds for every usage-event field without exception. It is a structural property of the emit sites, not a claim about their current contents: each property is either a literal, a counter, or a value from a closed enum compiled into the binary.

The one historical exception was `start_degraded.reason`, which carried the display string of the underlying license/auth error verbatim. Some of those strings interpolate text the binary does not control — notably the absolute path of the offline license file and the text of an underlying io error. It has been replaced by `reason_class`, a value from a fixed table (`no_credentials`, `session_expired`, `invalid_license_key`, `enterprise_enrollment`, `external_memdb_denied`, and ten `offline_license_*` classes). Classification happens on the client by matching the error's **type variant** rather than its rendered message, so no part of the original string is transmitted, and a newly added failure mode fails to compile until it is assigned a class.

PR watch state is persisted locally at `~/.memtrace/pr-watches.json` so the local daemon can poll GitHub for the PRs Memtrace reviewed. That local file may contain PR coordinates and the local repo root. It is not uploaded through telemetry.

### 3.3 Error events (`telemetry_errors` table)

The binary uses the `tracing` crate for internal logging. `WARN`- and `ERROR`-level log lines emitted by Memtrace's own crates are mirrored to the telemetry queue after passing through a sanitiser (see §4).

Schema:

| Field | Type | Notes |
|---|---|---|
| Identity fields (§3.1) | — | |
| `level` | string | `WARN` or `ERROR` |
| `target` | string | Tracing target — Memtrace crate name (e.g. `memtrace_mcp::search`) |
| `message` | string | Sanitised log message, max 8 KB |
| `fingerprint` | string | `sha256(version ‖ target ‖ level ‖ first 6 tokens of message)` |
| `occurrences` | integer | Count of identical fingerprints; one row per fingerprint, occurrences bumped |
| `first_seen_at`, `last_seen_at` | timestamp | When this fingerprint first / most recently appeared |

The fingerprint mechanism means a recurring error becomes one row, not thousands. The maximum cardinality of error rows from one machine over the product's lifetime is bounded by the number of distinct error fingerprints in the binary — typically dozens, not millions.

### 3.4 Crash reports (`telemetry_crashes` table)

If the binary panics, the panic hook captures:

| Field | Type | Notes |
|---|---|---|
| Identity fields (§3.1) | — | |
| `panic_message` | string | Sanitised panic message |
| `location` | string | `file:line` from the Rust crate (e.g. `crates/memtrace-mcp/src/server.rs:142`) |
| `backtrace` | string | Sanitised Rust backtrace, capped at 16 KB |
| `occurred_at` | timestamp | When the panic happened locally |

The location field is the crate file path *inside the Memtrace binary*, not a customer file path. The backtrace is the Rust call stack of the binary's own code.

Crash reports are written synchronously to `~/.memtrace/telemetry/queue.jsonl` inside the panic hook, so a hard crash that exits the process still leaves a breadcrumb. They flush to the ingestion endpoint on the next successful binary run.

### 3.5 Rail shadow records (`rail_shadow` table)

Memtrace Rail is an optional router that can intercept code-discovery searches (grep/ripgrep/find for source symbols) and answer them from the Memtrace graph. When Rail is active it records a **content-free** measurement of what it *would* have returned — the dataset used to decide whether Rail is reliable enough to enable by default. No part of the search query, the matched files, or any result content is captured.

| Field | Type | Notes |
|---|---|---|
| Identity fields (§3.1) | — | `device_id`, `version`, `os` — same as other streams |
| `mode` | enum | `observe` / `nudge` / `rail` / `strict` |
| `surface` | enum | always `memtrace_owned` (a source-symbol search in an indexed repo) |
| `would_route` | bool | whether Rail would route this search to Memtrace |
| `shape` | enum | `identifier` / `alternation` / `phrase` / `regex` / `empty` — the **shape** of the search pattern, derived locally; never the pattern text |
| `retrieval` | enum | `hit` / `miss` / `unavailable` — did Memtrace return a confident result |
| `score_bucket` | enum | `lt10` / `b10` / `b25` / `gte50` — bucketed relevance score, never the raw float |
| `relevance_proxy` | bool | computed **on-device**: did the top result's name/path contain a token from the search? Only the boolean is transmitted; the strings are compared locally and discarded |
| `latency_bucket` | enum | `fast` (<100 ms) / `mid` / `slow` |
| `occurred_at` | timestamp | When the search happened locally |

**Conditions for emission.** Produced by default in `observe` mode (every install), one row per Memtrace-owned code search. It is measured **asynchronously, off the user's critical path**: the search hook records a request to a local spool and returns immediately — it issues no query — and the long-running daemon performs the retrieval in the background. The search therefore incurs **no added latency**. Enforcing modes (`memtrace rail enable nudge|rail|strict`) additionally measure inline. Opt-out: `MEMTRACE_TELEMETRY=off` (all telemetry) or `MEMTRACE_RAIL_SHADOW=off` (Rail only); `MEMTRACE_RAIL_SHADOW_SAMPLE` (0–1) bounds the background sampling rate. Records do not pass through the §4 text sanitiser because they contain no free-text fields — only enums, buckets, and booleans.

---

## 4. Sanitisation pipeline

Before any error message, panic message, or backtrace is written to the local queue, it passes through a sanitiser implemented in the binary. The sanitiser performs three transformations:

| Transformation | Pattern | Replacement |
|---|---|---|
| Home-directory paths | Any absolute path under the OS-detected `$HOME` (or its Windows equivalent) | `~` |
| Token-shaped strings | Regex `[A-Za-z0-9_+/=-]{40,}` (matches API tokens, session tokens, JWTs, GitHub PATs, base64-encoded secrets) | `<redacted-token>` |
| Email addresses | RFC 5322 simplified regex | `<redacted-email>` |

Sanitisation is applied **before** the content fingerprint is computed, so two errors that differ only in their redacted content collapse to the same fingerprint.

The sanitiser source of truth is the public repo at [`crates/memtrace-mcp/src/telemetry.rs`](https://github.com/syncable-dev/memtrace-public/blob/main/crates/memtrace-mcp/src/telemetry.rs) — there are no closed-source telemetry paths.

### 4.1 What the sanitiser is *not* designed to do

Customers operating in regulated environments should understand the limits:

- **The sanitiser does not strip arbitrary file names below `$HOME`.** A panic backtrace that includes `~/clients/acme-corp/audit-2026/main.py` would emit `~/clients/acme-corp/audit-2026/main.py` — the home-directory prefix collapses, but the directory structure below it does not.
- **The sanitiser does not classify content semantically.** It uses regex patterns. A panic that happened to log a customer name, a project codename, or a non-token-shaped secret would not be redacted.
- **The sanitiser does not parse JSON / structured payloads.** It runs against the log line as a string.

In practice, Memtrace's own crates do not log customer content at `WARN`/`ERROR` level — these levels are reserved for indexer / runtime / network errors. But the sanitiser is a defence-in-depth measure, not a guarantee that no customer-derived path or identifier could ever appear in a sanitised payload.

If your data classification policy treats *any* path component below `$HOME` as sensitive (for example, KPMG client codenames in directory names), set `MEMTRACE_TELEMETRY=off` and rely on local error inspection.

---

## 5. What is explicitly never collected by product telemetry (§3)

The telemetry pipeline schema on the receiving end has no column for the following — collecting them would require a new product release. None of these ever leave the customer machine via the product-telemetry endpoint:

- Source code or file contents
- Symbol names extracted from customer code
- Embeddings, BM25 indices, or any derived data
- Repository names, paths, or remote URLs
- GitHub PR URLs, pull request discussion text, issue/review/comment bodies, or reviewer identities
- Branch names, commit messages, commit hashes, or git history
- Search queries (`find_code` query strings)
- File paths pointing inside the indexed repository, except where they appear in a sanitised crash backtrace (§4.1)
- Environment variable values (the sanitiser strips token-shaped strings; the binary does not read environment values directly into telemetry payloads)
- IP addresses on the server side (standard request logs are retained for 7 days for abuse mitigation only and are not joined to telemetry tables)

For the avoidance of doubt, the exclusion of search queries above covers the query *string* only. The fact that a tool ran — its name, status, duration, and result count — is recorded as a usage event (`mcp_call` / `rest_tool_call`, §3.2). An install's telemetry therefore shows that `find_code` was invoked 40 times and returned results, but not what was searched for or what matched.

---

## 6. Required network calls (non-telemetry)

For completeness, three non-telemetry call types exist. None carries source code or query content.

### 6.1 License authentication

| | |
|---|---|
| Endpoint | `POST https://www.memtrace.io/api/device/auth` |
| Transport | HTTPS, TLS 1.3 |
| Payload | License key (`MTC-COM-…`), machine hostname (used only as a human-readable label in the licensing dashboard) |
| Frequency | First run, then refresh near session-token expiry (typically every 24 hours) |
| Purpose | Validate the license, issue session token |
| Offline behaviour | 24-hour grace period before re-validation required |

### 6.2 Usage heartbeat

| | |
|---|---|
| Endpoint | `POST https://www.memtrace.io/api/device/heartbeat` |
| Transport | HTTPS, TLS 1.3 |
| Payload | Aggregate node, edge, and episode counts; billable query usage since the previous heartbeat; organization and seat identity for organization installs; MemDB endpoint, deployment mode, and an aggregate record count for MemDB deployments. No repository names, paths, symbol names, query text, or code. |
| Frequency | Every 15 minutes while the daemon is running |
| Purpose | Usage metering, entitlement checks |

### 6.3 Embedding model download (one-time, inbound)

| | |
|---|---|
| Source | HuggingFace Hub via the `fastembed` library |
| Direction | Inbound only — Memtrace downloads model weights; nothing about the customer machine is uploaded |
| Payload | ONNX model weights (typically `jina-embeddings-v2-base-code` or `bge-small-en-v1.5`) |
| Frequency | Once on first run, cached at `~/.cache/fastembed/` thereafter |
| Customer content sent | None |


---

## 7. Storage, retention, and access on the receiving end

| Property | Value |
|---|---|
| Operator | Syncable ApS (Denmark, EU) |
| Storage location | Memtrace-operated PostgreSQL on `*.memtrace.io` infrastructure |
| Tables | `telemetry_events`, `telemetry_errors`, `telemetry_crashes`, `rail_shadow` (content-free Rail routing-quality buckets; emitted only when Rail is enabled) |
| Schema | [`memtrace-ui/drizzle/0002_telemetry.sql`](https://github.com/syncable-dev/memtrace-public) (closed-source repo; available under NDA for compliance review) |
| Retention | No automatic purge today. Retention policy of 90 days is committed before the dataset exceeds 90 days of history. Material changes announced in release notes. |
| Access | Admin analytics dashboard at `https://memtrace.io/admin/analytics`, gated to `@syncable.dev` email accounts via authenticated session. No third-party access. |
| Third parties | None. The pipeline ships no data to third-party analytics SDKs (Segment, Mixpanel, Datadog, etc.). The binary contains no embedded third-party telemetry library. |
| Sale or sharing | Telemetry data is not sold, shared, or published in anonymised aggregate form without prior notice in the release notes. |

### 7.1 Right of erasure

Customers may request erasure of all telemetry data associated with their `device_id` by emailing `support@syncable.dev` with the device ID (visible in `~/.memtrace/credentials.json`). Erasure is processed within 30 days and confirmed by email.

### 7.2 Breach notification

In the event of a confirmed compromise of the telemetry storage layer, affected customers will be notified within 72 hours of confirmation, via the email associated with their license key, with details of the impact and recommended actions.

---

## 8. Network egress allowlist (for organisational firewalls)

If outbound traffic from developer machines is filtered, the following destinations must be allowed for Memtrace to function:

| Destination | Required for | Direction |
|---|---|---|
| `*.memtrace.io` (HTTPS / TCP 443) | License validation + heartbeat + telemetry | Outbound |
| `huggingface.co`, `cdn-lfs*.huggingface.co` (HTTPS / TCP 443) | One-time model download | Outbound (inbound payload) |
| `registry.npmjs.org` (HTTPS / TCP 443) | Only required when running `memtrace install` to upgrade | Outbound |

Blocking `huggingface.co` after first run is safe — the model is cached. Blocking `registry.npmjs.org` only prevents upgrades. Blocking `*.memtrace.io` puts the binary into offline-grace mode for 24 hours before license re-validation is required.

To disable telemetry traffic specifically while keeping license validation, set `MEMTRACE_TELEMETRY=off` — license calls continue, the telemetry queue stops flushing.

---

## 9. Customer-side verification

The customer can verify locally exactly what is in the telemetry queue before it ships:

```bash
# Inspect the on-disk queue
cat ~/.memtrace/telemetry/queue.jsonl

# Each line is one record; the "kind" field is "event" | "error" | "crash"
# There is no separate raw buffer — the file shown above is the complete record.
```

To run Memtrace and accumulate a queue *without* shipping it (for compliance review):

1. Set `MEMTRACE_TELEMETRY=off` in the environment.
2. Run Memtrace normally — the queue is **not** written when telemetry is off.

If your goal is to inspect what *would* have been queued, run a normal session with telemetry on, then immediately read the JSONL file before the flusher's 60-second batched flush.

---

## 10. Opt-out procedure

### 10.1 Per-process

```bash
MEMTRACE_TELEMETRY=off memtrace start
```

Accepted off-values: `off`, `0`, `false`, `disabled`, `no` (case-insensitive). Anything else, including unset, keeps telemetry enabled.

When set, the binary's behaviour is:

- The panic hook still installs locally (so a crash in a disabled session still leaves a `~/.memtrace/telemetry/queue.jsonl` breadcrumb), but the flusher never ships it.
- The `tracing` layer becomes a no-op for telemetry — `WARN`/`ERROR` lines are still printed to stderr but are not queued.
- The flusher exits immediately on startup — no network calls are made to the telemetry endpoint.
- Usage event callsites short-circuit before any data is constructed.

### 10.2 Permanent — shell profile

```bash
# ~/.zshrc / ~/.bashrc
export MEMTRACE_TELEMETRY=off
```

### 10.3 Permanent — MCP client configuration

```json
{
  "command": "memtrace",
  "args": ["mcp"],
  "env": { "MEMTRACE_TELEMETRY": "off" }
}
```

Applies to Claude Code, Cursor, Codex, Windsurf, and any MCP client that honours the `env` block.

### 10.4 The hard-override variable

A second environment variable, `MEMTRACE_TELEMETRY_DISABLED=1`, is documented in [`docs/environment-variables.md`](environment-variables.md#telemetry--auth) as a hard override that blocks telemetry regardless of any other state. For most users `MEMTRACE_TELEMETRY=off` is sufficient; the hard override is recommended for CI / locked-down environments where the higher-precedence variable should be unambiguous.

### 10.5 Verification that opt-out is active

The on-disk queue is the verification surface. After running Memtrace with `MEMTRACE_TELEMETRY=off`:

```bash
ls -la ~/.memtrace/telemetry/   # directory empty or absent
```

If the queue file is present and growing, telemetry is still on. If it's empty or missing, the kill switch took effect.

---

## 11. Recommended configuration for regulated environments

For an audit, financial-services, healthcare, or other regulated context, the following configuration is appropriate **with product telemetry enabled**:

- Keep `MEMTRACE_TELEMETRY` at its default (on) and use the queue inspection procedure (§9) periodically to confirm no client-identifying paths appear in errors.
- If the engagement involves hostnames that encode client identity, override the licensing hostname label so it doesn't surface in your account dashboard.
- Add `*.memtrace.io` and `huggingface.co` to the egress allowlist.
- Retain a copy of this datasheet and the linked source files in the project's compliance record.

If the organisation's policy prohibits *any* outbound diagnostic data regardless of content classification, set `MEMTRACE_TELEMETRY=off` permanently. The product remains fully functional in that mode. License validation and the authenticated heartbeat continue to run because they are account operations, not product telemetry.

---

## 12. Change management

Any change to:

- The list of fields collected
- The sanitisation pipeline
- The storage location or operator
- Retention or access policy

will be announced in:

1. The release notes of the version that introduces the change.
2. This datasheet (with an entry in §13 below).
3. The companion `PRIVACY.md` and `TELEMETRY.md` files.

Customers who require a notice period before a material telemetry change reaches their environment should pin to a specific Memtrace version (`npm install -g memtrace@0.3.89`) and review release notes before upgrading.

---

## 13. Changelog

| Version | Date | Change |
|---|---|---|
| v0.3.17 | 2025-09 | Telemetry pipeline introduced (events, errors, crashes). Sanitiser shipped with launch. Default on, env-var opt-out. |
| v0.3.89 | 2026-05 | This datasheet published. No change to telemetry behaviour. |

---

## 14. Contacts

| Topic | Contact |
|---|---|
| Compliance / DPA / SOC 2 questionnaire | `support@syncable.dev` |
| Security disclosures | `support@syncable.dev` (PGP key on request) |
| General support | `support@syncable.dev` |
| Public issue tracker | [github.com/syncable-dev/memtrace-public/issues](https://github.com/syncable-dev/memtrace-public/issues) |

A formal Data Processing Agreement (DPA), GDPR Article 30 record-of-processing-activities entry, and SOC 2 readiness questionnaire are available on request via `support@syncable.dev`.
