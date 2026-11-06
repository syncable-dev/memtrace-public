---
title: "Code Knowledge Graph: How to Build and Query One"
date: 2026-10-13
description: "Learn how to build and query a code knowledge graph in six steps to trace execution paths, assess blast radius, and power AI coding agents faster."
og_title: "Code Knowledge Graph: How to Build and Query One for Smarter Code Analysis"
og_description: "Learn how a code knowledge graph maps functions, classes, and dependencies as typed nodes and edges so developers and AI agents can trace structure and assess impact."
hero_image: "./code-knowledge-graph-how-to-build-and-query-one.png"
hero_image_alt: "Diagram of a code knowledge graph with nodes for functions and classes linked by calls and imports edges"
---

# Code Knowledge Graph: How to Build and Query One

A code knowledge graph maps every function, class, import, and test in a repository as typed nodes and edges, so developers and AI agents can trace execution paths, assess blast radius, and answer "why was this written this way?" Building one follows six steps: parse, extract symbols, resolve edges, store, expose a query interface, and keep the graph in sync with every commit.

Keyword search finds text. RAG finds semantically similar chunks. Neither tells you which functions call a given method, which tests cover a changed class, or how far a one-line edit ripples across a service boundary. A code knowledge graph answers those questions by encoding structure, not just content. This guide covers each build phase, names the tradeoffs, and shows how to expose the finished graph to AI coding agents.

## What Is a Code Knowledge Graph (and How Is It Different from RAG)?

A code knowledge graph represents a repository as a directed, typed graph. Each node is a code entity: a function, class, module, type, or test. Each edge is a relationship: calls, imports, inherits, tests, or depends on. Together, nodes and edges let you traverse the codebase the way a compiler does, following actual program structure rather than textual similarity.

RAG chunks source files into text segments, embeds them as vectors, and retrieves the chunks nearest to a query vector. That works well for prose documentation. It works poorly for code questions that require structural reasoning. "What calls `process_payment`?" is a graph traversal. A vector search returns files that mention `process_payment`, but it cannot rank them by call depth or tell you which callers sit inside a transaction boundary.

The node-and-edge model also preserves context that embeddings lose. An edge of type `tested_by` links a function directly to its test suite. An edge of type `depends_on` links a module to every library it imports. When an AI agent needs to assess blast radius before editing a function, it follows those edges outward rather than issuing multiple vector queries and guessing at relationships.

Research-scale graphs show how far this model can stretch. The CodeGraph project built an open-taxonomy knowledge graph containing approximately 158 million nodes and approximately 1 billion typed edges across 14 programming languages. Most production codebases are orders of magnitude smaller, which means the same architecture runs comfortably on a single machine.

Practical use cases include:

- **Impact analysis:** find every caller of a function before refactoring it.
- **Blast radius assessment:** count how many modules a schema change touches.
- **Test coverage tracing:** confirm which tests exercise a code path.
- **Onboarding queries:** answer "why does this module exist?" by surfacing its callers and commit history together.

Each of these is a graph query, not a keyword search. That distinction drives every architectural decision in the build phases below.

## How Do You Parse a Codebase into Graph-Ready Data?

Parsing turns raw source files into structured data the graph can ingest. Two steps cover this phase.

**Step 1: Parse source code into an Abstract Syntax Tree (AST) using a language-agnostic parser.** Language-agnostic parsers handle multiple grammars, so one extraction pipeline can process Python, TypeScript, and Go files without a separate tool per language. The parser's output is deterministic: the same source file always produces the same tree, which matters for incremental updates later.

**Step 2: Extract symbols from the syntax tree, including functions, classes, and types.** Once you have the AST, you walk it and collect every named entity. For each function, record its name, file path, line range, parameters, and return type. For each class, record its name, parent classes, and the methods it defines. For each import, record the importing module and the imported symbol or path. These extracted records become the nodes in your graph.

At this stage, store each node with a stable identifier, typically a combination of file path and symbol name. Stability matters because the incremental update system keeps new extractions matched against existing nodes to detect what changed rather than rebuilding from scratch.

**Choosing a parser.** Language-agnostic parsers that operate on ASTs support a wide range of grammars without requiring a separate tool per language. The Codeknow library, for example, supports building code knowledge graphs for 25+ languages using AST parsing. The key selection criteria are: grammar coverage for your stack, incremental parse support (re-parsing only changed files), and a query API that lets you walk the tree without loading the entire file into memory.

Raw symbol extraction gives you nodes. To build useful edges in the next phase, you also need to capture call sites: every location in the AST where one function invokes another. Capture these as raw records during the same AST walk. Resolving them into edges happens in step 3, but collecting them here avoids a second full parse pass.

A complete parse run on a mid-size repository typically finishes in seconds on a modern laptop. The bottleneck is usually I/O, not CPU, so parsing from an SSD makes a measurable difference on larger repos.

## Building the Graph: Edges, Storage, and Query Languages

