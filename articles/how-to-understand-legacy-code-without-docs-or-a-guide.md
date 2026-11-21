---
title: "How to Understand Legacy Code Without Docs or a Guide"
description: "Learn to understand legacy code without docs: trace entry points, mine schemas and history, map dependencies, and write tests that lock in current behavior."
ogTitle: "How to Understand Legacy Code Without Docs or a Guide: A Step by Step Method"
ogDescription: "A repeatable order of attack for inherited code with no documentation: entry points, one traced transaction, schema and history, dependencies, then characterization tests."
date: 2026-11-13
image: ./how-to-understand-legacy-code-without-docs-or-a-guide.png
imageAlt: "Developer tracing a code path across a dependency map on a monitor while reading an undocumented legacy system"
---

# How to Understand Legacy Code Without Docs or a Guide

To understand legacy code without docs, work from the outside in. Find the entry points, trace one real business transaction end to end, read the database schema and version history for hidden rules, and map dependencies. Then write characterization tests that lock in current behavior before you change anything. Document findings as you go.

Estimates of the average time developers spend on source code comprehension range from 30 to 70%, according to a 2023 ACM paper. Typing is the small part of the job. On inherited code with no documentation, reading is nearly all of it.

This guide gives you a repeatable order of attack. It runs from entry points to characterization tests, and each step leaves a written artifact: an inventory, a trace, a dependency map, a test suite. By the end, you will have the documentation the original authors never wrote.

## Where should you start when learning a legacy codebase?

Start with a short orientation pass, not a top-to-bottom read of the files. Before you open an editor, answer four questions: what the system is for, what goes in, what comes out, and who owns each part.

The most common mistake is reading files in alphabetical order. You collect details with no frame to hang them on. A repo with 2,000 files has maybe a dozen that matter for any single behavior.

Comprehension is the real work, so give it a time budget. Plan the first days around building a mental model, not shipping a fix. Developers who skip this phase tend to patch the wrong layer or break a rule nobody wrote down.

Keep one plain-text file open from the first minute. Call it `NOTES.md` or anything you will find again. Every unanswered question, surprising name, and setup error goes in. That file becomes your first piece of documentation.

The steps below follow a fixed order for a reason. Each one narrows the system a little more:

1. Talk to the business owners and get a running copy.
2. Identify every entry point.
3. Trace one core transaction.
4. Mine the schema and version history.
5. Map dependencies.
6. Write characterization tests.

## Step 1: Talk to the business owners and get a running copy

Interview the people who use the system before you read its code. They know what it is supposed to do, which the code alone cannot tell you.

Ask short, concrete questions:

1. What does this system do for the business?
2. What data goes in, and where does it come from?
3. What comes out, and who depends on it?
4. What breaks most often, and what happens when it does?
5. Which month-end, year-end, or batch process must never fail?

Write the answers down in your own words. Ask for one real example of each input and output, such as a sample invoice file or an order record. Those examples become test data later.

Then get a running copy. Install a local, breakable version of the system with realistic data. Do not point it at production. If you cannot get realistic data, ask for a scrubbed snapshot, or build a small set from the examples your interviewees gave you.

Setup will fail in several places: missing environment variables, an old runtime version, a database that expects a specific collation, a config file nobody committed. Record every error and its fix in your notes, in order. That list is the install guide the project never had.

Do not fix anything yet. A running copy exists so you can watch the system behave.

## Step 2: Identify every entry point

Execution starts somewhere. Find every place it can, and you have the outer boundary of the system.

Look for these kinds of entry points:

1. **Web routes and API handlers:** URL mappings, controller registrations, route tables.
2. **Scheduled jobs:** cron entries, task scheduler configs, job queue definitions.
3. **Database triggers:** logic that fires on insert, update, or delete, with no application code involved.
4. **Message consumers:** queue listeners, webhook receivers, file watchers.
5. **Command-line and batch scripts:** shell scripts, `.bat` files, anything an operator runs by hand.

Procedural codebases need a different search. With no classes or routers, look for `main` routines, top-level scripts, and the jobs that call them. In COBOL or RPG, that means job control language and program call chains. In older PHP or Perl, look for the files a web server maps directly to URLs. In C, start from `main()` and any signal handlers. In shell-driven systems, read the crontab and the scripts it calls.

Copy this template into your notes, one row per entry point:

