---
title: "Sourcegraph MCP: How to Set It Up Fast"
description: "Learn how to set up sourcegraph mcp fast, from enabling the server and configuring access tokens to connecting your AI coding client in minutes."
ogTitle: "Sourcegraph MCP Setup Guide: Get Connected Fast With This Step by Step Walkthrough"
ogDescription: "Set up Sourcegraph MCP in one sitting. This guide covers server enablement, access token configuration, and client setup for Enterprise and Cloud users."
date: 2026-10-21
image: ./sourcegraph-mcp-setup-guide.png
imageAlt: "Step by step Sourcegraph MCP server setup showing configuration file with server URL and access token fields"
---

# Sourcegraph MCP: How to Set It Up Fast

Sourcegraph MCP lets AI coding agents query your codebase through the Model Context Protocol. To set it up, enable the MCP server in your Sourcegraph instance, generate or configure an access token, then add the server URL and token to your client's MCP config file. The steps below cover every decision point, from server enablement to client configuration, so you finish in one sitting.

## What Is the Sourcegraph MCP Server?

The Sourcegraph MCP server exposes your Sourcegraph instance as an MCP endpoint. AI clients, such as Claude Desktop or a VS Code extension, connect to that endpoint and can search code, read files, and query symbols without you manually copying context into a chat window. Sourcegraph's Dan Adler reported that usage of the server grew 560% over a six-month period in his 2026 LinkedIn post.

The official server ships as part of Sourcegraph itself. It exposes a single HTTP endpoint at `/.api/mcp` on your instance domain. You enable it through the site admin configuration, and it uses your existing Sourcegraph auth layer for access control.

There is also a separate, community-built package called `sourcegraph-mcp` on PyPI. It is an independently maintained Python implementation, not an official Sourcegraph product. The two are not interchangeable. The community package's own documentation reports that symbol lookups complete in under 100ms per indexed search, and that an indexed search uses roughly ~400 tokens compared to loading entire files. Those figures come from the community implementation's benchmarks, not from Sourcegraph's official server.

Choose the official server if you are running Sourcegraph Enterprise or Sourcegraph Cloud and want a supported, admin-controlled integration. Use the community package only if you need a lightweight Python-native client and accept that it sits outside Sourcegraph's support boundary.

## Is Sourcegraph MCP Available for All Users or Just Enterprise?

The official Sourcegraph MCP server is available on both Sourcegraph Enterprise and Sourcegraph Cloud. It is not limited to Enterprise-only plans. However, access is disabled by default on every instance and must be enabled by a site admin before any client can connect.

Once enabled, access control works at two levels. First, the admin can turn the feature on or off instance-wide. Second, individual user access follows the same permission model as the rest of Sourcegraph: a user can only retrieve code through the MCP endpoint that they could already access through the Sourcegraph UI. No additional permission grants are required, and no separate MCP-specific role exists.

If you want to restrict MCP access to a specific group of users, the practical approach is to control which users hold valid access tokens or have completed the OAuth flow. Revoking a token or blocking OAuth for a user account removes that user's MCP access without affecting anyone else.

Sourcegraph Cloud users connect to the shared cloud endpoint. Self-hosted Enterprise users connect to their own instance URL. The configuration steps in both cases are the same, with the instance URL being the only variable that differs.

## How to Enable the Sourcegraph MCP Server as an Admin

The MCP server is off by default. A site admin must enable it before any client can reach the endpoint.

1. Sign in to your Sourcegraph instance as a site admin.
2. Open **Site admin** from the user menu, then navigate to **Site configuration**.
3. Locate the `experimentalFeatures` block in the JSON config, or add it if it does not exist.
4. Add `"mcpServer": { "enabled": true }` inside `experimentalFeatures` and save the configuration.
5. Verify the endpoint is live by making a GET request to `<your-instance-domain>/.api/mcp` (replace `<your-instance-domain>` with your actual Sourcegraph URL). A valid response confirms the server is accepting connections.

One limit applies once clients start reading files: the official Sourcegraph MCP server truncates any file larger than 128KB to its first 200 lines. Agents working with large generated files or minified assets will only see the top of those files. Plan your agent's workflow around this if your repo contains files that regularly exceed that size.

Field names in the site configuration have changed across Sourcegraph releases. If `mcpServer` is not recognized, check your instance's release notes for the current key name before assuming the feature is unavailable.

