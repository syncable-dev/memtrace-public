# CLI reference

This page mirrors the user-facing `memtrace --help` surface and adds the
extra context that does not fit in terminal help.

```bash
memtrace [COMMAND] [FLAGS]
```

Run this any time to see the exact command set for your installed binary:

```bash
memtrace --help
```

## Commands

| Command | Purpose |
|---|---|
| `memtrace start` | Start the local UI/API server, file watcher, PR command loop, and workspace owner against MemDB. This is also the default command when you run `memtrace` with no subcommand. |
| `memtrace stop` | Stop the running `memtrace start` workspace runtime (and unload any legacy OS service registrations from older installs). |
| `memtrace status` | Show backend, index, and runtime status. |
| `memtrace doctor [--fix] [--repair-install] [--purge-legacy]` | Diagnose runtime, stale locks, process conflicts, agent skills, and MCP registration. With flags, clean stale runtime state and reinstall local skills/MCP config. |
| `memtrace mcp` | Run the MCP server for Claude, Cursor, Codex, and other MCP-compatible agents. It attaches to an existing workspace owner or becomes the owner if none is running. |
| `memtrace index <path>` | Index a repository or workspace into MemDB, then exit. |
| `memtrace govern <path|glob> [--accept-all]` | Classify governance candidates for review. With `--accept-all`, persist all candidates classified by the current run. |
| `memtrace govern add|edit <path> ...` | Manually assert or replace one governance document in Cortex decision memory. |
| `memtrace code-review setup` | Choose the default headless agent for `@memtrace fix this`. |
| `memtrace code-review --pr <url>` | Review a GitHub pull request using local Memtrace context. |
| `memtrace pr status` | Show local PR watches registered by `memtrace code-review --post --watch`. |
| `memtrace pr sync` | Poll watched PRs once via the installed GitHub App. |
| `memtrace reset [repoId]` | Reset graph data. With no argument, wipes the local `.memdb`; with a repo id, removes only that repo's records and embedding cache. |
| `memtrace install` | Upgrade to the latest npm package. `memtrace install start` upgrades and then starts. |
| `memtrace install-rtk` | Run the optional RTK install/init flow. |
| `memtrace insight-card [repoId]` | Print the Codebase Insight Card for one or more indexed repos. |
| `memtrace install-hooks [--pre-commit] [--pre-push] [path]` | Install Memtrace-managed git hook blocks. Default installs pre-push only. |
| `memtrace uninstall-hooks [--pre-commit] [--pre-push] [path]` | Remove Memtrace-managed git hook blocks. |
| `memtrace pre-commit [--staged] [--agent-mode]` | Hook runtime used by installed pre-commit hooks. |
| `memtrace warmup [--model <name>]` | Preload and compile the local embedding model, useful on Apple Silicon. |
| `memtrace embed list` | List available local embedding presets. |
| `memtrace embed status` | Show active embedding config and current MemDB vector dimensions. |
| `memtrace embed set ...` | Set local or remote embedding provider configuration. |
| `memtrace embed test` | Probe the active embedding provider and report latency/dimensions. |
| `memtrace version` | Print the installed version. |
| `memtrace help` | Show top-level help. |

## Command roles

`memtrace index`, `memtrace start`, and `memtrace mcp` all use the
same local `.memdb` store, but they have different lifetimes:

- `memtrace index .` is a one-shot graph build or refresh.
- `memtrace start` is the visible local owner: UI, watcher, PR command
  loop, and API server.
- `memtrace mcp` is the agent-facing MCP server. It attaches to the
  visible owner if one exists; otherwise it can own the workspace
  itself for agent-only workflows.

Memtrace keeps an owner lock in the resolved `.memdb` directory, so
multiple agent sessions should not open duplicate embedded MemDB
owners for the same workspace.

Cortex uses a separate decision store. `memtrace index`, `--clear`,
`memtrace reset`, and MCP `index_directory(clear_existing=true)` only
affect the MemDB code graph; there is no normal Cortex reindex flag.
See [`cortex.md`](cortex.md).

## Governance intake

The sweep command classifies files for review. It does not persist a
preview unless you confirm candidates in the Cortex Governance view or
pass `--accept-all`:

```bash
memtrace start
memtrace govern docs/adr
memtrace govern docs/adr --accept-all
```

