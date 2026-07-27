# Environment variables reference

Every env var Memtrace reads, what it does, and the default. Grouped
by what you'd reach for them for. Most users never set any of these —
the defaults auto-tune to your machine.

## Quick index

- [Transport + ports](#transport--ports)
- [On-disk locations](#on-disk-locations)
- [Embedding pipeline](#embedding-pipeline)
- [Reranker](#reranker)
- [Search / retrieval tuning](#search--retrieval-tuning)
- [Resource caps](#resource-caps)
- [Install / uninstall lifecycle (v0.3.89)](#install--uninstall-lifecycle-v0389)
- [Watch persistence (v0.3.89)](#watch-persistence-v0389)
- [Headless / browser (v0.6.10)](#headless--browser-v0610)
- [Legacy daemon lifecycle (removed v0.6.10)](#legacy-daemon-lifecycle-removed-v0610)
- [MemDB connection (advanced)](#memdb-connection-advanced)
- [Telemetry + auth](#telemetry--auth)

## Transport + ports

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_TRANSPORT` | `stdio` | How `memtrace mcp` talks to its agent. See [`mcp-and-transports.md`](mcp-and-transports.md). Values: `stdio`, `streamable-http`, `sse` (alias for streamable-http), `http` (alias for streamable-http). Anything else is rejected with a clear error since v0.3.32. |
| `MEMTRACE_PORT` | `3000` | When transport is HTTP, the port `memtrace mcp` binds. |
| `MEMTRACE_UI_PORT` | `3030` | Local HTTP API + dashboard port while `memtrace start` (or an attached owner) is running. |
| `MEMTRACE_UI_HOST` | `127.0.0.1` | Bind address for the HTTP API. Set to `0.0.0.0` only when you intentionally need remote/VM/container exposure. |
| `MEMTRACE_HEADLESS` | (unset) | Set to `1` to skip auto-opening a browser tab on `memtrace start`. The API stays bound on `MEMTRACE_UI_PORT`. Same as `--headless`. |
| `MEMTRACE_NO_UI` | (unset) | **Deprecated alias** for `MEMTRACE_HEADLESS` (skips browser only — does not disable the HTTP API). |
| `MEMTRACE_NO_BROWSER` | (unset) | **Deprecated alias** for `MEMTRACE_HEADLESS`. Prefer `MEMTRACE_HEADLESS=1` or `memtrace start --headless`. |
| `MEMTRACE_WS_PORT` | `3031` | Internal WebSocket bus that pushes index events to the UI. Don't change unless 3031 is taken. |

## On-disk locations

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_DATA_DIR` | `<cwd>/.memtrace` | Per-project indexer state. Override to put it elsewhere. |
| `MEMTRACE_MEMDB_DATA_DIR` | `<repo-root>/.memdb` | The MemDB graph data dir. Anchored to the git root if there is one, otherwise the cwd. Override with an absolute path for shared / CI setups. |
| `FASTEMBED_CACHE_DIR` | `~/.memtrace/fastembed_cache` | Where downloaded embedding models live. |
| `MEMTRACE_DEFAULT_REPO` | (unset) | If set, every tool call without a `repo_id` argument uses this. Convenient for single-repo workflows. |
| `MEMTRACE_SESSION_LEDGER` | `~/.memtrace/session-ledger.jsonl` | **(v0.3.89)** Override the JSONL value ledger path. The default is user-global so `memtrace mcp` (agent-launched, any cwd) and `memtrace start` (run from your repo) write to / read from the same file regardless of where each process started. Set to an absolute path if you want per-workspace isolation. |

## Embedding pipeline

> **New CLI surface** — most of the knobs below now have a friendlier
> wrapper: `memtrace embed set <preset>` or
> `memtrace embed set --remote …` writes a persistent config so you
> don't have to re-export env vars on every shell. See
> [`embedding-providers.md`](embedding-providers.md) for the full
> command tour. The env vars listed here still work as a transient
> escape hatch — useful in CI — but the CLI is the recommended path
> for everyone else.

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_EMBED_MODEL` | `jina-code` (jinaai/jina-embeddings-v2-base-code, 768d) | Model to use. Other accepted values: `bge-small` (384d, ~140 MB — legacy rollback), `bge-base` (768d, ~440 MB), `nomic` (768d). |
| `MEMTRACE_EMBED_QUANT` | auto-picked: `int8` on Apple Silicon (any tier), `fp32` on Heavy tier elsewhere, `int8` on Standard / Light | Embedding quantisation. Force with `int8` or `fp32`. **(v0.3.83)** Apple Silicon now defaults to `int8` for every tier — restores pre-f0fcf221 performance characteristics on M-Max / M-Ultra hosts where fp32 fell back to the slow CoreML CPU path (the Apple Neural Engine only accelerates `int8`). Set `MEMTRACE_EMBED_QUANT=fp32` to opt back into fp32 on Apple Silicon (e.g. for a recall-vs-speed benchmark). Workstation Linux / Windows on Heavy tier are unchanged — CUDA / DirectML still accelerates fp32 there. |
| `MEMTRACE_VECTOR_DIMS` | model-derived (e.g. `768` for `jina-code`, `384` for `bge-small`, `1024` for `voyage-code-3`) | Vector dimensionality of the HNSW index. When unset, derived from the active model so `MEMTRACE_EMBED_MODEL=bge-small memtrace index .` now works on a fresh install without also setting this var. Explicit values still override. Must match the active model's output dim — switching models with a mismatch raises a clear error pointing at `memtrace embed set`. |
| `MEMTRACE_EMBED_INTRA_OP_THREADS` | tier-aware (1 / 2 / 4 for Light / Standard / Heavy) | Cap on ORT intra-op threads. Single biggest lever for memory on tight machines. |
| `MEMTRACE_EMBED_BATCH_SIZE` | tier-aware (8 / 16 / 64 for Light / Standard / Heavy) | Per-batch size handed to the embedder. Memory scales linearly with this. Smaller batches finish faster per call, which matters on slow CPU paths (see `MEMTRACE_EMBED_BATCH_TIMEOUT_SECS`). |
| `MEMTRACE_EMBED_BATCH_TIMEOUT_SECS` | `60` | Per-batch wall-clock ceiling. When exceeded, the embedding worker is abandoned and respawned on the next call (the bootstrap is self-healing — it just retries). On slow CPU paths (pre-AVX2 hosts: Intel Ivy Bridge / Xeon E5 v2 and older, AMD pre-Excavator) bump to `240` or higher; you'll see the warning `"Embedding batch timed out after 60s …"` if you need it. |
| `MEMTRACE_EMBED_TIMEOUT_DEBUG` | (unset) | Set to `1` to log the offending input previews when a batch times out. Useful for diagnosing whether a single very long symbol body is dragging an otherwise fast batch over the limit. Off by default — these logs include source snippets. |
| `MEMTRACE_EMBED_RSS_LIMIT_GB` | tier-aware (3 / 6 / 10 / 20 GB) | Soft RSS ceiling on the embed process that triggers back-pressure when exceeded. Set to `0` to disable the check. |
| `MEMTRACE_EMBED_PRESSURE` | `warn` | **(v0.3.82)** System-pressure gate threshold for **local** embedding. Values: `off` (no gating), `normal` (block on any pressure), `warn` (block on Warn/Critical), `critical` (block only on Critical). The probe reads system-wide free + compressor + swap-rate (Mach `host_statistics64` on macOS, `/proc/meminfo` on Linux, `GlobalMemoryStatusEx` on Windows). Sustained `Critical` pressure for 60 s consecutive trips a circuit breaker; daemon exits 75 with a clear banner instead of hanging silently. **Remote providers are unaffected** — when you've configured a remote embedder (OpenAI / Voyage / Ollama etc. via `memtrace embed set --remote …`), the gate is bypassed because the inference happens off-host and local pressure is irrelevant. |
| `MEMTRACE_EMBED_MIN_LINES` | `4` | Don't embed symbols with fewer body lines than this. Prevents wasting cache on trivial helpers. |
| `MEMTRACE_LONGFN_CHUNK_THRESHOLD` | `80` | **(v0.3.82)** Functions with more body lines than this are embedded as overlapping sub-spans rather than one blob. Improves recall on natural-language queries that match content deep inside long functions. Set to a very large number (e.g. `100000`) to disable. |
| `MEMTRACE_LONGFN_CHUNK_SIZE` | `60` | Sub-span size when chunking. |
| `MEMTRACE_LONGFN_CHUNK_OVERLAP` | `20` | Sub-span overlap when chunking. |
| `MEMTRACE_FIELD_BOOST_BODY_STRINGS` | `0.5` | **(v0.3.82)** BM25 weight on the new `body_strings` field (function-body string literals extracted at index time). Improves recall on natural-language → log-line queries. |
| `MEMTRACE_DISABLE_COREML` | (unset) | Set to `1` on Apple Silicon to force CPU execution provider instead of CoreML / ANE. Useful if CoreML's first-run graph compile hangs on your machine. |
| `MEMTRACE_TIER` | auto-detected (`light` / `standard` / `heavy`) | Force the host tier instead of letting Memtrace pick from RAM + CPU + accelerator signals. |
| `MEMTRACE_SKIP_EMBED` | (unset) | Set to `1` to disable **all** embedding (index, watcher, semantic search). Structural graph tools still work. |
| `MEMTRACE_SKIP_WATCHER_EMBED` | (unset) | Set to `1` to disable per-save watcher embedding only; index-time embedding still runs. |

## Reranker

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_RERANK` | `on` | Enable / disable cross-encoder rerank in `find_code`. Set to `off` for a pure BM25 + vector pipeline (~3–4 pp lower acc@1 but ~400 ms faster per query). |

## Search / retrieval tuning

You typically don't need these. They exist for benchmarking and for
edge cases where you want to bias the ranking yourself.

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_RRF_K` | `60` | Reciprocal Rank Fusion constant. Smaller = more aggressive top-K; larger = flatter. |
| `MEMTRACE_RRF_BM25_WEIGHT` | `2.0` | Weight on the BM25 leg in RRF. |
| `MEMTRACE_RRF_VECTOR_WEIGHT` | `1.0` | Weight on the vector leg. |
| `MEMTRACE_RRF_GRAPH_WEIGHT` | `0.75` | Weight on the graph signal. |
| `MEMTRACE_RRF_EXACT_WEIGHT` | tuned default | Weight applied to exact-name matches before fusion. |

## Resource caps

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_MAX_THREADS` | `n-2` for `memtrace index`, `n/2` for `memtrace start` | Rayon parse-thread pool size. Useful to leave cores free for your editor. |
| `MEMTRACE_RAM_TIER` | `on` | Keep the in-memory property index split into a recent hot tier and a cold tier. Set `off` (also `0`, `false`, or `no`) only if you need instant deep time-travel reads and have the RAM for it. Anything else, including an unset value, leaves tiering on. |
| `MEMTRACE_HOT_HORIZON_DAYS` | `15` | How many recent days stay in the hot property-index tier when `MEMTRACE_RAM_TIER` is on. Must be a positive whole number; empty, zero, and invalid values fall back to `15`. |
| `MEMTRACE_UNIFIED_CACHE_MB` | `256` | **(v0.4.60)** Single in-RAM hot-cache budget shared across the embed-vector and backend page-cache layers (moka W-TinyLFU eviction). Defaulting to 256 MB caps the daemon's discretionary RAM at a known number; raise to `512` / `1024` on hosts with RAM headroom for a higher hit rate. Set `0` to disable the cache entirely. Replaces a set of per-subsystem caches that compounded silently. |
| `MEMTRACE_ORT_LOW_RSS` | `0` | **(v0.4.60)** Opt-in ORT memory-arena toggle. When `1`, every `ort::Session::builder()` site (rerank, splade, embedder) calls `.with_memory_pattern(false)`, which releases CPU activation arena between batches. Cites [microsoft/onnxruntime#11627](https://github.com/microsoft/onnxruntime/issues/11627). Empirical impact on our shipping models: **−2.7% peak RSS for −19% throughput** on the concurrent rerank+embed workload — default OFF because that trade is bad on our model sizes (34 MB int8 reranker + 140 MB int8 embedder). Turn ON only if you're running much smaller custom models where the arena dwarfs the model. |

## Workspace daemon (v0.3.82)

The `--workspace` daemon now auto-watches `.git/refs/heads/<active_branch>` for new commits in addition to source files. A fresh `git commit` is picked up within 5 s without any explicit ping.

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_GIT_REF_WATCH` | `on` (for `--workspace`), `off` otherwise | Enable / disable the git-ref watcher. |
| `MEMTRACE_GIT_REF_WATCH_DEBOUNCE_MS` | `2000` | Debounce window for rapid ref changes (interactive rebase = many ref updates collapse to one delta sync after this settles). |

Branch switch (`git checkout other-branch`) re-targets the watcher to the new active ref without restart. Detached-HEAD doesn't crash — the watcher logs once and falls back to the file-save trigger.

## Pre-commit hook (v0.3.82 — now opt-in)

⚠️ **Breaking from prior versions:** `memtrace install` no longer auto-installs the pre-commit hook. To opt in: `memtrace install-hooks --pre-commit`. To remove an existing one: `memtrace uninstall-hooks`.

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_PRECOMMIT` | (unset) | Set to `off` / `0` / `false` / `no` / `disabled` for a silent no-op (kill switch checked in bash before the binary spawns, so even a broken binary can't block a commit). |
| `MEMTRACE_PRECOMMIT_MODE` | `blocking` | `blocking` (default — sync, 1.5 s capped) OR `agent` (fire-and-forget — forks daemon-ping detached and exits in ~15 ms). For agentic CI / Orbit-style pipelines, set to `agent`. |
| `MEMTRACE_PRECOMMIT_TIMEOUT_MS` | `1500` | Hard wall-clock cap on the blocking-mode hook (hook also wraps in `\|\| true` so it never blocks the commit even on timeout). |
| `MEMTRACE_PRECOMMIT_MAX_RSS_MB` | `512` | RLIMIT_AS cap on the pre-commit binary (Linux enforced; macOS no-op per kernel — documented). Prevents OOM. Set to `0` to disable. |
| `MEMTRACE_PRECOMMIT_MAX_DIFF_BYTES` | `1048576` | If `git diff --cached` exceeds this, skip analysis (huge commits aren't worth the wait). Set to `0` to disable. |
| `MEMTRACE_PRECOMMIT_MAX_SYMBOLS` | `500` | If parsed-affected-symbols list exceeds this, truncate (don't try to render warnings for 5,000 symbols). Set to `0` to disable. |

## Claude Code hooks (v0.3.82)

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_HOOK_MODE` | `advisory` | Set to `off` for unconditional no-op of the UserPromptSubmit hook. |
| `MEMTRACE_HOOK_DEBOUNCE_SECS` | `120` | Per-session debounce window. After the hook fires once for a session, suppresses further fires within this window. Set to `0` to disable debounce (every message fires). |
| `MEMTRACE_HEALTH_URL` | `http://localhost:3030/api/health` | Where the Claude Code `UserPromptSubmit` hook probes runtime liveness. Override for non-default UI ports. |

Session ID resolution (used to key the lock file at `~/.memtrace/hook-debounce/<session_id>.lock`):
1. `CLAUDE_SESSION_ID` env (if Claude Code sets it)
2. `CLAUDE_CONVERSATION_ID` env
3. Fallback: SHA-1 of `PPID + parent process start-time`

## Pre-push fortress hook (v0.3.82)

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_PREPUSH` | (unset) | Set to `off` / `0` / `false` / `no` / `disabled` for kill switch on the pre-push fortress hook (installed via `memtrace install-hooks --pre-push`). |

## Install / uninstall lifecycle (v0.3.89)

After @Badmrpotatohead's report that `npm install -g memtrace@latest` was wiping `~/.memtrace/` on upgrade, the uninstall path now distinguishes "user asked to uninstall" from "npm is upgrading me". User data is preserved by default — `MEMTRACE_PURGE_DATA=1` is the explicit opt-in for a full wipe.

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_PURGE_DATA` | (unset) | Set to `1` to allow `npm uninstall -g memtrace` to remove `~/.memtrace/` (session ledger, embed cache, auth tokens, telemetry buffer, logs). Without this, uninstall preserves user data. Has no effect during upgrades — the lifecycle gate refuses to purge during install/update/upgrade even if this var is set. |
| `MEMTRACE_UNINSTALL_PURGE_DATA` | (unset) | Alias for the above. |
| `MEMTRACE_INSTALL_PARENT` | (set internally) | `bin/memtrace.js` sets this to `1` before spawning the npm self-upgrade child process so the inner uninstall lifecycle knows it's an upgrade, not an explicit uninstall. Agents shouldn't set this manually. |

## Watch persistence (v0.3.89)

After @Magalz's report that `watch_directory` registrations vanished on MCP disconnect, watches now persist to `~/.memtrace/watches.json` and are restored on every MCP boot.

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_NO_WATCH_RESTORE` | (unset) | Set to `1` to skip the watch-list restore on MCP startup. Useful if you have stale watch state you want forgotten — just unset it again on the next start to re-enable restore. |

The watch file is a JSON array of `{ path, repo_id, registered_at, origin }` objects; `origin` is `"manual"` for entries you registered explicitly via `watch_directory` and `"restored"` for entries that were re-armed from a prior session.

## Headless / browser (v0.6.10)

`memtrace start --headless` (or `MEMTRACE_HEADLESS=1`) keeps the HTTP API on `:3030` but skips auto-opening a browser tab. This is the recommended mode for Orbit, CI, and agent hosts that need health/search endpoints without a GUI session.

Legacy names `--no-ui`, `--no-browser`, `MEMTRACE_NO_UI`, and `MEMTRACE_NO_BROWSER` still work as aliases and print a one-time deprecation note.

## Legacy daemon lifecycle (removed v0.6.10)

`memtrace service` / `memtrace daemon install|start|status|stop` was removed in **0.6.10**. Use `memtrace start` (foreground or `--headless` in tmux/screen/nohup) or `memtrace mcp` for agent workflows. `memtrace stop` still unloads legacy launchd/systemd/Windows service registrations if an older install left them behind.

The following env vars are **obsolete** and ignored by current binaries:

| Var | Was used for |
|---|---|
| `MEMTRACE_DAEMON_HEALTH_TIMEOUT_MS` | `memtrace daemon start` health poll (removed) |
| `MEMTRACE_DAEMON_HEALTH_INTERVAL_MS` | `memtrace daemon start` health poll interval (removed) |
| `MEMTRACE_LOGS_DIR` | Override for `~/.memtrace/logs/daemon.log` (file logging removed with OS service install) |

## MemDB connection (advanced)

You almost certainly don't touch these. They exist for people running
Memtrace against a remote MemDB cluster instead of the embedded
default.

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_MEMDB_MODE` | `embedded` (auto-detected from `MEMDB_ENDPOINT` if set) | `embedded` or `remote`. |
| `MEMTRACE_MEMDB_ENDPOINT` | `http://127.0.0.1:50051` | Remote MemDB gRPC endpoint. Setting this to anything other than the loopback default flips mode to `remote`. |
| `MEMTRACE_MEMDB_LOOPBACK_PORT` | `50051` | When `memtrace start` runs in embedded mode, the loopback gRPC port the in-process MemDB binds. Other processes (`memtrace mcp`, MemFleet broker) attach here. Set to `0` for ephemeral. |
| `MEMTRACE_MEMDB_DB` | `memtrace` | The database name within MemDB. |
| `MEMTRACE_MEMDB_AUTH_TOKEN` | (unset) | Bearer token for `remote` mode. Ignored for `embedded`. |

## Telemetry + auth

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_TELEMETRY` | (unset, treated as on) | The user-facing kill switch. Set to `off` / `0` / `false` / `disabled` / `no` to disable product telemetry. See [`TELEMETRY.md`](../TELEMETRY.md) and [`privacy-and-telemetry.md`](privacy-and-telemetry.md#2-product-telemetry-on-by-default--opt-out). |
| `MEMTRACE_TELEMETRY_DISABLED` | (unset) | Hard override. Set to `1` to block telemetry unconditionally — takes precedence over `MEMTRACE_TELEMETRY` and any other state. Recommended for CI / locked-down environments. |
| `MEMTRACE_LICENSE_KEY` | (unset) | Optional bearer-style license key for non-interactive (CI / server) authentication. Most users authenticate via device flow on first run instead. |

## Redis / pub-sub (multi-process deployments)

Memtrace can broadcast indexing events to a Redis channel so
detached UIs and orchestrators can subscribe without polling.

| Var | Default | Purpose |
|---|---|---|
| `REDIS_URL` | (unset) | Where to publish `memtrace:indexed` events. Empty = no publish; in-process WebSocket is still used. |
| `VALKEY_URL` | (unset) | Same as `REDIS_URL`. Convenience alias if you're on Valkey. |

## Internal / undocumented

There are a few env vars used by tests and CI that aren't part of the
stable API and may change without notice. They're all prefixed
`MEMTRACE_TEST_*` or `RUST_LOG`. Don't depend on them in production.

## Examples

A 16 GB laptop running on battery, wanting maximum thrift:

```bash
export MEMTRACE_TIER=light
export MEMTRACE_RERANK=off
memtrace start
```

A 64 GB workstation building agent infra, wanting maximum speed and
recall:

```bash
export MEMTRACE_TIER=heavy
export MEMTRACE_TRANSPORT=streamable-http
export MEMTRACE_PORT=4848
memtrace start
memtrace mcp     # binds the HTTP server on :4848
```

A CI pipeline that needs deterministic behaviour:

```bash
export MEMTRACE_LICENSE_KEY=<your CI key>
export MEMTRACE_TELEMETRY_DISABLED=1
export MEMTRACE_DISABLE_COREML=1   # macOS CI runners
memtrace index .
```

A small Raspberry Pi 4 (4 GB) — tight RAM, no rerank:

```bash
export MEMTRACE_TIER=light
export MEMTRACE_EMBED_MODEL=bge-small        # smaller model, 384d
export MEMTRACE_VECTOR_DIMS=384
export MEMTRACE_RERANK=off
export MEMTRACE_EMBED_BATCH_SIZE=4
memtrace start
```

See [`performance-tuning.md`](performance-tuning.md) for more recipes.
