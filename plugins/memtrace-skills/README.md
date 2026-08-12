# Memtrace skills plugin

`skills/` is the workflow source of truth. `commands/` contains manual
shortcuts only.

| Shortcut | Delegated workflow |
| --- | --- |
| `/memtrace-skills:mt-onboard` | `memtrace-codebase-exploration` |
| `/memtrace-skills:mt-impact` | `memtrace-impact` |
| `/memtrace-skills:mt-preflight` | `memtrace-preflight` |
| `/memtrace-skills:mt-recall` | `memtrace-decision-memory` |

Plugin-root documentation is not ambient Claude context. The consented npm
setup manages `~/.claude/MEMTRACE.md`. Native Rail owns discovery enforcement:
use `memtrace rail enable nudge|rail|strict` or `memtrace rail disable`. This
plugin deliberately ships no second shell hook.