```
Name:
Type: (route / job / trigger / consumer / script)
Location: (file and line)
Triggered by:
Reads:
Writes:
Owner or user:
```

Fill in what you know and leave the rest blank. The blanks are your reading list for the next steps.

## Step 3: Trace one core transaction from start to finish

Pick one business transaction that matters, such as placing an order or posting a payment, and follow it from its entry point to the final database write. One complete path teaches you more than ten files read in isolation.

Choose the transaction from your interview answers. It should be common, central to the business, and reproducible on your local copy.

Then trace it:

1. Start at the entry point from your inventory and trigger the transaction on your running copy.
2. Set a breakpoint at the first line of the handler. If you cannot attach a debugger, add temporary log statements that print the function name and key arguments.
3. Step through the call chain. At each hop, write down the function, the file, and what it changes.
4. Note every external call: other services, files read or written, emails sent, queues published to.
5. Stop at the last database write and record which tables and columns changed.

Your notes should end up as a plain sequence:

```
POST /orders -> OrderController.create (orders.py:41)
  -> validate_cart (cart.py:88), reads: cart, prices
  -> apply_discount (pricing.py:210), calls: promo service
  -> save_order (orders.py:132), writes: orders, order_lines
```

Mark anything surprising with a question mark, such as a function that mutates global state or a branch that depends on a customer type nobody mentioned. Do not chase every branch. Follow the main path first, then rerun the trace with a different input to see where it forks.

Remove your temporary logging when you finish, or keep it behind a flag. You now hold one verified, written path through the system.

## Step 4: Mine the database schema and version control history

Some of the most important rules in a legacy system never appear in application code. They live in the database and in the history of the repo.

Start with the schema. Read it like a document:

1. **Constraints:** foreign keys, unique indexes, and check constraints show which rules the data must obey.
2. **Defaults:** a column that defaults to `'P'` or `-1` often encodes a status nobody named.
3. **Stored procedures and triggers:** read these in full. They often hold pricing, rounding, or approval logic that the application never sees.
4. **Naming oddities:** tables like `ORD_TMP2` or columns like `flag3` point to past workarounds. Search the code for each one.

Then read the history. Version control records why code changed, which is the one thing the code itself cannot tell you:

1. Run `git blame` on a confusing line to find the commit that introduced it.
2. Read that commit message and run `git log -p` on the file to see the change in context.
3. If the repo is hosted on GitHub, open the pull request linked to the commit. Review comments often explain the decision, and linked issues often name the bug that prompted it.
4. Search the log for words like "hotfix", "workaround", and "revert". Those commits mark the places where the system surprised its own authors.

This reading pays off. Sourcegraph's [2024 post on understanding legacy code](https://sourcegraph.com/blog/a-simpler-way-to-understand-legacy-code) cites a study that puts the share of developer time lost to technical debt at 11% to 30%. Recovering the reason behind a strange line early keeps you from relearning it the hard way.

Add every recovered rule to your notes, with the commit hash beside it.

## Step 5: How do you map dependencies and document as you go?

A dependency map shows what the system relies on and what relies on it. You do not need a commercial tool. A pencil, a whiteboard, or a text file is enough.

Map two layers:

1. **Internal dependencies:** which modules call which. Take these from your entry-point inventory and transaction trace, then confirm with a search for imports or includes.
2. **External dependencies:** everything outside the repo. Look for connection strings, hardcoded hostnames, file drops and shared network folders, FTP or SFTP jobs, third-party APIs, and shared libraries pinned to old versions.

Search the config and source for patterns that signal an outside call: an `http` or `https` URL, `jdbc:`, `smtp`, `\\server\share`, `/mnt/`. Each hit is a node on your map.

A text diagram works fine and lives in version control:

```
[Web app] --> [Orders DB]
[Web app] --> [Promo service] (HTTP)
[Nightly job] --> [Orders DB] --> [CSV drop: /mnt/finance/out]
[Finance system] <-- reads CSV
```

Draw arrows in the direction data flows. Mark any dependency you cannot explain with a question mark and ask the owners from Step 1.

Skip the formal documentation plan. Keep one running notes file with sections for entry points, traces, recovered rules, dependencies, and open questions. Update it at the end of every working session, while the details are fresh. A short file you maintain beats a polished wiki nobody writes.

Commit the notes file to the repo. The next person starts from your map, not from zero.