## Authentication Options: OAuth 2.0 vs. Access Tokens

Two auth methods connect an MCP client to Sourcegraph: OAuth 2.0 and static access tokens. Pick one before you touch any client config file.

**OAuth 2.0** uses the standard authorization code flow. Your MCP client redirects to Sourcegraph, the user logs in, and Sourcegraph issues a short-lived token that the client refreshes automatically. This is the better choice for interactive clients like Claude Desktop, where a browser window is available and you want tokens to rotate without manual work. It also means you never store a long-lived secret in a config file on disk.

**Static access tokens** are personal access tokens (PATs) you generate once in Sourcegraph and paste into your client config. They do not expire unless you revoke them. This makes them practical for headless or automated agents, CI pipelines, or any environment where a browser redirect is not possible. The trade-off is that the token sits in a config file, so you need to protect that file and rotate the token on a schedule.

To generate a static token, open your Sourcegraph user settings, navigate to **Access tokens**, and create a new token with the `user:all` scope. Copy it immediately; Sourcegraph will not show it again.

A few practical rules for choosing:

- Use OAuth 2.0 for desktop clients where a human is present at setup time.
- Use a static token for agents running in CI or in a container with no browser access.
- Never commit a static token to version control. Store it in an environment variable or a secrets manager and reference it from your config.

Both methods use the same MCP endpoint URL. Authentication is the only thing that differs between them.

## How to Configure the JSON for Claude Desktop and Claude Code

Claude Desktop and Claude Code both read from a JSON config file that lists MCP servers. You add Sourcegraph as an entry in that file, restart the client, and the connection is live.

Follow these steps:

1. Locate your MCP config file. On macOS, Claude Desktop reads from `~/Library/Application Support/Claude/claude_desktop_config.json`. Claude Code reads from `.claude/settings.json` in your project root, or from the global config at `~/.claude/settings.json`.
2. Open the file in a text editor. If it does not exist, create it.
3. Add a `mcpServers` block (or add an entry to an existing one) using this structure:

```json
{
  "mcpServers": {
    "sourcegraph": {
      "url": "<your-instance-domain>/.api/mcp",
      "headers": {
        "Authorization": "token <your-access-token>"
      }
    }
  }
}
```

Replace `<your-instance-domain>` with your Sourcegraph instance URL, for example `sourcegraph.example.com`. Replace `<your-access-token>` with the PAT you generated in the previous section.

4. If you are using OAuth 2.0 instead of a static token, omit the `headers` block. Claude Desktop will open a browser window on first connection and complete the OAuth flow automatically.
5. Save the file and fully restart the client. A reload or refresh is not enough; the MCP server list is read at startup.
6. Confirm the connection by asking the client to search for a function in your repo. A result that names a file and line number confirms the endpoint is working.

**Field names to know:**

- `url`: the full MCP endpoint URL, including the `/.api/mcp` path.
- `headers.Authorization`: the bearer token, using the `token` prefix Sourcegraph expects, not `Bearer`.
- The top-level key is `mcpServers` in Claude Desktop's format. Claude Code uses the same key inside its settings structure.

The community `sourcegraph-mcp` package on PyPI uses a different config format. If you are using that package, its own documentation covers the setup. The JSON above applies to the official Sourcegraph MCP server only.

The community package's own documentation notes that indexed search uses roughly ~400 tokens per query. The official server's token behavior is not separately benchmarked, but the same principle applies: symbol and search queries cost far less context than file reads.

## Setting Up Sourcegraph MCP in VS Code and JetBrains

IDE-integrated MCP clients follow the same pattern as Claude Desktop but write config in different locations.

**VS Code**

1. Install an MCP-compatible extension, such as the Continue extension or another agent extension that supports MCP servers.
2. Open the extension's settings file. For Continue, this is `.continue/config.json` in your project root or `~/.continue/config.json` globally.
3. Add Sourcegraph under the `mcpServers` key using the same URL and token structure shown in the Claude Desktop section above.
4. Reload the VS Code window with **Developer: Reload Window** from the command palette. The extension picks up the new server on reload.
5. Run a test query from the extension's chat panel. Ask it to find a specific function by name. A response with file paths confirms the connection.

**JetBrains IDEs**