Use `add` or `edit` to make an exact manual assertion about one file:

```bash
memtrace govern add docs/adr/0007-use-postgres.md \
  --type adr \
  --guidance "Use Postgres for durable application state" \
  --scope 'path:apps/api/**'
```

Valid `--type` values are `adr`, `agent_rule`, and `standing_rule`.
`--scope` is optional and accepts comma-separated path selectors. `--ban`
marks a hard constraint. `edit` replaces the full assertion rather than
merging omitted fields from the previous one.

## Doctor and repair

Use `memtrace doctor` when Memtrace feels half-installed, the UI/API is
stuck, MCP tools are missing from your agent, or a previous run left
stale runtime state behind.

```bash
memtrace doctor
memtrace doctor --fix
memtrace doctor --fix --repair-install
memtrace doctor --fix --purge-legacy
```

Plain `doctor` is read-only. It reports:

- running `memtrace` processes and listeners on the UI port
- stale or invalid `.memdb/daemon.pid` and `daemon-state.json`
- stale `~/.memtrace/runtime.json`
- legacy launchd/systemd/Windows service artifacts from older installs
- supported agent setup: skills count, MCP registration, and config path

`--fix` stops stale supervisors, frees the UI port, and removes stale
PID/state artifacts. `--repair-install` reruns the local skills/MCP
installer:

```bash
npx -y memtrace-skills@latest install --local
```

Use `--fix --repair-install` when agents cannot see
`mcp__memtrace__*` tools or `doctor` reports that no supported agent
has both Memtrace skills and MCP registered. Use `--purge-legacy` only
when `doctor` reports old OS-service artifacts that can respawn a stale
owner.

## Global flags

| Flag | Applies to | Purpose |
|---|---|---|
| `--clear`, `--fresh` | `start`, `index` | Wipe the resolved local MemDB data directory before connecting. |
| `--workspace <path>` | `start`, `index`, `mcp` workspace resolution | Treat `path` as a multi-repo workspace root and write a `.memtrace-workspace` marker there. |
| `--no-workspace` | `start` | Disable the auto-workspace behavior when the current directory looks like a parent folder containing multiple git repos. |
| `--headless` | `start` | Keep the HTTP API on `MEMTRACE_UI_PORT` (default `3030`) but do not auto-open a browser tab. Use for Orbit, CI, and agent hosts. |
| `--no-ui`, `--no-browser` | `start` | **Deprecated aliases** for `--headless` (one-time note on first use). Prefer `--headless` or `MEMTRACE_HEADLESS=1`. |

> **Removed in 0.6.10:** `memtrace service` / `memtrace daemon` (OS login autostart install) was removed. Use `memtrace start` (optionally `--headless`) or `memtrace mcp`. `memtrace stop` still tears down legacy launchd/systemd/Windows service registrations if an older install left them behind.

## Code review

`memtrace code-review` is local-first. The hosted Memtrace service only
mints a short-lived GitHub App installation token. Diff analysis, graph
lookup, ranking, and comment selection run on the developer machine.
For the end-to-end PR workflow, GitHub comment commands, and local agent
fix setup, see [`code-reviewer.md`](code-reviewer.md).

```bash
memtrace code-review setup
memtrace code-review --pr https://github.com/OWNER/REPO/pull/123
memtrace code-review --pr https://github.com/OWNER/REPO/pull/123 --post
memtrace code-review --pr https://github.com/OWNER/REPO/pull/123 --post --watch
```

Flags:

| Flag | Purpose |
|---|---|
| `--post` | Publish GitHub PR review comments. Without this, Memtrace prints a dry-run preview. |
| `--watch` | After posting, track the PR locally for human response, approval, requested changes, merge, close, or new pushes. |
| `--dry-run` | Force preview mode even if `--post` is present. |
| `--max-comments <N>` | Comment budget. Default is 5 when posting and 10 for dry runs. |
| `--min-severity <low|medium|high|critical>` | Minimum issue severity. Default is `high`. |
| `--graph-mode <strict|off>` | Use graph-backed review checks in `strict` mode, or disable graph-backed checks with `off`. Default is `strict`. |
| `--repo-id <ID>` | Memtrace repo id for graph review. Defaults to `MEMTRACE_DEFAULT_REPO`, then the local repo basename. |
| `--repo-root <PATH>` | Local checkout root. Defaults to the git toplevel. |

