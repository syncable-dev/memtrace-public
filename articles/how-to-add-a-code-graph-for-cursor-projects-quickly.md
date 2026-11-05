---
title: "How to Add a Code Graph for Cursor Projects Quickly"
description: "Learn how to add a code graph for Cursor projects using CodeGraph, Graft, or GitNexus to give your AI agent fast structural context via MCP."
date: "2026-11-03"
image: "./how-to-add-a-code-graph-for-cursor-projects-quickly.png"
imageAlt: "A developer wiring a local code graph tool into Cursor AI settings to enable structural MCP queries across a repository"
ogTitle: "How to Add a Code Graph for Cursor Projects Quickly and Boost AI Context"
ogDescription: "Discover how to wire a local code graph for Cursor using CodeGraph, Graft, or GitNexus. Cut tool call overhead and keep AI context accurate across your entire repo."
---

# How to Add a Code Graph for Cursor Projects Quickly

To add a code graph to Cursor, pick a local graph tool (CodeGraph, Graft, or GitNexus), install its CLI, wire its MCP server into Cursor's settings, and start the server. Cursor's AI agent then queries the graph instead of grepping raw files, cutting unnecessary tool calls and keeping context accurate across your whole repository. Each tool follows a standard CLI pattern, so the wiring step is straightforward once you have the right commands.

A dedicated local code graph exposes structural context through MCP, so the agent knows what a change will touch before it touches anything.

## Does Cursor Have a Built-In Code Graph?

Cursor indexes your repository server-side, but that index is not a structural code graph. It does not map which functions call which, which tests cover a given module, or how a change in one file propagates across the codebase.

