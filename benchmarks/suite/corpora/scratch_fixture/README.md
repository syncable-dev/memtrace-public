# scratch_fixture

Tiny hand-authored codebase used by **Bench #4 — Incremental Indexing**.

This fixture is **owned by the benchmark suite**, not by any single adapter.
Reviewers can diff it independently and reason about ground truth from the
Python sources alone.

## Module map

| File | Role | Classes | Functions |
|---|---|---|---|
| `task.py` | unit of work dataclass | `Task` | `make_task` |
| `queue.py` | priority heap | `TaskQueue` | — |
| `worker.py` | pulls + runs tasks | `Worker` | — |
| `scheduler.py` | fans out to workers | `Scheduler` | — |
| `events.py` | pub/sub bus | `EventBus` | `make_bus` |
| `logger.py` | trivial stderr logger | `Logger` | `get_logger` |
| `retry.py` | retry decorator | — | `retry`, `jitter` |
| `storage.py` | key/value result store | `ResultStore` | `new_store` |
| `metrics.py` | task outcome counter | `MetricsCollector` | `reset` |
| `cli.py` | entrypoint | — | `greet`, `run_demo`, `main` |
| `config.py` | config dict + helpers | — | `load_config`, `get_num_workers` |
| `errors.py` | exception hierarchy | `TaskError`, `QueueFullError`, `SchedulerStoppedError` | `wrap_error` |
| `middleware.py` | logging/timing decorators | — | `with_logging`, `with_timing` |
| `health.py` | scheduler health checks | — | `is_healthy`, `pending_work`, `health_report` |
| `backoff.py` | backoff policies | — | `exponential`, `fixed`, `choose_backoff` |
| `__init__.py` | re-exports | — | — |
| `tests/test_queue.py` | queue priority test | — | `test_*` |
| `tests/test_worker.py` | worker drain test | — | `test_*` |
| `tests/test_scheduler.py` | scheduler fan-out test | — | `test_*` |
| `tests/test_retry.py` | retry behaviour test | — | `test_*` |

Totals: **10 classes** (`Task`, `TaskQueue`, `Worker`, `Scheduler`,
`EventBus`, `Logger`, `ResultStore`, `MetricsCollector`, `TaskError` +
two subclasses — core set of 5 domain classes plus support types),
**21 top-level functions**, **21 Python files** (4 of them tests, 2 of
them `__init__.py`), all under 30 lines per source file.

## Properties Bench #4 relies on

- Real import graph — `worker.py` imports `queue`, `events`, `logger`;
  `scheduler.py` imports `worker` etc. This lets edit-script actions like
  `move_symbol` produce observable cross-file changes.
- Symbol names are unique across the fixture, so
  `find_symbol("make_task")` has exactly one correct answer.
- Named `retry.py` rather than `foo.py` so the corpus doesn't fingerprint
  as synthetic to tools that weigh filename heuristics.