Run `memtrace code-review setup` once to choose the local headless
agent used by `@memtrace fix this`. Non-interactive examples:

```bash
memtrace code-review setup --agent codex
memtrace code-review setup --agent claude
memtrace code-review setup --agent cursor
memtrace code-review setup --agent gemini
memtrace code-review setup --agent auto
```

The GitHub App must be installed on the repository before `--post` can
publish comments. The local UI exposes an **Install app** action for the
selected indexed repository when Memtrace can derive the GitHub `owner/repo`
from the repo remote.

## PR watches

Use watches when you want Memtrace to keep local state for posted PR
reviews while you wait for human feedback.

```bash
memtrace pr status
memtrace pr sync
```

`status` reads local watch state. `sync` polls GitHub once and updates
whether reviewed PRs are awaiting response, approved, changed, merged,
closed, stale after a push, or blocked by a poll error.

## Embedding provider management

Local presets:

```bash
memtrace embed list
memtrace embed status
memtrace embed set jina-code
memtrace embed test
```

Remote OpenAI-compatible providers, including Voyage:

```bash
export VOYAGE_API_KEY='vk-...'

memtrace embed set --remote openai-compat \
  --url https://api.voyageai.com/v1 \
  --model voyage-code-3 \
  --dim 1024 \
  --api-key-env VOYAGE_API_KEY

memtrace embed test
```

If the configured vector dimension does not match the existing MemDB
store, Memtrace refuses to silently rebuild vectors at startup. Use
`memtrace embed status` to inspect the mismatch, then explicitly reset
and reindex when that is what you want.

## Environment files

Memtrace loads `.env` files from the current directory and its parents
without overriding variables already set in the shell. This lets a
workspace carry local defaults such as:

```bash
VOYAGE_API_KEY=...
GITHUB_TOKEN=...
MEMTRACE_UI_PORT=3030
```

`.env` files are not indexed as source code.

## Important environment variables

| Variable | Purpose |
|---|---|
| `MEMTRACE_UI_PORT` | Local HTTP API + dashboard port. Default: `3030`. |
| `MEMTRACE_UI_HOST` | Bind address for the HTTP API. Default: `127.0.0.1`. Set only when you intentionally need remote/VM/container exposure. |
| `MEMTRACE_HEADLESS` | Skip browser auto-open on `memtrace start` (`1`, `true`, `yes`, or `on`). API stays up. |
| `MEMTRACE_NO_UI`, `MEMTRACE_NO_BROWSER` | Deprecated aliases for `MEMTRACE_HEADLESS`. |
| `MEMTRACE_TRANSPORT` | MCP transport: `stdio`, `streamable-http`, `sse`, or `http`. |
| `MEMTRACE_PORT` | MCP HTTP port when transport is not stdio. |
| `MEMTRACE_DATA_DIR` | Job state directory. |
| `MEMTRACE_MEMDB_DATA_DIR` | MemDB data directory. Defaults to the resolved workspace `.memdb`. |
| `MEMTRACE_MEMDB_MODE` | `embedded` or `remote`. Default: `embedded`. |
| `MEMTRACE_MEMDB_ENDPOINT` | Remote-mode MemDB gRPC endpoint. |
| `MEMTRACE_EMBED_MODEL` | Local embedding preset when using local embeddings. |
| `MEMTRACE_VECTOR_DIMS` | Explicit vector dimension override. Prefer `memtrace embed set` for normal use. |
| `MEMTRACE_SKIP_EMBED` | Skip all embedding (index, watcher, search). Structural graph remains usable. |
| `MEMTRACE_SKIP_WATCHER_EMBED` | Skip per-save watcher embedding only; keep index-time embed. |
| `MEMTRACE_NO_REPLAY` | Skip git-history replay. HEAD graph remains usable. |
| `MEMTRACE_OWNER_ATTACH` | Set to `0` to force legacy direct embedded open for debugging. |
| `MEMTRACE_START_PHASE2` | Set to `foreground` to run embedding/replay in the foreground instead of background phase 2. |
| `MEMTRACE_TELEMETRY` | Set to `off` to disable product telemetry. |

See [`environment-variables.md`](environment-variables.md) for the full
reference.