## Step 6: What are characterization tests and how do you write them?

Characterization tests record what the code does today, not what it should do. They capture current behavior, odd parts included, so you can tell when a later change alters it.

You need them before you edit anything. Without them, a refactor is a guess. With them, a change that breaks an undocumented rule fails a test instead of failing in production.

Use the write-run-observe-assert loop:

1. Pick a behavior from your transaction trace, such as "order total with a promo code".
2. Write a test that calls the code with a real input from your interviews, and assert something you expect to be wrong, like an empty result.
3. Run it. The test fails and prints the actual value.
4. Read that value. If it is plausible, paste it into the assertion as the expected result.
5. Run it again. It now passes, and the behavior is locked in.

A small example:

```python
def test_total_with_promo_code():
    order = build_order(items=[("A100", 2)], promo="SPRING")
    assert calculate_total(order) == EXPECTED_TOTAL  # observed value, not specified
```

If the number looks wrong, do not fix it. Keep the assertion, add a comment saying it looks like a bug, and raise it with the business owner. Characterization tests describe reality first and judgment second.

Cover the main path from Step 3, then the edge cases your schema and history reading turned up: the odd default, the rounding rule, the hotfix. Aim for breadth over depth. Ten tests on ten behaviors protect more than fifty on one.

## How can AI and a code graph help you understand legacy code faster?

AI and a code graph speed up the lookups in the manual workflow. They do not replace the interviews, the trace, or the tests.

An AI assistant can summarize an unfamiliar file in seconds, as long as you check the summary against the trace you ran yourself. AI summaries can sound confident and still be wrong about code the model only half saw.

A code graph answers a different question: what does this touch? [Memtrace](https://memtrace.io/) is a code graph tool that builds and maintains a local graph linking functions, callers, APIs, and tests across one repository, for developers and coding agents. You or your coding agent query it over MCP before the next edit to see the blast radius of a change, trace an execution path, or find which tests cover a function. The graph updates as the code changes, so the answers stay current. For more on how that persistent structure works for agents, see [Memtrace's code map](https://memtrace.io/codemap).

Use it where the manual method is slowest. Searching text for every caller of a function misses dynamic calls and renamed wrappers. A graph query returns them in one pass.

The graph shows structure, not intent. It cannot tell you why a rule exists or whether a quirk is a bug. Interviews, commit history, and characterization tests still answer those.

## FAQs

### How can AI assist in understanding legacy code?

AI helps most with reading speed. It can summarize unfamiliar files, explain unusual syntax, and suggest what a function probably does. Treat each answer as a hypothesis and confirm it against a traced run or a characterization test. AI cannot recover business intent that lives only in people's heads or old tickets.

### Can I understand legacy code on my own without asking the original developers?

Yes, but it is slower and riskier. The schema, version history, and a traced transaction recover much of the intent. Business owners and users still fill gaps the code cannot, such as why a rule exists. If the original authors are gone, interview current users and note every question you cannot answer.

### How long does it take to understand a legacy codebase?

Aim for milestones instead of a fixed number: a running local copy, an entry-point inventory, one traced transaction, then a first set of characterization tests. Reaching those lets you make a small, safe change. Deeper understanding builds with each change.

### What if the legacy code is procedural or has no classes?

The method still works. Entry points become `main` routines, batch scripts, job control language, and files a web server maps to URLs. Trace calls between routines instead of between objects. Dependencies show up as shared files, global variables, and database tables. Characterization tests can wrap a routine or run a whole script against sample input.

### Should I refactor legacy code while I am still learning it?

No. Refactoring before you understand the behavior is how undocumented rules get deleted. Learn first, write characterization tests second, and refactor third, in small steps with the tests running after each one. If something looks ugly, note it in your file and come back once the tests protect you.

### What is the difference between a unit test and a characterization test?

A unit test checks that code does what it should, based on a specification. A characterization test checks that code still does what it does now, whether or not that behavior is correct. You write unit tests to define intent. You write characterization tests to protect unknown behavior before you change it.

## Conclusion

You understand legacy code by working from the outside in: entry points, one traced transaction, the schema and history, a dependency map, and characterization tests, with notes kept as you go. Each step shrinks the unknown and leaves a written artifact behind.

Start this week with two things. Get a local copy running and log every setup error. Then write the entry-point inventory, even with blanks, so you can see what you still need to learn.