That distinction matters in practice. When Cursor's agent needs to understand a change's scope, it falls back to reading files directly. Those file reads consume context window tokens and add tool call overhead. [According to DX research](https://getdx.com/uploads/ai-assisted-engineering-q4-impact-report.pdf), developers using AI coding assistants save 3.6 hours per week on average, but that gain depends on the agent spending its context budget on reasoning rather than discovery. The [2025 Stack Overflow survey](https://survey.stackoverflow.co/2025/ai) found that 51% of professional developers now use AI tools every day, which means the cost of a slow or context-hungry agent compounds quickly.

A local structural graph solves this by pre-computing the relationships Cursor's index does not store. It answers questions like "what calls this function?" or "which files import this module?" in a single query, without reading any source file at runtime. Connecting that graph to Cursor via MCP gives the agent a dedicated lookup path that is faster and more token-efficient than raw file search.

Cursor's server-side index and a local code graph are not interchangeable. They answer different questions. The index finds text; the graph maps structure.

## Comparing Code Graph Tools for Cursor: CodeGraph, Graft, and GitNexus

Three tools cover most use cases for adding a structural code graph to Cursor. Each builds the graph differently, exposes it through MCP (or hooks), and suits a different kind of project.

| Tool | How it builds the graph | MCP support | Search type | Pricing tier |
|------|------------------------|-------------|-------------|--------------|
| CodeGraph | Indexes codebase structure into a local SQLite/FTS5 database using a Rust kernel | Yes, 26 MCP tools | Full-text structural search | Not publicly listed |
| Graft | Builds a tree-sitter-derived map as linked markdown files, uses hooks to inject matching nodes into prompts | Yes, plus prompt hooks | Node-based context injection | Not publicly listed |
| GitNexus | Transforms the repository into an interactive graph with hybrid BM25 plus semantic search | Yes, 17 MCP tools | Hybrid (BM25 + semantic) | Not publicly listed |

**CodeGraph** indexes your codebase into a local SQLite database with a full-text search layer (FTS5) and exposes 26 MCP tools that answer architectural questions directly. The Rust kernel keeps indexing fast. One operational detail: you must keep a `codegraph serve` terminal running for the MCP connection to stay active. Tosea.ai reports that CodeGraph leads to 70% fewer tool calls for coding agents during a typical session, though that figure comes from the tool's own vendor documentation, so treat it as a reported benchmark rather than independent research.

**Graft** takes a different approach. It uses tree-sitter to parse the repository and stores the resulting map as linked markdown files, which are git-ignored and regenerable at any time. Rather than relying solely on MCP, Graft uses hooks to inject matching graph nodes directly into agent prompts, which means the model receives the structural context whether or not it explicitly queries for it. Controlled testing across 162 runs showed Graft reduces token usage by 42% and tool calls by 46% compared to a baseline agent.

**GitNexus** builds an interactive repository graph with hybrid search combining BM25 keyword matching and semantic vector search, and it includes blast radius analysis out of the box. The 17 MCP tools cover a wide range of architectural queries. GitNexus requires an enterprise license for commercial work, so confirm that before adopting it for a production codebase.

For most Cursor users starting out, CodeGraph or Graft is the faster path. CodeGraph suits teams that want a broad set of MCP tools and fast local queries. Graft suits developers who want the graph context injected automatically without relying on the agent to ask for it. GitNexus fits larger organizations that need hybrid search and are prepared to manage licensing.

## How to Add a Code Graph for Cursor: Step-by-Step Setup

The steps below use CodeGraph as the example because it connects to Cursor purely through MCP and its setup follows a standard CLI pattern. Exact commands vary by tool, so check the documentation for Graft or GitNexus if you use either of those instead.

1. **Install the CLI globally.** Run the CodeGraph installer via npm or the provided shell script. After installation, confirm the binary is on your PATH by running `codegraph --version`.

2. **Initialize the graph in your repository.** Navigate to your project root and run the initialization command. CodeGraph scans the directory, builds the SQLite/FTS5 index, and writes a local configuration file. The index is stored locally and does not leave your machine.

3. **Wire the MCP server into Cursor's settings.** Open Cursor's settings and go to the MCP section. Add a new server entry pointing to the CodeGraph MCP endpoint, typically `http://localhost` on the port the CLI printed during initialization. Check the CodeGraph documentation for the exact format. Save the configuration. This step tells Cursor where to send graph queries.

4. **Start the local MCP server and keep it running.** Run `codegraph serve` in a dedicated terminal. This process must stay running for the MCP connection to remain active. If you close the terminal, Cursor loses the graph connection. A process manager or a persistent terminal session keeps it alive across work sessions.

5. **Confirm the connection inside Cursor.** Open Cursor's MCP settings panel and check that the CodeGraph server shows a connected status. You can also send a test query from Cursor's agent, such as asking it to list the callers of a specific function. If the agent returns a structured answer rather than reading a file, the graph is working.

Once the connection is confirmed, the agent routes structural queries through the graph automatically. You do not need to change how you prompt Cursor for the basic setup to work, though the next section covers editor rules that make the agent prefer graph queries over file reads in every session.

Store the MCP config in a shared dotfile to avoid repeating setup across machines.

## How Do I Force Cursor to Use My Code Graph Instead of Grepping Files?

Cursor's agent defaults to reading files when it needs structural information. Adding a code graph via MCP does not automatically change that behavior. You need editor rules that tell the agent to query the graph first.

The most direct method is a `.cursorrules` file at your project root. Add a rule that instructs the agent to call a graph tool before opening any file for structural questions. A working pattern looks like this:

```
Before reading any source file to answer a structural question,
call the code graph MCP tool first. Use file reads only when
the graph returns no result.
```

Keep the rule short and specific. Name the MCP tool explicitly, for example `codegraph_query`, so the agent has no latitude to skip it.

For Claude-based sessions in Cursor (code graph for cursor claude), the same `.cursorrules` file applies. Claude follows system-level instructions reliably, so a rule that names the graph tool by its MCP identifier steers it consistently. Codegraph Claude Code sessions benefit from adding a second rule that restricts blast radius checks to graph queries only, since Claude tends to grep for callers when no rule prevents it.

Graft handles this differently. Its hook system injects matching graph nodes into the prompt before the agent acts, so the agent receives structural context without needing a rule to ask for it. If rule-based steering proves unreliable with CodeGraph, Graft's injection approach is worth testing. Tosea.ai's vendor data reports 70% fewer tool calls with CodeGraph when rules are in place, which suggests the rules do meaningful work, though that figure reflects ideal conditions.

Check your session logs after a few prompts. If you see file-read tool calls before graph calls, tighten the rule wording. If the graph returns results and the agent still opens files, add a second rule that explicitly forbids redundant file reads when a graph result exists.

## Performance Benefits: Token Savings and Fewer Tool Calls

A code graph reduces two costs that compound across a long agent session: tokens spent on discovery and tool calls spent reading files. Both matter because each file read consumes context window space the agent cannot use for reasoning.

Controlled testing of Graft across 162 runs showed a 42% reduction in token usage and 46% fewer tool calls compared to a baseline agent without a graph. Those numbers reflect Graft specifically, not code graph tools in general. Results will vary by tool, repository size, and the kinds of questions you ask most often.

The directional signal is consistent across both tools: structural queries answered by a graph cost fewer tokens than the same questions answered by file reads. Fewer tool calls also mean the agent spends more of its context budget on reasoning rather than discovery.

The downstream effect is a more useful agent. When the context window is not filled with file contents, the agent has more room to reason about the change it is making. [DORA's 2025 report](https://www.thoughtworks.com/content/dam/thoughtworks/documents/report/tw_report_state_of_ai_assisted_software_development_2025.pdf) found that over 80% of technology professionals using AI report productivity gains, with 90% of technology professionals using AI in their work. Keeping the agent's context budget on reasoning rather than discovery is one concrete way to stay in that productive majority.

For teams running many agent sessions daily, the savings accumulate. Fewer tool calls also mean shorter response times, since each skipped file read removes a round trip from the agent loop.

## When a Continuously Maintained Graph Is the Better Choice

CodeGraph, Graft, and GitNexus all build their graphs at index time. If your codebase changes frequently, that means re-indexing manually to keep the graph accurate. For a small or stable project, that trade-off is reasonable. For active repositories where functions are added, renamed, or moved between files regularly, a stale graph returns wrong answers, and a wrong answer about blast radius is worse than no answer.

Most tools treat graph building as a one-shot operation. The graph reflects the codebase at the moment you ran the indexer, not the moment the agent queries it.

A continuously maintained graph updates as the codebase evolves, so the agent always queries current structure. This matters most in three situations: active feature branches where function signatures change daily, monorepos where a change in one package affects callers in several others, and teams where multiple developers push to the same repository throughout the day.

[Memtrace](https://memtrace.io/) is a local code graph tool that connects functions, callers, APIs, and tests across a repository and keeps the graph current as the codebase evolves, eliminating the manual re-index step. It connects to Cursor via MCP; confirm the integration details in its documentation before adopting it. For teams that need the graph to reflect the current state of the repository at query time, that continuous maintenance removes a failure mode the one-shot tools leave open.

If your repository sees fewer than a handful of structural changes per day, manual re-indexing with CodeGraph or Graft is probably sufficient. If your team pushes frequently and the agent's accuracy depends on up-to-date caller chains, a maintained graph is the more reliable choice.

## Hardware and Repository Size Considerations

Local graph indexing runs on your machine, so available RAM and disk speed affect how long the initial index takes and how quickly queries return. For most repositories under a few hundred thousand lines, a modern laptop handles indexing without issue. Larger monorepos take more time and more disk space for the index files. GitNexus handles large repositories and includes blast radius analysis, but check its licensing requirements before using it commercially. Keep the index on an SSD if query latency matters, and watch available disk space as the index grows.

Prices and plan limits verified as of November 2026.

## FAQs

### Can I use a code graph with Cursor's Claude integration?

Yes. Claude follows system-level instructions reliably, so a `.cursorrules` file that names your graph tool's MCP identifier steers it to query the graph before reading files. The same setup works for any Claude-based session inside Cursor, including Claude Sonnet and Opus model selections.

### Does a local code graph work with monorepos?

It depends on the tool. Check each tool's documentation for how it handles monorepo roots, since coverage depends on where you initialize the indexer. For monorepos where a change in one package affects callers in several others, a continuously maintained graph gives more reliable blast radius answers than a one-shot index that may be hours out of date.

### Will the code graph slow down Cursor while it indexes?

The main operational cost is keeping the MCP server process running in a dedicated terminal. Check the tool's documentation for details on indexing behavior and query performance for your repository size.

### Can I use more than one code graph tool at the same time in Cursor?

You can register more than one graph tool in Cursor's MCP settings. In practice, running two graph servers at once means the agent may call either one depending on how your rules are written. For predictable routing, name each tool's MCP identifier explicitly in your `.cursorrules` file and assign each one to specific query types. Running duplicate graphs of the same repository wastes resources without adding accuracy.

### Does the code graph store my source code in the cloud?

The tools covered in this article, CodeGraph, Graft, and GitNexus, are described as building their indexes locally. Confirm the specific tool's architecture in its documentation to verify that your source code does not leave your machine during indexing or querying. Memtrace also builds its graph locally. The MCP connection runs between your local graph server and Cursor, not through an external service. If your organization has strict data residency requirements, confirm the specific tool's architecture in its documentation before adopting it.

## Conclusion

Adding a structural code graph for Cursor gives the AI agent a direct path to function relationships, caller chains, and blast radius information without reading source files at runtime. The right tool depends on your workflow: CodeGraph and Graft suit most individual developers, GitNexus fits teams that need hybrid search, and a continuously maintained graph like Memtrace suits active repositories where the codebase changes throughout the day.

Pick one tool, follow the four setup steps to wire its MCP server into Cursor, and add a `.cursorrules` file that names the graph tool explicitly. Run a few agent sessions and check the logs to confirm graph queries are replacing file reads. That single check tells you whether the setup is working and where to tighten the rules if it is not.
