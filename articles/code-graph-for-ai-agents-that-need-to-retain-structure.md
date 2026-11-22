---
title: "Code Graph for AI Agents That Need to Retain Structure"
description: "Compare a code graph for AI agents on local versus cloud indexing, MCP support, and retained structure, then pick the right fit for your codebase."
ogTitle: "Code Graph for AI Agents: Four Tools Compared on Retained Structure"
ogDescription: "A code graph for AI agents stores symbols, calls, imports and routes so agents stop rescanning files. See how four tools compare on indexing, MCP support and retention."
date: 2026-11-04
image: ./code-graph-for-ai-agents-that-need-to-retain-structure.png
imageAlt: "Comparison table of code graph tools showing where each runs and what it indexes for agents"
---

# Code Graph for AI Agents That Need to Retain Structure

A code graph for AI agents is a persistent index of symbols, calls, imports and routes that an agent queries instead of scanning files. Several options cover the space, and they differ in local versus cloud indexing, MCP support, and how much structure they retain between sessions.

## What is a code graph for AI agents and why does it matter?

A code graph is a stored map of how a repository fits together: which functions call which, which modules import which, which routes hit which handlers, and which tests cover what. An agent queries that map instead of opening files and guessing.

Without it, every session starts from zero. The agent greps, reads a dozen files, builds a mental model, then loses it when the session ends. That costs tokens and invites missed callers.

The demand is already there. In the 2025 Stack Overflow survey, 51% of professional developers said they use AI tools daily. Jellyfish platform data shows AI code assistant adoption rose from 49.2% in January to 69% in October 2025. More agents editing more code means more edits made blind to what else a change touches.

Structure helps accuracy, not only speed. A 2026 software engineering study found that adding lightweight call and inheritance topology improved an agent's function-level localization by 2.2 percentage points. That result covers a simple topology layer, not any single product. Still, it points one way: callers and inheritance are signal an agent cannot reliably recover from raw text.

The rest of this article compares four tools, shows how to connect one to your agent, then gives you a way to choose.

## Code graph tools compared at a glance

The tools below differ most on where they run, what they index, and whether they separate facts from guesses. The table summarizes the three with published tool details. Memtrace is a local code graph tool that links functions, callers, APIs, and tests for AI coding agents, and it has its own section later.

| Tool | Runs | What it indexes | Edge provenance | Cost tier |
|---|---|---|---|---|
| CodeGraph | Desktop app, local | Symbols, calls, imports, routes in a SQLite graph | Parsed from source | A paid plan |
| GitNexus | Desktop app | Repository graph with hybrid search and cross-repo service links | Parsed from source | Pricing not publicly listed |
| Graphify | Desktop app | Code, docs and wikis across repositories, 33+ tree-sitter grammars | Labeled extracted vs. LLM-inferred | Pricing not publicly listed |

License terms also differ. CodeGraph is MIT licensed, Graphify is Apache-2.0, and GitNexus uses the PolyForm Noncommercial license. Check that last one before you adopt it at work.

Every option here ships code graph context to your agent over MCP, so the real differences are retention and trust. Ask what each tool keeps between sessions and how it flags the edges it is sure about. Wider context only helps when the structure is current.

## 1. CodeGraph: local SQLite graph for precise structural queries

CodeGraph parses a repository into a local SQLite knowledge graph of symbols, calls, imports and routes. Your agent asks the graph a structural question and gets a precise answer, not a pile of files to read.

**What it retains.** The graph stores definitions and the relationships between them. An agent can ask who calls a function or which routes reach a handler without opening each file.

**Setup.** CodeGraph provides an MCP server and a browser viewer, which is in preview. You point your agent at the server and it queries the graph from inside its normal loop. The code lives in a public GitHub repository, so you can inspect it before you commit. Similar graph-RAG projects on GitHub share the same shape: parse locally, store relationships, expose a query interface.

**Fit.** CodeGraph suits a solo developer or small team that wants a local, MIT-licensed graph and does not need cross-repository tracing.

**Watch for.** Telemetry is enabled by default, with an opt-out. If you work on private code, turn it off before the first index. It is also listed as a paid plan; confirm current terms on the project's own pages.

## 2. GitNexus: impact analysis and cross-repo tracing over MCP

GitNexus runs a LadybugDB-backed engine that combines hybrid search, impact analysis, and cross-repository service tracking for coding agents. It answers "what breaks if I change this" across repository boundaries, not only inside one.

**What it adds.** The server exposes 17 MCP tools covering impact analysis and change detection. Your agent can request the affected symbols before an edit, then check which services depend on the code it touched. That cross-repo view is the main reason to pick it over a single-repo graph.

**Traction.** By August 2026, the GitNexus MCP server reached 45.7K stars on GitHub, which put it among the leaders in the code intelligence category. Stars measure interest, not accuracy. Read that number as proof the tool has users and an active community, nothing more.

**Fit.** GitNexus suits teams with several services that call each other. If Sourcegraph felt heavy and you want something an agent can drive directly over MCP, this is the closest match on the list.

**Watch for.** GitNexus uses the PolyForm Noncommercial license. That restricts commercial use, so read the terms before you wire it into company work. Pricing is not publicly listed, which means you need to ask before budgeting for a team rollout.

## 3. Graphify: persistent cross-repository graph with labeled edges

Graphify builds a persistent graph across repositories and separates relationships it extracted from source from relationships an LLM inferred. That label matters: an extracted call edge is a fact, an inferred edge is a guess.

**What it retains.** Graphify supports over 33 tree-sitter grammars and spans code, docs, and wikis. Your agent can trace from a design note to the function that implements it, then to its callers. The graph keeps provenance on every edge, so the agent can weigh a parsed call differently from a suggested link.

