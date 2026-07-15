---
description: Recall decisions, bans, and conventions from Cortex memory before a refactor
argument-hint: "<query> [repo_id]"
allowed-tools: ["ToolSearch", "mcp__memtrace__list_indexed_repositories", "mcp__memtrace__recall_decision", "mcp__memtrace__why_is_this_here", "mcp__memtrace__governing_contracts", "mcp__memtrace__verify_intent"]
---

# mt-recall: decision-memory recall before delete / refactor / library re-pick

Do NOT infer why code exists from the diff or `git log`, and do NOT assume unfamiliar
code is safe to remove. Cortex decision memory records the bans, conventions, and
contracts that a text search cannot see; recall them first.

## Parameters

`$ARGUMENTS` is `<query> [repo_id]`. The query may be free text ("did we ban X", "why do
we use Y") or a symbol name. The optional trailing token, if it names an indexed repo,
is used as `repo_id`.

## Behavior

0. Preload the deferred tool schemas:
   ```
   ToolSearch(query="select:mcp__memtrace__recall_decision,mcp__memtrace__why_is_this_here,mcp__memtrace__governing_contracts,mcp__memtrace__verify_intent,mcp__memtrace__list_indexed_repositories")
   ```
1. Parse inputs. If `$ARGUMENTS` is empty, ask what decision or symbol to check and stop.
   Resolve `repo_id` from a trailing token that names an indexed repo, else via
   `list_indexed_repositories` matched against the current working directory.
2. Recall, widening then narrowing:
   - `recall_decision(query=<free text>, repo_id=<repo_id>)` - ranked decisions, bans, or
     conventions matching the intent. Read the top hits; a ban here vetoes the planned
     action.
   - If the target is a specific symbol: `why_is_this_here(symbol=<name>)` - the decision
     lineage that put it there.
   - `governing_contracts(symbol=<name>)` - the invariants or contracts that bind the
     symbol (what must not be broken).
   - If a decision looks relevant, `verify_intent(decision_id=<id>)` - did that decision
     actually hold, or was it violated (Held / ViolatedAt / CannotProve)? Requires a
     `decision_id` from a prior step.
3. State plainly whether there is a recorded decision, ban, or contract touching this.
   Quote it with its id. Give the go / no-go: proceed, proceed-with-constraint (name the
   contract), or stop (a ban applies). Zero recall hits is not proof of absence: if the
   index is a stale stub (`last_indexed_at: null`), say the recall is unreliable rather
   than clearing the action.

## Example

`/mt-recall "did we ban global mutable state"` returns any matching decision with a
go/no-go verdict. `/mt-recall parseConfig backend-api` recalls decision lineage for that
symbol in `repo_id: backend-api`.

## Restrictions

Read-only recall: makes no edits and never resets the index.