With nodes and raw call-site records in hand, the next two steps turn extracted data into a queryable graph.

**Step 3: Identify relationships to create edges.** For each call site captured during parsing, resolve the callee to a known node. A call from `checkout()` to `process_payment()` in the same module resolves immediately. A call across module boundaries requires resolving the import path to a file, then matching the symbol name to a node in that file. Unresolvable calls (dynamic dispatch, reflection, external libraries) become dangling edges or are dropped, depending on your tolerance for incomplete data. Beyond call edges, extract:

- `imports` edges from each module to its dependencies
- `inherits` edges from subclass to parent class
- `tested_by` edges by matching test function names to the functions they assert against (using naming conventions or decorator metadata)

**Step 4: Store nodes and edges in a graph-capable storage layer.** Storage choice determines query expressiveness, operational complexity, and how well the graph scales.

Neo4j with Cypher is the best fit for large, relationship-heavy codebases and supports self-hosting. SQLite with an adjacency list works well for small to mid-size repos with minimal setup and runs in-process with no server required. An in-memory graph library such as NetworkX suits prototyping and ephemeral CI runs. Hosted graph databases using Cypher or SPARQL are available for teams avoiding infrastructure work.

**Cypher vs. SPARQL for code analysis.** For most code graph use cases, Cypher is the more direct fit. SPARQL can express the same queries but requires modeling code entities as subject-predicate-object triples, which adds indirection. SPARQL becomes relevant if you need to federate your code graph with external knowledge sources that already use RDF.

**SQLite as a lightweight alternative.** An adjacency list in SQLite works well for repositories under a few hundred thousand nodes. You lose native path traversal, so multi-hop queries require recursive CTEs or application-layer graph walks. The tradeoff is zero infrastructure: SQLite runs in-process, requires no server, and fits inside a CI container without extra setup.

**Self-hosting considerations.** Graph databases that store large node and edge counts are memory-intensive and disk I/O bound. Plan for enough RAM to hold your working set in memory, and use fast local storage. For a monorepo with millions of nodes, a dedicated server is more practical than a developer laptop. For a single-service repository, a laptop or a small cloud instance is sufficient.

## How Do You Keep a Code Graph Synchronized with Code Changes?

A code graph that reflects last week's code is worse than no graph at all. It sends agents down stale paths and produces blast-radius estimates that miss recent additions. Incremental updates solve this by rebuilding only what changed.

When a file changes, re-parse that file, extract its updated symbols and call sites, diff the result against the existing nodes and edges for that file, and apply only the delta. Nodes that disappeared are removed. New nodes are inserted. Changed edges are replaced. Files that did not change are untouched. This keeps the graph current without re-processing the entire repository on every commit.

The performance advantage is significant. One vendor-reported example shows an incremental mode running 60x faster than full ingestion by processing only changed files. That figure comes from a single vendor source, so treat it as an illustrative order-of-magnitude rather than a universal benchmark. The underlying principle holds regardless: a 500-file repository where three files changed should not re-parse 497 files.

The simplest trigger is a filesystem watcher that fires on save. For CI/CD pipelines, a post-commit or post-merge hook works better: parse the diff, identify changed files, and run the incremental ingestion job on that set. Most CI systems support this through a step that compares the current commit to its parent.

Run the graph update as a non-blocking CI step after tests pass. Store the graph artifact in a shared cache keyed to the commit SHA. Agents and developers pull the latest artifact at query time. The graph stays at most one commit behind, and no developer machine needs a full rebuild after cloning.

Stale edges from deleted functions are the most common consistency issue. Build a cleanup pass into the incremental job: after applying updates, scan for edges whose source or target node no longer exists and remove them.

## How Do You Handle Polyglot Codebases in a Code Knowledge Graph?

Most production repositories are not single-language. A backend in Go, a frontend in TypeScript, shared configuration in Python, and build scripts in shell is a common combination. A code knowledge graph that covers only one language leaves cross-language call paths dark.

**Language-agnostic parsers.** The practical answer is a parser that handles multiple grammars through a shared AST interface. You define one extraction pipeline, and the parser selects the right grammar per file extension. The Codeknow library supports building code knowledge graphs for 25+ languages using AST parsing, which covers most production stacks. The key requirement is that the parser produces a normalized symbol format across languages so that a Python function node and a TypeScript function node share the same schema and can participate in the same edge types.

Call edges within a single language resolve through import paths. Across languages, the resolution mechanism depends on the interface layer. A REST API boundary connects a TypeScript caller to a Go handler through a URL pattern rather than a symbol import, so resolving those edges means matching the URL string in the caller to the route definition in the handler. The same logic applies to gRPC service definitions, GraphQL schemas, and message queue topics. These cross-language edges are harder to resolve automatically, but they are also the most valuable: they are exactly the paths that keyword search and single-language analysis miss.

**Scale at the research level.** The CodeGraph project demonstrates what the architecture can hold: a graph spanning 14 programming languages with roughly 1 billion typed edges. A real-world monorepo with several million lines of code sits well below that ceiling, which means the polyglot challenge is primarily one of edge resolution logic, not storage capacity.