**Reported results.** MemVerge's Senior Product Manager Steve Scargall reported 79x token reductions with Graphify compared to previous methods. That figure is vendor-reported and comes from a single case study. Nobody has independently reproduced it, and it may not hold on your repo. Models can also ignore graph tools unless the prompt tells them to call them, so an installed graph is not the same as a used one.

**Fit.** Graphify suits teams whose context lives in more than code: docs, wikis, and several repos. The extracted versus inferred split gives you something to audit.

**Watch for.** Extraction precision varies by language, and published comparisons between, say, C++ and Python are thin. Run it on your own repo and spot-check edges. It ships under Apache-2.0, and pricing is not publicly listed.

## 4. Memtrace: persistent structure that answers what a change touches

[Memtrace](https://memtrace.io/) builds and maintains a local code graph that connects functions, callers, APIs, and tests across a repository. Your agent queries it before the next edit to see the blast radius, trace execution paths, and recall why code was written a certain way.

**What it retains.** Callers, APIs, tests, and history stay linked in one graph. A file scan returns text. The graph returns relationships. It is [persistent structural memory for AI coding agents](https://memtrace.io/codemap).

**Setup.** Memtrace connects to your existing coding tools over MCP and keeps the graph current as the repo changes. The graph is local, so your code stays on your machine. That answers the common question of whether you can use a code graph without sharing source with a third party.

**Fit.** Memtrace suits developers and teams who want a change checked against callers and tests before the agent writes anything.

**Plans.** Pricing is public:

1. **Community:** free, 1,000 graph queries per month, then 50 per day.
2. **Pro:** $9 per month, 5,000 graph queries per month, no daily throttle.
3. **Pro+:** $15 per month, 20,000 graph queries per month.
4. **Teams:** $29 per seat per month, unlimited graph queries with self-hosted MemDB, an organization workspace, and seat pool enforcement.
5. **Enterprise:** custom pricing with SSO, air-gapped MemDB, and deployment support.

Start on Community and measure how often your agent calls the graph before you pay for more queries.

## How do you connect a code graph to Claude Code or Cursor?

Wiring a graph server into your agent takes one config change, then one prompt change. Three steps cover it.

1. **Index the repository.** Run the tool's indexer against one repository first, so you can check the graph before you trust it.
2. **Register the server.** Wire the graph server into the configuration of your coding agent, whether that is Claude Code or Cursor. Each tool takes an MCP server entry with a command and its arguments.
3. **Tell the agent to use it.** Add a short rule to your project instructions: query the graph for callers and impact before every edit. Models can skip graph tools unless nudged, so the rule matters as much as the config.

Then verify. Ask the agent which functions call a symbol you know well, and confirm it answered from the graph instead of opening files. Check the tool-call log for graph queries. A Memtrace install follows the same pattern.

## Which code graph should you choose? A decision checklist

No tool wins by default. Work through these checks in order.

1. **Local or hosted.** If code cannot leave your machine, rule out anything that indexes in the cloud. CodeGraph and Memtrace both run a local graph.
2. **License.** GitNexus uses the PolyForm Noncommercial license, so read it before commercial use.
3. **Grammar coverage.** Confirm your languages are supported. Graphify lists 33+ tree-sitter grammars. For the rest, test extraction precision on your own repo.
4. **Edge provenance.** Prefer a graph that labels extracted edges apart from inferred ones.
5. **MCP support for your agent.** Confirm the server works with the agent you run.
6. **Benchmark skepticism.** Treat the 79x token figure as vendor-reported. The only independent number here is the 2.2 percentage point localization gain from call and inheritance topology, and it measures a technique, not a product.
7. **Pricing transparency.** Two tools list no public pricing. Memtrace publishes every tier.

Prices and plan limits verified as of November 2026.

## FAQs

### Can I use a code graph locally without sharing code with a third party?

Yes. CodeGraph stores its graph in a local SQLite file, and Memtrace builds a local graph on your machine. Check each tool's telemetry settings first: CodeGraph enables telemetry by default, with an opt-out. Turn it off before you index private code.

### Which MCP servers support my specific AI coding agent?

All four tools in this article connect over MCP, which Claude Code and Cursor both support. GitNexus exposes 17 MCP tools. Support for other agents varies, so check each project's documentation for your agent before you commit to an install.

### Does the code graph actually improve agent accuracy or just speed?

Both are plausible, but evidence is thin. The only independent result here covers accuracy and tests a technique, not a product. Token savings claims, like Graphify's 79x, are vendor-reported. Measure accuracy on your own tasks.

### How does a code graph differ from embeddings-based code search?

Embeddings find text that looks similar to your query. A code graph stores explicit relationships: calls, imports, routes, and tests. Ask an embedding index who calls a function and it guesses from similarity. Ask a graph and it returns the callers it parsed.

### How do I know whether my agent is actually calling the graph tool?

Read the agent's tool-call log after a task. If you see no graph queries, the model skipped the tool. Add an explicit project rule telling it to query the graph before edits, then rerun the same task and compare.

### What language support should I check before adopting a code graph?

List the languages in your repo, then check the tool's parser coverage. Graphify supports over 33 tree-sitter grammars. Even with a grammar, extraction precision varies by language, so spot-check edges in your most-used language before you rely on the results.

## Conclusion

A code graph gives your agent callers, imports, routes, and tests before it edits, instead of a pile of files to re-read each session. The four options differ in where they run, what they keep, and how openly they label what they inferred.

Pick one repository and one tool, wire it over MCP, and add the rule that tells your agent to query it. Then compare a few real tasks with and without the graph, and keep the tool only if the numbers hold on your own code.