# Cortex decision memory

Cortex is Memtrace's local memory for decisions, bans, conventions, and
governing documents. It is separate from the MemDB code graph.

| Store | Contains | Normal refresh path |
|---|---|---|
| MemDB code graph | Source symbols, relationships, and git history | `memtrace index`, `memtrace index --clear`, or MCP `index_directory` |
| Cortex decision memory | Decisions captured from episodes and confirmed governance documents | Automatic capture plus governance review/confirmation |

This distinction matters during recovery. Rebuilding or resetting the code graph
does not rebuild Cortex.

## Configure one MCP server

The supported public MCP entry point is `memtrace mcp`:

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

Do not point an MCP client at `memcortex-mcp`, a bundled Node path, a socket, or
a named pipe. Those are internal implementation details. The normal Memtrace MCP
server publishes the Cortex tools and proxies their calls over the correct local
transport on macOS, Linux, and Windows.

Start the workspace runtime before using decision memory:

```bash
memtrace start
```

Then restart or reconnect the MCP client so it reloads the tool list. See
[`mcp-and-transports.md`](mcp-and-transports.md) for client configuration.

## Recall a decision

`recall_decision` accepts a natural-language query and two optional filters:

| Argument | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | Non-empty description of the decision, ban, or convention. |
| `top_k` | positive integer | no | Maximum number of results. Defaults to 10 when omitted. |
| `min_score` | finite number | no | Drop results below this score. |

Example tool arguments:

```json
{
  "query": "why do we avoid raw SQL in request handlers?",
  "top_k": 5,
  "min_score": 0.05
}
```

An honest `CannotProve` response means Cortex did not find enough evidence for
that query. It is not proof that the runtime or store is broken. Try a short,
distinctive phrase from a known decision before changing runtime state.

## Governance intake must be confirmed

Pointing `memtrace govern` at a file, directory, or glob classifies the matched
files and shows the governance candidates it recognizes. A preview alone does
not add those candidates to decision memory.

```bash
# Preview and classify candidates for review
memtrace govern docs/adr

# Persist every governance candidate shown by this run
memtrace govern docs/adr --accept-all
```

`--accept-all` persists the classifier's recommendation for each governance
candidate shown in the preview. Effective ADRs, agent rules, and included
standing rules are written as `include`; ineffective ADRs and excluded standing
rules are written as `exclude`. Rows classified as `NotGovernance` are omitted.

You can also review and confirm candidates in the dashboard's Cortex Governance
view. Use `govern add` or `govern edit` when you need to assert one exact document
and its meaning without relying on classification:

```bash
memtrace govern add docs/adr/0007-use-postgres.md \
  --type adr \
  --guidance "Use Postgres for durable application state" \
  --scope 'path:apps/api/**'

memtrace govern edit docs/adr/0007-use-postgres.md \
  --type adr \
  --guidance "Use Postgres for durable application state" \
  --scope 'path:apps/**'
```

`edit` replaces the full assertion. Omitting `--scope` or `--ban` clears that
part of the previous assertion.

## Status and recovery

Run `memtrace start`, then inspect Cortex directly rather than inferring its
health from code-index counts:

```bash
curl -s http://localhost:3030/api/cortex/status
memtrace status --json
```

On Windows PowerShell:

```powershell
Invoke-RestMethod http://localhost:3030/api/cortex/status
memtrace status --json
```

The same status is visible in the Cortex tab at `http://localhost:3030`.
Wait for `snapshotState` to become `fresh` before treating decision counts as
current. A temporarily false `daemonAvailable` can mean the serial endpoint is
busy, so retry before assuming the daemon is dead.

If the endpoint stays unavailable:

```bash
memtrace stop
memtrace doctor
memtrace doctor --fix
memtrace start
```

Restart the MCP client after the runtime is healthy, then call
`recall_decision` through the existing `memtrace` MCP server.

There is no supported Cortex reindex CLI command or MCP flag. These operations
only rebuild or clear the MemDB code graph:

- `memtrace index`
- `memtrace index --clear`
- `index_directory` with `clear_existing: true`
- `memtrace reset`

Do not delete `~/.memtrace/cortex-store` as a recovery step. If the restart and
governance confirmation flow still fail, capture the Cortex status JSON,
`memtrace status --json`, `memtrace --version`, the OS, and the exact
`recall_decision` arguments for a bug report.
