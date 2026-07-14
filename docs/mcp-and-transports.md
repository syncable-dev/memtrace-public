# MCP and transports

How agents talk to Memtrace, and how to pick the right transport for
your situation.

## Background — why two transports

The Model Context Protocol (MCP) is a JSON-RPC dialect Anthropic
introduced for tool calls between AI clients and external services.
The wire shape is the same regardless of how you ship those bytes;
the spec defines two transports:

- **stdio** — the agent spawns the MCP server as a child process and
  pipes JSON-RPC over stdin/stdout. One process per agent session.
- **streamable-HTTP** — the MCP server is a long-running HTTP service
  listening on a port; many agent sessions multiplex through it via
  a session id in headers.

Memtrace supports both. The stdio path has been there from day one.
**Streamable-HTTP is on by default since v0.3.32.**

## Which one should I use?

```
                     ┌────────────────────────────────────────┐
                     │  How many concurrent agents share this │
                     │  Memtrace install?                     │
                     └────────────────────────────────────────┘
                                       │
              ┌────────────────────────┴───────────────────────────┐
              │                                                    │
        1 agent at a time                              many agents (5+)
        (Claude Code, Cursor,                          (Orbit, agent platforms,
         single dev workflow)                           dashboards, CI fleets)
              │                                                    │
              ▼                                                    ▼
        Use stdio.                                   Use streamable-HTTP.
        (the default)                                (one server, many sessions
                                                      multiplexed through it)
```

If you're a regular dev using one editor at a time, **don't change
anything** — stdio Just Works. The rest of this document is for
people building on top of Memtrace.

## stdio transport

### How it looks

```
   ┌─────────────────────┐
   │  Claude Code        │
   │  (or Cursor, Codex, │
   │   Gemini CLI…)      │
   └──────────┬──────────┘
              │ spawns
              ▼
   ┌─────────────────────┐
   │  memtrace mcp       │
   │  child process      │
   │  stdio JSON-RPC     │
   └──────────┬──────────┘
              │
              │ attaches to an existing owner,
              │ or becomes the owner if none exists
              ▼
   ┌─────────────────────┐
   │ workspace owner     │
   │ one per .memdb      │
   │ (MemDB, models,     │
   │  indexes, loopback) │
   └─────────────────────┘
```

The agent spawns one `memtrace mcp` per session. That process first
checks whether the current workspace already has a Memtrace owner:

- If `memtrace start`, a headless daemon, or another `memtrace mcp`
  already owns the workspace, the new process attaches over localhost
  loopback and stays thin.
- If nothing owns the workspace yet, the first `memtrace mcp` becomes
  the owner itself and opens the embedded MemDB.

That means a normal agent-only setup does not require a separate
`memtrace start` command. Run `memtrace start` when you want the local
dashboard, live file watcher, PR command polling, or a visible
foreground owner.

This ownership step does not index an unknown repository by itself.
Index each repo once with `memtrace index .`, `memtrace start`
auto-indexing, or the MCP `index_directory` tool before expecting
graph-backed answers.

When you close your Claude Code window, the stdio child exits. If it
was only attached, the owner keeps running. If it was the owner, the
owner exits with that MCP session.

### Setup

Nothing — it's the default. `npm install -g memtrace` writes the MCP
config entry into your client's config file, and the agent picks up
the config on next launch.

### Configuration shape

For tools that don't auto-configure, the entry your config needs is:

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

That tells the client "spawn `memtrace mcp` as a child and talk to
its stdio". Per-tool config locations:

| Tool | Config file |
|---|---|
| Claude Code | `~/.config/claude-code/mcp.json` (varies by OS) |
| Cursor | `<project>/.cursor/mcp.json` or `~/.cursor/mcp.json` |
| Codex | `~/.codex/mcp.json` |
| Gemini CLI | `~/.gemini/mcp.json` |
| Windsurf | `~/.windsurf/mcp.json` |

The Memtrace installer handles this for you — most users never edit
these files.

### Cortex uses the same MCP entry

Cortex decision-memory tools are exposed by the normal `memtrace mcp`
server. Configure the `memtrace` entry above once. Do not add a second
entry for `memcortex-mcp`, manually link a bundled Node path, or set a
socket/named-pipe flag. The internal Cortex sidecars and OS transport are
managed by Memtrace.

Run `memtrace start` before using Cortex so its decision-capture service
and local endpoint are available. If the MCP client was already open,
restart or reconnect it after starting Memtrace. The five Cortex tools,
including `recall_decision`, should then appear in the ordinary Memtrace
tool list.

For status checks, governance confirmation, and the difference between
code reindexing and decision memory, see [`cortex.md`](cortex.md).

## Streamable-HTTP transport

### How it looks

```
   ┌─────────────────────┐
   │  workspace owner    │ ◄────── optional existing loopback
   │  (start, daemon,    │
   │   or HTTP mcp)      │
   │  heavy state:       │
   │   MemDB, models,    │
   │   indexes           │
   └──────────┬──────────┘
              │
              │ in-process when HTTP mcp is the owner,
              │ loopback when it attaches to another owner
              ▼
   ┌─────────────────────┐
   │  memtrace mcp       │
   │  MEMTRACE_TRANSPORT │  ◄── ONE long-lived process
   │  =streamable-http   │       Listens on http://localhost:4848/mcp
   │  MEMTRACE_PORT=4848 │
   └──────────┬──────────┘
              │ HTTP JSON-RPC
              │ (session id in header)
              ▼
   ┌─────────────────────────────────────────────────┐
   │  Your orchestrator / proxy / dashboard          │
   │  (Orbit, Memtrace UI, custom MCP gateway, etc.) │
   └─────────────────────────────────────────────────┘
              │
              ▼
   ┌─────────┬─────────┬─────────┬─────────┐
   │ Agent A │ Agent B │ Agent C │ Agent D │ … many concurrent
   └─────────┴─────────┴─────────┴─────────┘
```

