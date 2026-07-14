# Full-suite execution architecture

## Target

Run all 1,136 ContextBench tasks in one to three hours without losing completed
work when a worker, container, API call, or benchmark process fails.

The first 21-task run averaged 393.5 seconds end to end and 22.7 seconds for
indexing. At that rate, a three-hour run needs at least 42 continuously busy
task slots. Use 48 slots as the planning target so stragglers do not determine
the wall clock.

A faster GPU alone cannot provide that speedup. Indexing represented about 6%
of measured wall time; mini-SWE-agent/API waits, Docker startup, repository I/O,
and tests represented the rest. The design therefore separates graph building
from agent execution and scales task workers independently.

## Recommended Hetzner layout

Use two dedicated machines for the first production run:

1. **AX162-2 worker:** AMD EPYC 9454P, 48 cores/96 threads, 256 GB RAM, and
   local NVMe. Start at 32 task workers, then raise toward 48 after measuring
   peak container memory and test CPU contention.
2. **GEX44 embedding service:** RTX 4000 SFF Ada with 20 GB VRAM. Batch local
   embeddings and reranking requests from graph builders. It is not the main
   task runner because its 64 GB RAM and 14-core hybrid CPU limit Docker
   concurrency.

At Hetzner's 2026-06-15 list prices, that combination is EUR 1,074.60/month
before VAT and IPv4, plus EUR 533 setup. A single GEX131-1 is operationally
simpler and has a much faster 96 GB Blackwell GPU plus 256 GB RAM, but it costs
EUR 1,197.30/month plus EUR 599 setup and has only 24 CPU cores. For this
benchmark, the AX162 plus GEX44 split gives more CPU concurrency for less
monthly cost.

Current official specifications and prices:

- https://www.hetzner.com/dedicated-rootserver/ax162/
- https://docs.hetzner.com/robot/dedicated-server/server-lines/gpu-server/
- https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/

## Immutable inputs

Create one run manifest before any worker starts. It pins:

- ContextBench dataset hash and ordered instance IDs;
- Memtrace git SHA and binary checksum;
- parser/graph schema namespace;
- embedding model ID, revision, dimension, and checksum;
- reranker model ID and checksum;
- mini-SWE-agent config checksum;
- model name and inference parameters;
- Docker image digests;
- query-plan and selector prompt versions.

Changing any graph-affecting value creates a new cache namespace. Never reuse a
graph merely because its repository and commit match.

## Three execution phases

### 1. Repository and image preparation

- Maintain one bare Git mirror for each of the 66 repositories.
- Create task checkouts from the mirrors at deterministic paths such as
  `/srv/contextbench/tasks/<instance-id>/repo`.
- Pre-pull every official task image and record its digest.
- Keep the same checkout mount path on every worker because current graph
  records contain canonical absolute source paths.

### 2. Graph build

Build each task graph once, before launching the coding agent. The cache key is
conceptually:

```
sha256(
  repo_url,
  base_commit,
  memtrace_build_sha,
  parser_schema,
  ignore_policy_hash,
  embedding_model_revision,
  embedding_dimension
)
```

The current runner supplies `repo_url`, `base_commit`, and a caller-controlled
`--cache-namespace`. Set that namespace to a digest of all remaining fields.

Write to a temporary cache directory. Only publish `complete.json` after the
MCP process reports exactly one repository, a non-zero embedding count, and a
successful index. The manifest is atomically renamed into place. Incomplete or
workspace-mismatched entries are deleted and rebuilt.

Keep a hot copy on local NVMe. Upload completed entries as immutable `tar.zst`
objects to S3-compatible object storage. Object storage is disaster recovery,
not the live database: each running task gets its own local copy or reflink.

### 3. Agent and evaluation

- Run 32 workers initially and use a bounded queue.
- Give each task a dedicated checkout, MemDB, agent output directory, and
  Docker namespace.
- Apply a global OpenAI requests/tokens-per-minute limiter rather than letting
  48 workers independently retry 429s.
- Write predictions, patches, audit data, and logs before marking a task done.
- Run official grading as a separate resumable stage; a grading failure must
  not rerun retrieval or the agent.

The 21-task sample used 330 model calls, or 15.7 calls per task. A full run is
therefore approximately 17,800 API calls. The OpenAI rate limit must be checked
before choosing worker count.

## Durable task state

Track every task with an append-only state transition:

```
pending -> checkout_ready -> graph_ready -> retrieval_done
        -> agent_done -> graded -> archived
```

Each transition stores input hashes and artifact paths. On restart, workers
resume from the last valid transition. Existing JSONL outputs remain an export
format; they should not be the scheduler's only source of truth.

For one machine, SQLite in WAL mode plus per-task file locks is sufficient. For
two machines, use Postgres or a small durable queue so leases expire and tasks
can be reclaimed after worker death.

## Expected runtime

Using the original 393.5-second average:

- 32 slots: about 3.9 hours before straggler allowance;
- 42 slots: about 3.0 hours;
- 48 slots: about 2.6 hours.

The repaired one-task run completed in 171 seconds, which would put 32 slots at
about 1.7 hours if representative. Do not capacity-plan from that single task;
run a stratified 50-task load test and use its p50, p90, p99, peak RAM, and API
throughput to set the production concurrency.

## Launch gate

Before ordering the full run:

1. prove a graph-cache hit reports `index_seconds: 0` and identical retrieval;
2. kill one worker during indexing and prove the partial cache is rejected;
3. kill one worker during agent execution and prove the graph is reused;
4. run 16 tasks concurrently and record CPU, RAM, NVMe, GPU, Docker, and API
   saturation;
5. run a stratified 50-task slice with no manual intervention;
6. freeze all hashes and launch the 1,136-task manifest.
