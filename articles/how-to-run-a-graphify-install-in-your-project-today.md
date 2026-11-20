---
title: "How to Run a Graphify Install in Your Project Today"
description: "Learn how to run a graphify install using uv, pipx, or pip and avoid the double y package name error to build your knowledge graph fast today."
ogTitle: "How to Run a Graphify Install in Your Project Today and Build Graphs Fast"
ogDescription: "Run a graphify install in minutes using uv, pipx, or pip. This guide covers the double y package name fix, platform setup steps, and CLI vs Cloud options to get started."
date: 2026-10-26
image: "./how-to-run-a-graphify-install-in-your-project-today.png"
imageAlt: "Terminal window showing a graphify install command using uv with graph.html and graph.json output files generated"
---

# How to Run a Graphify Install in Your Project Today

To run a Graphify install, add the package (PyPI name: `graphifyy`, with two y's) via uv, pipx, or pip, then run `graphify install` to register the skill with your AI coding assistant, and finally trigger `/graphify.` inside the assistant's chat to build the graph and produce `graph.html` and `graph.json`. This guide covers all four steps and flags the double-y package name before you hit your first error. It also compares the local CLI with Graphify Cloud so you can pick the right setup for your workflow.

Graphify turns codebases, documentation, and media into a queryable knowledge graph for AI coding assistants using deterministic AST parsing. The [graphifyy package has reached 9,322,215 total downloads on PyPI](https://pepy.tech/projects/graphifyy), and in a 30-day window ending October 2026, it was downloaded 1,790,557 times. Those numbers reflect a tool that sees real daily use across many teams and repos.

## What You Need Before You Install Graphify

Run `python --version` in your terminal before you do anything else. If your output shows an older version, update Python first. The install commands below may fail on versions that Graphify does not support.

**The most common install error:** the PyPI package name is `graphifyy`, spelled with two y's at the end. Searching `graphify` on PyPI returns a different, unrelated package. Every command in this guide uses the correct double-y spelling.

**Platform support:** Graphify runs on Linux (Ubuntu and other distributions), macOS, and Windows. The install commands are the same on all three platforms, but a few environment details differ:

- **Ubuntu / Linux:** Make sure `python3-pip` and `python3-venv` are installed via your package manager before using pip or uv.
- **macOS:** Homebrew users can install uv with `brew install uv`. The system Python on older macOS versions may be outdated, so check your version first.
- **Windows:** Use PowerShell or Windows Terminal. If `python` maps to the Microsoft Store stub, install a supported Python version directly from python.org and confirm the path resolves correctly.

Graphify's GitHub repository had accumulated 122,800 stars as of late September 2026, per [founderland.ai's 2026 report](https://www.founderland.ai/articles/graphify-labs-launches-knowledge-graph-with-122k-stars-muoq2qjj). That adoption means the community is large and issues are well-documented, which helps when troubleshooting platform-specific setup problems.

For non-code assets (PDFs, video, community detection), Graphify requires optional extras and an LLM API key. Code files parse without any model configuration.

## How to Install Graphify Using uv, pipx, or pip

Three package managers work for a Graphify install. Pick the one that fits your existing workflow.

**Option 1: uv (recommended)**

uv installs tools into isolated environments and resolves dependencies fast. If you do not have uv yet, install it with:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then install Graphify:

```bash
uv tool install graphifyy
```

After the command completes, confirm the install with:

```bash
graphify --version
```

A version string in the output means the binary is on your PATH and ready to use.

**Option 2: pipx**

pipx also installs into an isolated environment and keeps the `graphify` binary globally accessible without polluting your project's virtual environment:

```bash
pipx install graphifyy
```

Confirm with `graphify --version` as above. If pipx reports that it cannot find the binary after install, run `pipx ensurepath` and restart your terminal.

**Option 3: pip**

pip installs `graphifyy` into whichever Python environment is currently active. Activate a virtual environment first to avoid polluting your system Python:

```bash
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows PowerShell

pip install graphifyy
```

Confirm with `graphify --version`.

**A note on the pip install graphify command:** searching for `pip install graphify` (one y) is the most common mistake new users make. The single-y package is unrelated. If your install completes but `graphify --version` returns an unexpected result or the `graphify install` command in the next section fails, the wrong package is likely installed. Uninstall it with `pip uninstall graphify` and reinstall using `pip install graphifyy`.

The Graphify source and issue tracker are on GitHub. If an install step fails in a way not covered here, the repo's issues page is the fastest place to find a reported fix or file a new one.

## How to Register the Graphify Skill with Your AI Coding Assistant

With the package installed, the next step is skill registration. From your terminal, run:

```bash
graphify install
```

This command writes the Graphify skill configuration to the location your AI coding assistant reads for registered tools. After it runs, the assistant gains access to the `/graphify` command family inside its chat interface.

**Compatible assistants:** Graphify works with Claude Code and other AI coding assistants that support skill or tool registration via a defined protocol. No AI coding assistant officially bundles Graphify, so this is a third-party integration on both sides. The `graphify install` command detects which assistant configuration directory is present on your system and writes to it automatically. If you use more than one assistant, run `graphify install` once per assistant or check the Graphify GitHub documentation for multi-assistant configuration options.

**How to install Graphify in Claude Code specifically:** Claude Code reads skill configurations from a known local directory. Running `graphify install` in your terminal, with Claude Code already installed, writes the skill definition to that directory. No manual file editing is required. Restart Claude Code after running the command so it picks up the new skill.

**Graphify for Claude and other assistants:** the same `graphify install` command applies regardless of which assistant you use, provided that assistant supports the skill registration protocol. Parallel extraction is not supported on OpenClaw and Aider, which use sequential processing instead.

After registration, open your assistant's chat interface and type `/graphify --help` to confirm the skill is active. A list of available subcommands in the response confirms successful registration. If the command is not recognized, close and reopen the assistant, then retry.

## How to Build Your Code Graph Inside the Assistant Chat

With the skill registered, open your AI coding assistant's chat interface and run:

```
/graphify .
```

The dot tells Graphify to scan the current working directory. Run this from your project root so the graph covers the full repository. If you want to scope the scan to a subdirectory, replace `.` with the relative path, for example `/graphify src/`.

**What the scan does:** Graphify uses deterministic AST parsing to walk your code files. It does not call an LLM for code. It reads the syntax tree directly, which means the scan is reproducible and does not vary between runs. Functions, callers, APIs, and tests are extracted from the structure of the code itself.

**Non-code assets need extra configuration.** PDFs, video files, and community detection features require optional extras and an LLM API key. If your repository contains only source code, you do not need to configure a model. If you want to include documentation or media, install the relevant extras before running the scan:

```bash
pip install graphifyy[pdf]      # PDF support
pip install graphifyy[video]    # Video support
```

Then set your API key as an environment variable per the Graphify GitHub documentation.

**What happens during the scan:** Graphify walks each file, builds the graph in memory, and writes two output files to your project root when it finishes: `graph.html` and `graph.json`. The assistant's chat interface shows progress as files are processed. On a large repository the scan may take several minutes. There is no published benchmark for build time, so treat your first run as a calibration point.

**Confirming success:** when the scan completes, the assistant reports the number of nodes and edges added to the graph. Both `graph.html` and `graph.json` appear in your project root. If either file is missing, the scan did not complete. Check the assistant's output for a reported error, confirm your working directory was the project root, and rerun `/graphify.`.

## What Do graph.html and graph.json Contain?

When `/graphify.` finishes, Graphify writes two files to your project root. Each serves a different access pattern.

**graph.html** is an interactive browser visualization. Open it by running:

```bash
open graph.html          # macOS
xdg-open graph.html      # Linux
start graph.html         # Windows PowerShell
```

The file loads in your default browser with no server required. Inside, you see a node-and-edge diagram of your repository. Nodes represent functions, API endpoints, and tests. Edges show caller relationships, so you can follow a function call chain visually. Clicking a node surfaces the file path, line number, and connected nodes. This view is useful for exploring unfamiliar code or tracing how a change in one function propagates to callers.

**graph.json** is the machine-readable version of the same graph. It contains the full node and edge list in a structured format your terminal tools and scripts can query directly. To inspect it without a JSON viewer, pipe it through `jq`:

```bash
cat graph.json | jq '.nodes | length'
```

That command returns the total node count. To find all nodes connected to a specific function name, use:

```bash
cat graph.json | jq '.nodes[] | select(.label == "your_function_name")'
```

Replace `your_function_name` with the actual function. The output shows the node's ID, type, file path, and edge connections.

Both files reflect the state of your repository at the time of the last `/graphify.` run. They do not update automatically when you edit code. To refresh the graph after changes, rerun `/graphify.` from the assistant chat. Tools like [memtrace.io](https://memtrace.io/) can help you track memory usage if you are scanning a very large repository and want to profile the build process.

## Local CLI vs. Graphify Cloud: Which One Should You Use?

Graphify ships in two forms: the local CLI you installed above, and Graphify Cloud, a hosted version. The right choice depends on whether you need the graph to persist between sessions and whether you work across multiple repositories.

| Feature | Local CLI | Graphify Cloud |
|---|---|---|
| Always-on memory | No, graph is rebuilt on demand | Yes, continuous background mapping |
| Graph persistence | Files saved locally after each run | Hosted, persists across sessions |
| Cross-repo support | Single repo per run | Yes, per notes |
| Formal verification | Not included | Yes |
| Pricing | Not publicly listed | Paid plan |

The local CLI fits solo developers who work in one repository and can tolerate rebuilding the graph when needed. Graphify Cloud adds continuous background mapping and SDLC-wide features that are exclusive to the hosted service, per index.devtoolwatch.com. Graphify had reached 129.1k GitHub stars by mid-October 2026, and [weekly downloads hit 418.1k during the week of October 12, 2026](https://index.devtoolwatch.com/tools/graphify-labs-graphify/), which reflects adoption across both deployment modes.

If your team needs the graph available without a manual rebuild step before each session, Graphify Cloud is the path. If you prefer local control and a one-time scan workflow, the CLI you installed above covers the core use case.

## When a Live Code Graph Gives You More Than a Static Snapshot

A graph you rebuild on demand captures your repository at a single point in time. The moment you edit a file, the graph is stale. For a quick exploration session that is fine. For an AI coding agent making decisions about blast radius across dozens of files mid-sprint, it is a meaningful gap.

The local CLI produces an accurate snapshot, but keeping it current requires you to rerun `/graphify.` after each meaningful change. On an active codebase, that friction adds up. If your workflow involves frequent edits, consider how often you will need to rebuild and whether a manual step fits your process.

Prices and plan limits verified as of October 2026.

## FAQs

### Can I use Graphify with VS Code or only with Claude Code?

Graphify works with AI coding assistants that support skill or tool registration. VS Code extensions that expose a compatible skill registration interface can work. Claude Code is the most commonly documented integration, but the install command is not exclusive to it. Check the Graphify GitHub repository for the current list of confirmed compatible assistants before assuming compatibility with a specific tool.

### Does Graphify work on monorepos with multiple languages?

Graphify uses deterministic AST parsing to extract functions, callers, APIs, and tests from code files. It scans whatever is in the directory you point it at. For a monorepo, run `/graphify.` from the root to cover all subdirectories. Whether every language in a mixed-language monorepo produces full graph output depends on which parsers Graphify ships with. Check the Graphify GitHub documentation for the current list of supported languages before running a full scan on a large multi-language repo.

### How do I update Graphify after a new version is released?

The update command mirrors the install command for whichever package manager you used. With uv: `uv tool upgrade graphifyy`. With pipx: `pipx upgrade graphifyy`. With pip inside your virtual environment: `pip install --upgrade graphifyy`. After upgrading, run `graphify --version` to confirm the new version is active, then rerun `graphify install` to refresh the skill registration in your AI coding assistant.

### Will running graphify install modify my project files?

No. `graphify install` writes to your AI coding assistant's configuration directory, not to your project. Your source files, `pyproject.toml`, `requirements.txt`, and version-controlled files are not touched. The two files Graphify does write to your project root, `graph.html` and `graph.json`, are created only after you run `/graphify.` inside the assistant chat, not during the install step.

### How large can a codebase be before graph build times become slow?

Your first `/graphify.` run on a large repository is your calibration point. The scan uses deterministic AST parsing without LLM calls for code files, which removes one common bottleneck. For very large repositories, scoping the initial scan to a subdirectory (for example `/graphify src/`) gives you a faster first result while you assess performance on your specific codebase.

## Conclusion

Running a Graphify install takes four steps: install the `graphifyy` package (two y's), run `graphify install` to register the skill, trigger `/graphify.` in your assistant's chat to build the graph, and then open `graph.html` in a browser or query `graph.json` from the terminal. The local CLI gives you a precise snapshot of functions, callers, APIs, and tests at the moment you run the scan.

Start by confirming your Python version and running the install command for your preferred package manager. If you find yourself rebuilding the graph frequently as code changes, the comparison table in the Local CLI vs. Graphify Cloud section above outlines which deployment mode fits that workflow.