## Connecting the Code Graph to AI Agents and LLMs via MCP

An AI coding agent that cannot query your code graph falls back to reading raw files and guessing at structure. Exposing the graph through a Model Context Protocol (MCP) server gives the agent a structured query interface it can call the same way it calls any other tool.

**Step 6: Expose the graph through a query engine or MCP server.** An MCP server wraps your graph query layer and exposes a set of named tools: `get_callers`, `get_callees`, `get_tests_for`, `get_blast_radius`, `trace_path`. The agent calls these tools with a symbol name and receives a structured list of nodes and edges. No file reading, no token-heavy context dumps. The agent gets exactly the structural facts it needs for the next edit.

**Query patterns agents use most.** Three patterns cover the majority of agent workflows:

- **Caller tracing:** "Which functions call `send_invoice`?" The agent runs this before editing `send_invoice` to know what it might break.
- **Blast radius:** "How many modules depend on `UserSchema`?" The agent uses the count and the list to decide whether a change is safe to make autonomously or needs human review.
- **Test coverage lookup:** "Which tests exercise `calculate_discount`?" The agent retrieves the test list before modifying the function, then re-runs only those tests to verify correctness.

A vector search for "functions related to `send_invoice`" returns files that mention the string. It cannot rank results by call depth, filter to direct callers only, or confirm that a test actually asserts against the function rather than just importing it. Graph traversal answers all three because the relationships are encoded as typed edges, not inferred from text proximity.

Claude, Cursor, and other MCP-compatible agents can connect to a graph MCP server without modification. The agent treats graph tools the same as file-read or shell-exec tools. For teams exploring this pattern, several open-source MCP bridge implementations connect Claude to a locally running graph server.

## When a Maintained Local Code Graph Is the Better Choice

Building and maintaining a code graph from scratch takes real engineering time: parser selection, edge resolution logic, incremental update pipelines, and an MCP server to expose it all. For teams that want the graph without that overhead, a pre-built option is worth considering.

[Memtrace](https://memtrace.io/) is a local code graph tool that connects functions, callers, APIs, and tests across a repository and keeps the graph current as code changes. It connects to existing coding tools via MCP, so agents like Claude can query it immediately. Pricing starts at a free Community tier (1,000 graph queries per month, then 50 per day), with paid tiers at $9/month for 5,000 queries, $15/month for 20,000 queries, and $29/seat/month for teams needing unlimited queries with a self-hosted database and organization workspace. Enterprise plans include SSO and air-gapped deployment.

The self-built path gives you full control over the schema and storage layer. The maintained path gives you a working graph on day one.

Prices and plan limits verified as of October 2026.

## FAQs

### Can a code knowledge graph be built without an LLM?

Yes. Parsing, extraction, storage, and incremental updates are all deterministic. LLMs become relevant only at query time, when an AI agent uses the graph to gather context before generating code. The graph itself requires no LLM to construct or maintain.

### What graph query language is best for code analysis, Cypher or SPARQL?

For most code graphs, Cypher is the more direct fit. SPARQL can express the same queries but requires modeling code entities as subject-predicate-object triples, which adds indirection. Choose SPARQL only if you need to federate your code graph with external RDF knowledge sources.

### How large can a code knowledge graph get for a real-world monorepo?

Research-scale graphs show the upper bound: the CodeGraph project reached approximately 158 million nodes and approximately 1 billion typed edges across 14 programming languages. A large production monorepo with several million lines of code sits well below that ceiling. Storage and query performance depend more on your choice of storage layer and indexing strategy than on raw node count, so benchmark your own graph against your chosen backend before committing to hardware.

### What hardware is needed to self-host a graph database for a large codebase?

Graph databases are memory-intensive and disk I/O bound. Frequently traversed nodes and edges need to fit in RAM for query latency to stay low. For a single-service repository, a developer laptop or small cloud instance is sufficient. For a monorepo with millions of nodes, plan for a dedicated server with fast local storage. No specific figures apply universally; benchmark your own graph size against your chosen storage layer.

### How is a code knowledge graph different from a call graph or dependency graph?

A call graph and a dependency graph each capture one type of relationship. A code knowledge graph combines multiple relationship types in a single schema, so you can answer questions that neither alone can handle, such as which tests cover a changed function or how many modules inherit from a modified class.

## Conclusion

A code knowledge graph gives developers and AI agents a structural map of a repository: typed nodes for every function, class, and module, and typed edges for every call, import, and test relationship. Building one follows six steps from AST parsing through MCP exposure. Keeping it useful means running incremental updates on every commit, not full rebuilds.

To build your own, start with a language-agnostic AST parser, extract symbols and call sites in a single pass, and choose a storage layer that matches your repo size and infrastructure constraints. For a working graph without the build overhead, look for a pre-built local code graph tool that connects to your existing coding tools via MCP and runs incremental updates on every commit.