1. Install an MCP-compatible plugin from the JetBrains Marketplace. Plugins that support the MCP protocol typically store their config in the IDE's settings directory.
2. Open the plugin's configuration panel under **Settings / Preferences**.
3. Add a new MCP server entry. Paste your Sourcegraph instance URL with the `/.api/mcp` path as the endpoint, and add your access token in the authorization field.
4. Apply the settings and restart the IDE.
5. Confirm by running a symbol search through the plugin's interface.

Both IDEs support static token auth without any extra steps. OAuth 2.0 support depends on the specific plugin; check the plugin's documentation before choosing that auth path. The Sourcegraph MCP server itself does not care which IDE is calling it, only that the request carries a valid token and hits the correct endpoint.

## When Your AI Agent Still Lacks Deep Code Context: Adding a Local Code Graph

Sourcegraph MCP gives your agent search, file reads, and symbol lookup. What it does not provide is structural context: which functions call this one, what tests cover it, and what breaks if you change its signature. That gap shows up when an agent edits code confidently, then misses a caller three directories away.

This is where a local code graph fills in. [Memtrace](https://memtrace.io/) is a local code graph tool that maps functions, callers, APIs, and tests across your repository. Before your agent makes an edit, it can query that graph to understand blast radius, trace execution paths, and recall why a function was written a certain way. Memtrace connects to your existing coding tools via MCP, so it sits alongside Sourcegraph MCP rather than replacing it.

The two work at different layers. Sourcegraph MCP handles search and retrieval across your instance. Memtrace answers structural questions about a specific function or call chain. Together, they give your agent both the breadth to find relevant code and the depth to understand what changing it will affect.

If your agent is returning search results but still making edits that break unrelated callers, adding a [local structural memory layer](https://memtrace.io/codemap) is the next step.

Prices and plan limits verified as of October 2026.

## FAQs

### Does Sourcegraph MCP work with Sourcegraph Cloud or only self-hosted instances?

Sourcegraph MCP works with both Sourcegraph Cloud and self-hosted Enterprise instances. The setup steps are identical. The only difference is the instance URL you paste into your client config. Cloud users point to their assigned Sourcegraph Cloud domain; self-hosted users point to their own instance. Access control in both cases follows the same permission model as the Sourcegraph UI.

### Can I use the community Python sourcegraph-mcp package instead of the official server?

You can, but the two are not interchangeable. The community `sourcegraph-mcp` package on PyPI is an independently maintained Python implementation. It is not an official Sourcegraph product and sits outside Sourcegraph's support boundary. Its own documentation reports symbol lookups complete in under 100ms per indexed search. Use it if you need a Python-native client and accept that distinction.

### How do I rotate or revoke an access token used by an MCP client?

Open your Sourcegraph user settings, navigate to **Access tokens**, and delete the token you want to revoke. Sourcegraph invalidates it immediately. Then generate a new token, update the `Authorization` header in your client's MCP config file, and restart the client. If multiple clients share the same token, update each config file before restarting them.

### What file size limits apply when the MCP server reads source files?

The official Sourcegraph MCP server truncates any file larger than 128KB to its first 200 lines. Your agent will not see the rest of that file in a single read. If your repo contains large generated files, minified assets, or vendored code that regularly exceeds 128KB, design your agent's workflow to work with the truncated output or break those files into smaller modules.

### Can multiple AI clients share the same Sourcegraph MCP endpoint simultaneously?

Yes. The MCP endpoint at `/.api/mcp` on your Sourcegraph instance handles concurrent connections. Each client authenticates independently using its own token or OAuth session. One client's queries do not affect another's. The only shared constraint is your Sourcegraph instance's overall rate limits and resource capacity, which apply to all API traffic, not just MCP.

## Conclusion

Sourcegraph MCP connects AI coding agents to your codebase through a standard protocol. Enable the server in site config, choose OAuth 2.0 or a static token based on your environment, then add the endpoint URL and credentials to your client's config file. The official server works on both Sourcegraph Cloud and self-hosted Enterprise, and the same JSON structure applies across Claude Desktop, Claude Code, VS Code, and JetBrains.

Search and file retrieval get your agent most of the way there. If it still misses caller chains or blast radius after you have the MCP connection working, check whether a local code graph fits your workflow. Start with the auth method that matches your environment, get one client connected first, then expand from there.