One `memtrace mcp` HTTP process. One HTTP endpoint. Many concurrent
agent sessions, each with their own session id in the request header.
That HTTP process can own the workspace itself or attach to an
already-running owner.

### Setup

Two env vars on the `memtrace mcp` process:

```bash
MEMTRACE_TRANSPORT=streamable-http MEMTRACE_PORT=4848 memtrace mcp
```

You should see:

```
[memtrace] MCP streamable-HTTP transport listening on http://0.0.0.0:4848/mcp
```

Test it works:

```bash
curl -X POST http://localhost:4848/mcp \
  -H 'Content-Type: application/json' \
  -H 'Mcp-Session-Id: test-1' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
```

A successful response means the transport is up.

### Aliases for compatibility

The MCP spec deprecated plain "SSE" in favour of "streamable HTTP"
(same wire shape, more robust session model). For back-compat,
Memtrace accepts:

| `MEMTRACE_TRANSPORT` value | What it does |
|---|---|
| `streamable-http` | Modern canonical name. Use this. |
| `http` | Short alias for `streamable-http`. |
| `sse` | Legacy alias — also binds streamable-HTTP. Logs a one-line deprecation hint pointing at the new name. |
| `stdio` | Default. Child-process JSON-RPC. |
| (anything else) | Hard error since v0.3.32 — Memtrace refuses to start rather than silently fall back to stdio. |

### Connecting an agent in HTTP mode

Most clients support HTTP MCP via a slightly different config entry:

```json
{
  "mcpServers": {
    "memtrace": {
      "url": "http://localhost:4848/mcp"
    }
  }
}
```

(Exact key may vary per client — Cursor uses `url`, some others use
`type: "http"` + `endpoint`.) Per-client docs evolve quickly; consult
your AI tool's MCP setup guide for the current shape.

### Operational notes

- **HTTP API binds `127.0.0.1` by default** (since 0.6.0). Set
  `MEMTRACE_UI_HOST=0.0.0.0` only when you intentionally need
  remote/VM/container exposure. MCP HTTP transport (`MEMTRACE_PORT`)
  bind behaviour is separate — see your orchestrator setup.
- **Port collisions.** If `MEMTRACE_PORT` is already in use, you'll
  get a clear error pointing at the env var. Pick a free port.
- **Auth.** The HTTP transport doesn't enforce auth at the MCP layer —
  if you're exposing it, put it behind a proxy (Cloudflare tunnel,
  Caddy, nginx) that enforces whatever access control you want.
- **Session lifecycle.** The server keeps in-memory state per
  `Mcp-Session-Id` header. Clean up on the client side when an
  agent disconnects; the server also reaps stale sessions on a
  timer.

## Long-running server pattern (orchestrators)

If you're building a platform on top of Memtrace (orchestrator,
dashboard, agent fleet), the layout you want is:

1. **One workspace owner per indexed workspace.** This can be
   `memtrace start`, a headless daemon service, or the HTTP
   `memtrace mcp` process itself.
2. **One `memtrace mcp` HTTP endpoint per host/workspace** with
   `MEMTRACE_TRANSPORT=streamable-http` and a stable
   `MEMTRACE_PORT`.
3. **Your platform proxies to that endpoint**, multiplexing many
   concurrent agent sessions through it via session ids.

Why not "one HTTP `memtrace mcp` per agent session"? You would pay
startup overhead per session and make ownership more complex. With
one HTTP endpoint, the heavy state is loaded once.

Why not "spawn `memtrace mcp` per call"? You'd pay startup on every
single tool call. Even worse than per-session — please don't.

## Building from source with a non-default transport

The streamable-HTTP transport is **on by default** in the npm release
since v0.3.32. If you're building from source and want the smaller
stdio-only binary:

```bash
cargo install --git https://github.com/syncable-dev/memtrace-public \
    --no-default-features
```

This drops the streamable-HTTP code path. The binary will refuse to
start with `MEMTRACE_TRANSPORT=streamable-http` (clear error message),
but stdio works.

## Common questions

### "I see streamable-HTTP, but my legacy code uses `sse`. Will it break?"

No. `sse` is accepted as an alias and maps to the same code path. You
get a one-line deprecation hint logged to stderr; nothing else
changes.

### "Can I run multiple `memtrace start` daemons on one host?"

Yes, but not for the same `.memdb` directory. Memtrace uses an
owner lock inside `.memdb`, so a second owner for the same workspace
attaches or refuses rather than opening the embedded store again.

Separate workspaces are fine. Give each one its own
`MEMTRACE_MEMDB_DATA_DIR`; set `MEMTRACE_MEMDB_LOOPBACK_PORT` only
when you need stable, non-default loopback ports.

### "Can the same `memtrace mcp` process serve both stdio and HTTP?"

No. You pick one transport per process. You can run two `memtrace mcp`
processes — one stdio (for your local agent) and one HTTP (for your
orchestrator). One may own the workspace; the other attaches.

### "Does the stdio child stay running between tool calls within a session?"

Yes. The child stays alive for the lifetime of the agent session. The
agent reuses the open stdio pipe across many `find_code` / `find_symbol`
/ etc. calls. So per-tool-call cost is either an in-process graph call
or a localhost loopback roundtrip to the owner.

### "What happens if the owner dies while an MCP session is open?"

An attached MCP process gets a loopback connection error and returns
a clean error to the agent. Start Memtrace again, or restart the
agent session so a fresh `memtrace mcp` can become the owner. No
graph data is lost — the graph is on disk, not in process memory.
