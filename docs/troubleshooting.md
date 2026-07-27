# Troubleshooting

Concrete fixes for the most common issues. Skim the table of contents,
find your symptom, follow the fix.

## Quick index

- [Install fails](#install-fails)
- [Runtime won't start](#runtime-wont-start)
- [Run doctor first](#run-doctor-first)
- [Claude Code hook JSON validation failed](#claude-code-hook-json-validation-failed)
- [Agent says "Memtrace index is empty"](#agent-says-memtrace-index-is-empty-0-nodes-0-edges-at-session-start)
- [Agent isn't using the MCP](#agent-isnt-using-the-mcp)
- [Cortex recall is missing, unavailable, or empty](#cortex-recall-is-missing-unavailable-or-empty)
- [Dashboard graph or timeline looks partial after cold start](#dashboard-graph-or-timeline-looks-partial-after-cold-start)
- [`MEMTRACE_TRANSPORT=sse` hangs](#memtrace_transportsse-hangs)
- [Indexing hangs / never finishes](#indexing-hangs--never-finishes)
- [Indexing pulls in too much](#indexing-pulls-in-too-much--generated-code-vendored-deps-fixtures)
- [Indexing eats all my RAM](#indexing-eats-all-my-ram)
- [Embedding warns about batch timeouts on slow CPUs](#embedding-warns-about-batch-timeouts-on-slow-cpus)
- [`find_code` returns 0 results](#find_code-returns-0-results)
- [Stale records / orphan symbols after deletes](#stale-records--orphan-symbols-after-deletes)
- [Vector dim mismatch error](#vector-dim-mismatch-error)
- [Windows-specific issues](#windows-specific-issues)
- [Runtime won't shut down cleanly](#runtime-wont-shut-down-cleanly)
- [Multiple `memtrace` processes accumulating RAM / CPU](#multiple-memtrace-processes-accumulating-ram--cpu)

## Install fails

### `npm install -g memtrace` errors with `EACCES` / permission denied

Your global npm prefix is owned by root. Either:

```bash
# Option A: install with sudo (works but not recommended)
sudo npm install -g memtrace

# Option B: re-prefix npm to a user-owned dir (recommended)
mkdir -p ~/.npm-global
npm config set prefix '~/.npm-global'
echo 'export PATH=~/.npm-global/bin:$PATH' >> ~/.zshrc
source ~/.zshrc
npm install -g memtrace
```

### `npm install -g memtrace` errors with `spawn npm.cmd EINVAL` on Windows

Fixed in **v0.3.32**. Older versions hit the
[CVE-2024-27980 mitigation](https://nodejs.org/en/blog/vulnerability/april-2024-security-releases-2)
without the `shell: true` workaround. Upgrade your Node (18.20+ /
20.12+ / 21.7+) AND make sure you're on Memtrace v0.3.32 or newer.

### `npm install` fails to download the platform binary

Memtrace's optional platform dep (`@memtrace/darwin-arm64` etc.) is
sometimes silently skipped by npm if your network blocks the
registry mid-install. Self-heal:

```bash
npm install -g memtrace --include=optional
```

If that also fails, check whether your firewall blocks
`registry.npmjs.org`.

## Runtime won't start

### "Address already in use" on port 3030 (UI) or 3000 (MCP)

Something else is using the port. Either kill it, or move Memtrace:

```bash
export MEMTRACE_UI_PORT=3035
export MEMTRACE_PORT=4848
memtrace start
```

### "License check failed" / device-flow can't open browser

If you're on a headless machine, the browser-flow doesn't work. Use
a license key instead:

```bash
export MEMTRACE_LICENSE_KEY=<your key>
memtrace start
```

License keys are obtainable at [memtrace.io](https://memtrace.io)
once you're past the waitlist.

### "ONNX Runtime not available" (exit 75 on `memtrace start`)

Symptom: `memtrace start` prints something like

```text
  ✗  ONNX Runtime not available: Failed to load ONNX Runtime dylib: …dlopen failed
  macOS: install via `brew install onnxruntime` OR set `DYLD_LIBRARY_PATH` …

  Workarounds:
    1. Install the dylib (recommended) — see hint above
    2. MEMTRACE_SKIP_EMBED=1 memtrace start — skips embedding, structural graph still works
    3. MEMTRACE_NO_REPLAY=1 — also skips git replay
```

…and the daemon exits with code 75 (`EX_TEMPFAIL`).

This is the pre-flight probe added in v0.3.83. Before v0.3.83, the
same dylib-missing condition presented as a silent hang or a long
delayed exit-0 (the panic was deep inside a worker thread and got
swallowed by the supervisor). The probe surfaces the failure in
microseconds, before any worker spawns.

**Fix — install the dylib:**

- macOS: `brew install onnxruntime`. Or if you have a custom build:
  `export DYLD_LIBRARY_PATH=/path/to/dir/with/libonnxruntime.dylib`
- Linux: install via your package manager (`apt install libonnxruntime`,
  `dnf install onnxruntime`, etc.). Or:
  `export LD_LIBRARY_PATH=/path/to/dir/with/libonnxruntime.so`
- Windows: ensure `onnxruntime.dll` is on `PATH` (the npm install
  normally drops it next to the `memtrace.exe` binary; if you moved
  the binary, copy the dll alongside).

**Workaround — run structural-only:**

If you don't need semantic search (vectors), skip the embedding stage
entirely:

```bash
MEMTRACE_SKIP_EMBED=1 memtrace start
```

The structural graph (symbols, callers, callees, references, processes)
still indexes. `find_symbol`, `find_code` (substring path), `get_impact`,
`get_evolution`, and the API-topology tools all work; `find_code` in
semantic mode and any vector-leg retrieval return empty.

Pair with `MEMTRACE_NO_REPLAY=1` if you also want to skip the git replay
stage (faster cold start on large repos):

```bash
MEMTRACE_SKIP_EMBED=1 MEMTRACE_NO_REPLAY=1 memtrace start
```

**Power-user override:** if you have a non-default ONNX Runtime build
(non-AVX2, custom EP, etc.), point the `ort` resolver at it directly:

```bash
export MEMTRACE_ORT_DYLIB_PATH=/path/to/libonnxruntime.dylib
memtrace start
```

### `memtrace start` exits immediately

Check the stderr — usually it's printing the actual reason. If it's
silent, run with verbose logging:

```bash
RUST_LOG=info memtrace start 2>&1 | head -50
```

The first 50 lines almost always reveal the problem (model download
failed, port collision, corrupt MemDB, etc.).

## Run doctor first

If Memtrace feels stuck, half-installed, or invisible to your agent,
run the read-only doctor before deleting `.memdb`:

```bash
memtrace doctor
```

Doctor checks both runtime health and agent setup:

- duplicate `memtrace` processes or listeners on the UI port
- stale or invalid `.memdb/daemon.pid`, `daemon-state.json`, and
  `~/.memtrace/runtime.json`
- legacy launchd/systemd/Windows service artifacts from older installs
- supported-agent skills and MCP registration paths

If it reports runtime conflicts or stale state, let it repair those:

```bash
memtrace doctor --fix
```

If your agent cannot see `mcp__memtrace__*` tools, or doctor says no
supported agent has both Memtrace skills and MCP registered, repair the
local agent installation too:

```bash
memtrace doctor --fix --repair-install
```

That runs the same standalone installer you can invoke manually:

```bash
npx -y memtrace-skills@latest install --local
```

If doctor reports old OS-service artifacts that can respawn stale
owners, add the legacy purge flag:

```bash
memtrace doctor --fix --purge-legacy
```

After repair, restart your AI tool so it reloads MCP configuration.

## Agent says "Memtrace index is empty (0 nodes, 0 edges)" at session start

Symptom (reported by @Corpo): your agent invokes `list_indexed_repositories` as its first MCP call, gets back an empty array, and concludes "Memtrace is broken, fall back to grep". This is **almost always** an anchor mismatch, not an actually-empty index.

**As of v0.3.89, every empty MCP response carries a `_meta` envelope explaining what happened.** Ask your agent (or inspect the raw response) for:

```jsonc
{
  "result": [],
  "_meta": {
    "anchor_source":      "cwd_fallback",   // env_override | workspace_marker | git_root | cwd_fallback
    "data_dir":           "/Users/you/agent-cwd/.memdb",
    "workspace_root":     "/Users/you/agent-cwd",
    "cwd":                "/Users/you/agent-cwd",
    "warming":            false,
    "empty_state_reason": "no_workspace_marker_in_cwd_chain"
  }
}
```

The four common `empty_state_reason` values:

| Value | What it means | Fix |
|---|---|---|
| `no_workspace_marker_in_cwd_chain` | `memtrace mcp` was launched from somewhere without a `.memtrace-workspace` marker or `.git/` above it; it created a fresh empty `.memdb` there | Add a `.memtrace-workspace` marker at the top of your project. Or set `MEMTRACE_MEMDB_DATA_DIR` to the absolute path of your real `.memdb`. |
| `workspace_genuinely_empty` | Right `.memdb` was found, but nothing has been indexed yet | Run `memtrace index <path>` or start `memtrace start` and let auto-index run |
| `still_loading` | MCP boot finished but the bound `.memdb` is still warming | Retry in 1-2 s |
| `dual_memdb_drift` | `memtrace mcp`'s anchor differs from `memtrace start`'s anchor — they're looking at different `.memdb` directories | Set `MEMTRACE_MEMDB_DATA_DIR` to the absolute path of the one with real data, OR add a `.memtrace-workspace` marker so both processes converge |

The fastest permanent fix for the common case: drop an empty file named `.memtrace-workspace` at the top of your project (or monorepo root). Both `memtrace start` and `memtrace mcp` walk upward looking for this marker and converge on the same `.memdb` regardless of which cwd they were launched from. See [`data-directories.md`](data-directories.md#where-memdb-actually-lives--workspace-anchor) for the full discovery order.

If your custom MCP client doesn't read the `_meta` envelope, the result is still a bare empty array — non-empty responses pass through bit-for-bit unchanged.

## Agent isn't using the MCP

Symptoms: your agent does lots of `Read`/`Grep`/`Glob` calls instead
of `mcp__memtrace__find_symbol`.

### Did the install register the MCP?

Check your tool's MCP config:

```bash
# Claude Code (path varies — check ~/.config/claude or ~/Library/Application Support)
cat ~/.config/claude-code/mcp.json 2>/dev/null

# Cursor
cat ~/.cursor/mcp.json 2>/dev/null
cat <project>/.cursor/mcp.json 2>/dev/null
```

Look for an entry like:

```json
{
  "mcpServers": {
    "memtrace": {
      "command": "memtrace",
      "args": ["mcp"]
    }
  }
}
```

If it's missing, re-run the installer:

```bash
memtrace doctor --fix --repair-install
```

### Did you restart your AI tool after install?

Most MCP clients only load servers at startup. After repairing the MCP
config, **fully quit and reopen** Claude Code / Cursor / Codex / etc.

### Is there a workspace owner?

`memtrace mcp` can run without a separate `memtrace start`; if no
owner exists, it becomes the owner for that agent session. If
`memtrace start` (including `memtrace start --headless`) is already
running, `memtrace mcp` attaches to it.

If the agent reports connection errors or empty state, check what
Memtrace thinks is active:

```bash
memtrace status
```

It should print the resolved data directory and indexed repository
counts. If you want a persistent foreground owner while debugging,
run `memtrace start` from the repository root.

### Did the agent skill not get installed?

Memtrace ships agent skills (`memtrace-first`, `memtrace-search`, etc.)
that nudge the agent toward MCP-first behaviour. They live at:

- Claude Code: `~/.claude/skills/`
- Codex: `~/.agents/skills/`
- Cursor: `~/.cursor/skills/`
- Windsurf: `~/.codeium/windsurf/skills/`
- VS Code Copilot: `~/.copilot/skills/`

If those directories are empty, repair the local skills/MCP install:

```bash
memtrace doctor --fix --repair-install
```

## Cortex recall is missing, unavailable, or empty

First, check the MCP configuration. Cortex tools come through the normal
Memtrace server:

```json
{
  "mcpServers": {
    "memtrace": {
      "command": "memtrace",
      "args": ["mcp"]
    }
  }
}
```

Do not configure `memcortex-mcp`, a package-internal Node path, a Unix
socket, or a Windows named pipe. Remove any second Cortex MCP entry.

Then check the runtime and Cortex's own status:

```bash
memtrace start
curl -s http://localhost:3030/api/cortex/status
```

Windows PowerShell:

```powershell
memtrace start
Invoke-RestMethod http://localhost:3030/api/cortex/status
```

Wait until `snapshotState` is `fresh`. Restart or reconnect the MCP client
after Memtrace starts so it reloads the tool list.

| Symptom | Likely cause | Action |
|---|---|---|
| Cortex tools are absent | Wrong/stale MCP registration | Keep only `command: "memtrace"`, `args: ["mcp"]`; run `memtrace doctor --fix --repair-install`; restart the client. |
| Tool reports Cortex unavailable or the local transport closed | Runtime or sidecar is not ready | Run `memtrace stop`, `memtrace doctor --fix`, then `memtrace start`; poll `/api/cortex/status`. |
| Status is healthy but recall returns `CannotProve` | No evidence matched the query | Try a short distinctive phrase from a known decision. Check that governance candidates were confirmed. |
| `memtrace govern` lists files but the Cortex index/count does not change | A sweep is classification/review, not persistence | Confirm in the dashboard, use `--accept-all`, or use `govern add/edit` for one exact assertion. |
| `memtrace index --clear` or `memtrace reset` did not change Cortex | Expected: those commands only affect the MemDB code graph | Use the Cortex runtime and governance flow; do not repeat source reindexing. |

There is no supported full Cortex reindex command or MCP flag. Do not
delete `~/.memtrace/cortex-store`. If recovery still fails, save the
output of `/api/cortex/status`, `memtrace status --json`,
`memtrace --version`, your OS, and the exact `recall_decision` request.

See [`cortex.md`](cortex.md) for parameter details and governance examples.

## Dashboard graph or timeline looks partial after cold start

Symptoms:

- the dashboard initially shows a full graph, then the **All
  repositories** view looks partial
- individual repositories render correctly, but the combined view lags
- the history/timeline panel says there is no history yet while replay
  is still warming

On a cold start, Memtrace may be attaching to the existing workspace
owner, loading graph partitions, replaying git history, and writing
embeddings in overlapping phases. During that window, a combined graph
view can legitimately be incomplete even though per-repository views
are already available.

Check status instead of guessing:

```bash
memtrace status --json
```

Look at the `graph`, `phase2`, `auth_session`, and `recovery` fields.
If status reports auth degradation, refresh login:

```bash
memtrace auth login
```

If status or doctor reports stale runtime state, process conflicts, or
missing MCP setup, repair it:

```bash
memtrace doctor --fix
memtrace doctor --fix --repair-install
```

If the combined graph is still partial after the replay/embedding
status is successful, hard-refresh the dashboard once. If it remains
partial, capture `memtrace status --json` and `memtrace doctor --json`
with your issue report; those outputs include the recovery hints the UI
uses.

## `MEMTRACE_TRANSPORT=sse` hangs

Fixed in **v0.3.32**. Earlier versions had streamable-HTTP gated
behind a non-default feature flag, so setting `sse` silently fell
back to stdio without binding any HTTP server.

Upgrade:

```bash
npm install -g memtrace@latest
memtrace --version    # should show 0.3.32 or later
```

Then:

```bash
MEMTRACE_TRANSPORT=streamable-http MEMTRACE_PORT=4848 memtrace mcp
```

Verify:

```bash
curl -X POST http://localhost:4848/mcp \
  -H 'Content-Type: application/json' \
  -H 'Mcp-Session-Id: test-1' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
```

A successful response means HTTP transport is working. See
[`mcp-and-transports.md`](mcp-and-transports.md) for the full setup.

## Indexing hangs / never finishes

### Stuck at "Loading embedding model"

First-run only. The embedding model is being downloaded (~340 MB)
and CoreML / OpenVINO is compiling the graph. On Apple Silicon this
can take 1–3 minutes for fp32. Either wait, or:

```bash
# Force int8 (smaller, faster compile)
export MEMTRACE_EMBED_QUANT=int8

# Or skip CoreML entirely (CPU-only — slower but no compile)
export MEMTRACE_DISABLE_COREML=1

memtrace stop && memtrace start
```

### Stuck mid-index on a specific file

A pathological source file (10MB minified blob, generated code,
etc.) can wedge the parser. Add it to `.memtraceignore`:

```
# .memtraceignore
**/generated/**
**/*.min.js
**/*_pb2.py
```

Then `memtrace start` and the file will be skipped.

### Indexing finishes but the graph is empty

`memtrace status` should show > 0 symbols. If it's zero on a non-
empty repo, the indexer skipped everything. Most common cause:
your repo's primary language isn't in the supported list.

Currently parsed: Python, JS, TS, Rust, Go, Java, Ruby, C, C++, C#.

If your codebase is in a different language (Elixir, OCaml, Haskell,
etc.), Memtrace can't parse it yet. The graph will be empty.

### Indexing pulls in too much — generated code, vendored deps, fixtures

If the indexed graph contains files you'd rather Memtrace skipped,
you have three layers of control. From most-to-least common:

1. **Make sure your `.gitignore` is doing its job.** Memtrace honours
   gitignore by default. Run `git check-ignore -v <path>` to confirm
   a path is ignored — if it's not, add it to `.gitignore` and reindex.
2. **Drop a `.memtraceignore` at the repo root** for paths you want
   git to track but Memtrace to skip (generated code, vendored
   dependencies, fixture blobs). Same syntax as `.gitignore`.
3. **The built-in always-on skip list** already handles `node_modules`,
   `target`, `dist`, `.next`, `__pycache__`, `.venv`, `coverage`,
   `vendor`, `.terraform`, `.git`, `.memdb`, `.claude`, plus binary /
   media file extensions — no config needed.

Full reference (precedence rules, the complete built-in list, common
patterns): [`indexing-and-ignore-rules.md`](indexing-and-ignore-rules.md).

After updating ignore rules, reindex from scratch to drop previously-
indexed files that are now ignored:

```bash
memtrace index --clear-existing /path/to/repo
```

Or via MCP: `index_directory(path="…", clear_existing=true)`.

## Indexing eats all my RAM

This was the v0.3.30 failure mode. v0.3.31+ caps thread fan-out and
batch size by host tier.

### Quick verification you're on the fix

```bash
memtrace --version    # should be 0.3.31 or later
```

In the daemon's stderr you should see, near startup:

```
ort: global thread pool capped — intra_op=2, inter_op=1
```

If you don't see that, you're on a pre-v0.3.31 build.

### If you're on v0.3.31+ and still struggling

```bash
export MEMTRACE_TIER=light
export MEMTRACE_EMBED_BATCH_SIZE=4
export MEMTRACE_EMBED_INTRA_OP_THREADS=1
export MEMTRACE_RERANK=off
export MEMTRACE_EMBED_RSS_LIMIT_GB=4
memtrace stop && memtrace start
```

See [`performance-tuning.md`](performance-tuning.md) for the full
range of options.

## Embedding warns about batch timeouts on slow CPUs

You see this in the daemon's stderr during the initial bootstrap:

```
WARN code_intelligence::search::semantic: Embedding batch timed out
after 60s on 32 symbols — abandoning worker; will respawn on next call.
Bump MEMTRACE_EMBED_BATCH_TIMEOUT_SECS for slow CPU paths, or set
MEMTRACE_EMBED_TIMEOUT_DEBUG=1 to log the offending input previews.
```

**It's not fatal.** The pipeline is self-healing — the worker is
abandoned, a fresh one respawns on the next call, and the batch is
retried. The bootstrap will eventually finish; you'll just see the
warning a lot in the meantime.

The default 60s ceiling is tuned for AVX2-capable CPUs running int8 or
fp32 quantised models. On a pre-AVX2 host (Intel Ivy Bridge / Xeon E5
v2 and older, AMD pre-Excavator) inference is 5–10× slower per batch
because the model falls back to scalar / SSE codepaths.

### Confirm you're on the slow path

`memtrace start` prints a host-profile line near the top of stderr:

```
Host profile: Intel(R) Xeon(R) CPU E5-2697 v2 @ 2.70GHz · 24 cores · 15 GB · score=5 · tier=standard · embed=int8
```

`tier=standard` + a known pre-AVX2 CPU = you're hitting the slow path.

### Fix: raise the timeout, lower the batch size

```bash
export MEMTRACE_EMBED_BATCH_TIMEOUT_SECS=240   # 4 min per batch instead of 60s
export MEMTRACE_EMBED_BATCH_SIZE=32            # smaller batches finish faster per call
memtrace stop && memtrace start
```

`MEMTRACE_EMBED_BATCH_TIMEOUT_SECS` and `MEMTRACE_EMBED_BATCH_SIZE`
compose: smaller batches reduce per-call wall time so the timeout is
less likely to fire; the higher ceiling absorbs the occasional outlier
batch (a long symbol body, GC pause, etc.).

If you need to know **which** symbols are timing out, set
`MEMTRACE_EMBED_TIMEOUT_DEBUG=1` — the warning will log a preview of
the offending input. Off by default because these previews include
source snippets.

### Persistence

| how memtrace is launched | where to set the env vars |
|---|---|
| Shell-launched `memtrace start` | `export …` in `~/.bashrc` / `~/.zshrc` |
| systemd service | `[Service]` block: `Environment=MEMTRACE_EMBED_BATCH_TIMEOUT_SECS=240` |
| Claude Code / Cursor MCP server | `~/.claude/settings.json` → memtrace MCP entry → `"env": { … }` block |
| direnv | `export …` in `.envrc` at the workspace root |
| docker / Kubernetes | `-e MEMTRACE_EMBED_BATCH_TIMEOUT_SECS=240` or pod env |

### Once the bootstrap is done

The watcher steady-state is incremental embeds on file save (small
batches), so the warning stops on its own once the initial pass is
complete. You can leave the env vars set; they just become a higher
ceiling that's almost never hit.

See also [`pre-avx2-cpus.md`](pre-avx2-cpus.md) if memtrace itself
won't launch (CPU baseline gate refuses to start with `"this CPU does
not support AVX2"`) — that's a different failure mode about the
bundled ONNX Runtime requiring an AVX2-capable CPU.

## `find_code` returns 0 results

### The repo isn't indexed

```bash
memtrace status
```

If the repo isn't listed, run `memtrace index <path>` from its
root, or `memtrace start` (which auto-indexes).

### The query is too narrow / specific

`find_code` is hybrid (BM25 + vector + rerank), but it can still
miss if the query has no overlap with any indexed symbol's name,
signature, or body. Try:

- Broaden the query: `"authentication"` instead of `"oauth2_pkce_handler"`
- Use shorter / more abstract terms
- Try `find_symbol` with `fuzzy: true` instead

### The body wasn't embedded

Symbols smaller than `MEMTRACE_EMBED_MIN_LINES` (default 4) aren't
embedded — they're still queryable by name (BM25), just not
semantically. If you need full coverage:

```bash
export MEMTRACE_EMBED_MIN_LINES=1
memtrace reset && memtrace start
```

### You're querying a non-indexed file path

If you scope `find_code` to `file_path_filter="<dir>"` and that
directory wasn't parsed (excluded, unsupported language), you get
zero results. Check the repo stats; remove the filter.

## Stale records / orphan symbols after deletes

If you've:
- Run `rm -rf` on a directory while the daemon was off
- Removed a git worktree without the daemon noticing
- Switched branches and old symbols still appear

You have stale records. Clean them up:

```
mcp__memtrace__cleanup_stale_records(
    repo_id="<your repo>",
    check_missing=true,
    dry_run=true       # see what would be deleted
)
```

If the dry-run output looks right, re-run with `dry_run=false`.

## Vector dim mismatch error

Symptom:

```
Embedding model dim mismatch: jina-embeddings-v2-base-code produces
768-dim vectors but MemDB HNSW is configured for 384-dim.
Reset with `memtrace reset` then re-index with
`MEMTRACE_VECTOR_DIMS=768 memtrace index <path>`.
```

You changed `MEMTRACE_EMBED_MODEL` but not `MEMTRACE_VECTOR_DIMS`.
The HNSW index is fixed-dim and can't accept mismatched vectors.

Fix:

```bash
memtrace reset                                # wipe MemDB
export MEMTRACE_EMBED_MODEL=jina-code         # or whatever you picked
export MEMTRACE_VECTOR_DIMS=768               # match the model's output
memtrace start                                # re-indexes from scratch
```

Common mappings:

| Model | Dim |
|---|---|
| `jina-embeddings-v2-base-code` (default) | 768 |
| `bge-base-en-v1.5` | 768 |
| `nomic-embed-text-v1.5` | 768 |
| `bge-small-en-v1.5` | 384 |

## Windows-specific issues

### `spawn npm.cmd EINVAL`

Fixed in v0.3.32. Upgrade.

### Auto-rewrite hooks don't fire on native Windows

The auto-rewrite hook (used by some integrations) requires a Unix
shell. Native Windows falls back to `CLAUDE.md` injection — your
agent gets the instructions but commands aren't auto-rewritten.

For full hook support on Windows, use **WSL**.

### Antivirus / Defender quarantines the binary

The Memtrace binary is unsigned (we're working on it). Defender or
corporate AV may quarantine it on first run. Add an exclusion for:

- `~/.npm-global/lib/node_modules/memtrace/`
- `~/.memtrace/`

### Dashboard at `localhost:3030` doesn't open

On native Windows, sometimes the firewall prompts and you click
"Cancel" by reflex. Re-run:

```powershell
memtrace stop
memtrace start
```

and accept the firewall prompt this time. Or run inside WSL where
firewalls don't intervene for localhost binds.

## Claude Code hook JSON validation failed

Symptom: every prompt shows

```text
UserPromptSubmit hook error
Hook JSON output validation failed — (root): Invalid input
```

**Cause:** Memtrace **0.6.0** shipped a `UserPromptSubmit` hook that
emitted the old flat JSON shape (`decision: "continue"` +
top-level `additionalContext`). Current Claude Code requires the
`hookSpecificOutput` envelope.

**Fix:** upgrade and refresh hooks:

```bash
npm install -g memtrace@latest
memtrace install
```

You need **0.6.10** or later. Restart Claude Code after upgrading.

## Runtime won't shut down cleanly

`memtrace stop` should stop the workspace runtime and free its ports.
It also unloads legacy OS service registrations (`launchd` /
`systemd` / Windows Service) from installs that used the removed
`memtrace daemon install` command. If it hangs or returns "no daemon
running" while a process is still listening:

```bash
# Find any memtrace process
ps -ef | grep memtrace

# Kill it
pkill -f "memtrace start"
pkill -f "memtrace mcp"
```

If a port is still bound after the process is dead, restart the
machine (the OS will release the socket on next boot).

## Multiple `memtrace` processes accumulating RAM / CPU

**Symptom.** Activity Monitor / Task Manager shows two or more
`memtrace` processes for the same project, combined RSS climbing
into the multi-GB range. Common variants users have reported:

- 2–4 `memtrace mcp` processes, each at 500 MB – 6 GB resident.
- One `memtrace start` process pegging a single CPU core at 100%
  for hours, total CPU time in the tens of thousands of seconds.
- `~/.orbit/tmp/memtrace-daemon-state.json` (or the equivalent
  supervisor heartbeat file in your orchestration tool) showing
  `{"pid": null, "status": "healthy"}` while processes are very
  much alive.

**What should happen in current versions.** Memtrace keeps an
owner lock in the resolved `.memdb` directory. Only one process
should own embedded MemDB for a workspace. Additional `memtrace mcp`
processes should attach over localhost loopback and stay thin.

It is normal to see more than one `memtrace` process when multiple
agents or transports are open. It is not normal for each one to grow
into a heavy owner for the same `.memdb`.

If you see multiple heavy owners after upgrading, first check for
stale old processes from a pre-fix version. Kill the older processes
manually:

```bash
# macOS / Linux
ps -ef | grep memtrace | grep -v grep
# pick the OLDER PID(s) and kill them
kill <pid>
# escalate if it doesn't die
kill -9 <pid>

# Windows PowerShell
Get-Process memtrace | Sort-Object StartTime
# kill all but the newest
Stop-Process -Id <pid>
```

Then restart your supervisor cleanly:

```bash
memtrace stop && memtrace start
```

If it comes back immediately, collect:

```bash
memtrace status
ls -la .memdb/daemon.pid .memdb/daemon-state.json
cat .memdb/daemon-state.json
```

and open an issue. That means the owner lock or attachment path is
not behaving as intended on your host.

## Still stuck?

Open a GitHub issue at
[`syncable-dev/memtrace-public`](https://github.com/syncable-dev/memtrace-public/issues)
with:

1. `memtrace --version`
2. Your OS + version (`uname -a` on Unix; `winver` on Windows)
3. The full output of `memtrace status`
4. The exact command you ran and the exact error you saw

The issue tracker is fastest. Discord is also good for "did anyone
else hit this?" questions —
[discord.gg/memtrace](https://discord.gg/RySmvNF5kF).
