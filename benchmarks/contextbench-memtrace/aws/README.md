# ContextBench on AWS — burst-box harness

Run the full ContextBench benchmark (gated: 500-task verified set first, then
the 1,136-task full set) for Memtrace on a rented `c7a.24xlarge`
(96 cores / 192 GB, x86), with resumable results on a persistent EBS volume.
Spot is the default market, but no instance launches until an explicit hourly
ceiling is configured. On-demand fallback is disabled by default and is
separately price-guarded when enabled.

Everything here is idempotent and re-runnable. All state lives in
`aws/state/` (gitignored). Nothing in this directory ever contains the OpenAI
key — it travels only as an `scp` of `memtrace-public/.env` to the box
(`chmod 600`).

## Files

| file | runs | purpose |
|---|---|---|
| `config.env.example` | — | defaults; `cp` to `config.env` (gitignored) and edit |
| `common.sh` | local | shared helpers (config, state.json, ssh/rsync) |
| `00-preflight.sh` | local | aws CLI, credentials, market-specific vCPU quotas, spending ceilings, key, `.env`, and clean-source checks |
| `01-provision.sh` | local | SG + key pair + persistent data volume + stopped/running instance reuse or guarded launch; schedules the automatic stop |
| `02-bootstrap.sh` | local | rsync adapter, scp `.env`, run `remote/bootstrap-remote.sh`; in source mode also rsync the private memtrace source + build it |
| `03-run.sh` | local | start/resume the benchmark in tmux on the box |
| `04-status.sh` | local | progress, ETA, failures, watchdog events, load/mem/disk |
| `05-evaluate-and-collect.sh` | local | evaluator + report + triage on box, artifacts back to `aws/state/artifacts/<run-id>/`, prints the triage layer-share table |
| `06-teardown.sh` | local | terminate instance; data volume preserved unless `--delete-data` |
| `07-watch.sh` | local | poll progress on a cadence and ALERT (bell + notification) on zero progress, a dead tmux session, or driver exit — see "Watching a run" below |
| `remote/*.sh` | box | bootstrap / build-memtrace (source mode) / run / spot-watcher / status / evaluate |

## Prerequisites

1. **aws CLI v2** locally (`brew install awscli`), configured for the target
   account (`aws configure` or `aws configure sso`), region `us-east-1` by
   default.
2. **vCPU quota ≥ 96** for every market the configured launch path can use:
   - `L-34B43A08` — All Standard Spot Instance Requests
   - `L-1216C47A` — Running On-Demand Standard instances

   Spot quota is required when `USE_SPOT=1`. On-demand quota is required only
   when `USE_SPOT=0` or `ALLOW_ON_DEMAND_FALLBACK=1`; otherwise it is reported
   as optional. New accounts default to 5. `00-preflight.sh` checks both and prints the
   exact `aws service-quotas request-service-quota-increase ... --desired-value 128`
   command if you are short. **File this first — it is the long pole**
   (hours to days). Launch symptom of a low spot quota:
   `MaxSpotInstanceCountExceeded`.
3. **IAM permissions** — minimal policy for everything the scripts do:
   `ec2:RunInstances, StartInstances, TerminateInstances, ModifyInstanceAttribute,
   DescribeInstances, DescribeInstanceStatus,
   DescribeInstanceTypes, DescribeImages, DescribeSpotPriceHistory, CreateVolume,
   AttachVolume, DetachVolume, DeleteVolume, DescribeVolumes, CreateSnapshot,
   DescribeSnapshots, CreateSecurityGroup, AuthorizeSecurityGroupIngress,
   RevokeSecurityGroupIngress, DescribeSecurityGroups, DeleteSecurityGroup,
   CreateKeyPair, ImportKeyPair, DescribeKeyPairs, CreateTags, DescribeVpcs,
   DescribeSubnets, DescribeAvailabilityZones`;
   `ssm:GetParameter(s)` on `arn:aws:ssm:*::parameter/aws/service/canonical/*`
   (AMI lookup); `pricing:GetProducts` (the on-demand spending guard);
   `servicequotas:GetServiceQuota, RequestServiceQuotaIncrease,
   ListRequestedServiceQuotaChangeHistoryByQuota`; and
   `iam:CreateServiceLinkedRole` conditioned on
   `iam:AWSServiceName = spot.amazonaws.com` (needed on accounts that have
   never used spot).
4. **SSH key pair** on this machine (default `~/.ssh/id_ed25519[.pub]`; the
   public key is imported as EC2 key pair `memtrace-bench`).
5. **`OPENAI_API_KEY`** in `memtrace-public/.env` (gitignored). Used for
   GPT query planning + selection.

## Costs (us-east-1, July 2026)

- `c7a.24xlarge`: spot ≈ **$1.60/hr**, on-demand **$4.93/hr**. These are
  planning examples, not the launch authority.
- A ~12 h 500-task verified run ≈ **$19 spot / $59 on-demand** + ~$3 EBS.
- The full 1,136-task run scales roughly 2.3×.
- The 1 TB gp3 data volume (provisioned 6000 IOPS / 500 MB/s) idles at
  **~$110/month** if you forget it ($0.08/GB-mo storage + ~$30/mo for the
  provisioned IOPS/throughput above the free baseline) — `06-teardown.sh`
  reminds you and only deletes it with `--delete-data`.

Spending is fail-closed. Set `SPOT_MAX_HOURLY_USD` to a positive ceiling for
Spot. If on-demand is selected or enabled as a fallback, also set
`MAX_ON_DEMAND_HOURLY_USD`; preflight and provisioning resolve the current
Linux shared-tenancy rate from the AWS Pricing API and refuse to start or
launch above the ceiling. A Spot capacity error never falls back unless
`ALLOW_ON_DEMAND_FALLBACK=1`, and non-capacity Spot errors remain fatal.

`MAX_INSTANCE_HOURS` must also be positive. Every successful provision or
restart configures instance-initiated shutdown to **stop** the instance and
replaces the Ubuntu shutdown timer with a new deadline. This limits compute
burn if the local terminal disappears, but it does not delete the instance or
its EBS volumes; stopped-instance root storage and the persistent data volume
continue to incur storage charges.

## Happy path (6 commands)

```bash
cd benchmarks/contextbench-memtrace/aws
# required once: copy the example, then set an explicit Spot and/or on-demand
# hourly ceiling plus a deliberate MAX_INSTANCE_HOURS
cp config.env.example config.env

./00-preflight.sh                 # 1  checks (quotas are the long pole)
./01-provision.sh                 # 2  reuse/start by tag, or guarded launch
./02-bootstrap.sh                 # 3  deps, models, evaluator, pre-warm (~15 min first time)
./03-run.sh                       # 4  starts the run in tmux (DATASET=verified default)
./05-evaluate-and-collect.sh      # 5  evaluator + report, artifacts to aws/state/artifacts/
./06-teardown.sh                  # 6  terminate box; data volume preserved
```

Watch progress any time with `./04-status.sh` (progress, ETA, failures,
watchdog kills, load/mem/disk). It also tells you if a spot interruption
notice arrived. For an unattended multi-hour run, prefer `./07-watch.sh` in
its own terminal — see "Watching a run" below.

`01-provision.sh` treats the `TAG_PREFIX` instance tag as an idempotency key.
It reuses a pending/running instance, waits for a stopping instance, and starts
the exact stopped instance instead of launching a duplicate. It fails if more
than one live/stopped instance has the tag or if the preserved instance type
does not match `INSTANCE_TYPE`. The on-demand price guard is applied before a
preserved on-demand instance is started or reused, not only before new
launches. Re-running `01-provision.sh` refreshes the public IP and resets the
automatic-stop deadline.

## Source-build deploy mode (benchmark an exact private build)

By default the box installs the `MEMTRACE_VERSION` pin from npm. When core
fixes are landing locally, you don't have to wait for an npm release. Use a
clean worktree at the commit to benchmark and set in `config.env`:

```bash
MEMTRACE_INSTALL_MODE=source
MEMTRACE_SOURCE_DIR=/Users/alexholmberg/Desktop/Memtrace/memtrace   # default
```

then run `./02-bootstrap.sh` as usual. In source mode it additionally:

1. **Fails closed on source state**: the normal path requires a clean Git
   worktree with initialized recursive submodules. `ALLOW_DIRTY_SOURCE=1`
   exists only for explicit diagnostics and marks the manifest
   `publishable_source: false`; do not publish a score from that override.
2. **Captures provenance locally, before anything ships**: `git rev-parse
   HEAD`, `git describe --tags --dirty --always`, recursive submodule status,
   dirty state, and `source_payload_sha256` go into `source-manifest.json`
   (`aws/state/`, gitignored). The payload digest covers a per-file
   SHA-256 list for tracked files (including tracked submodule files) plus
   non-ignored untracked files. `.git` objects are never transferred.
3. **Rsyncs the source** to `/srv/contextbench/memtrace-src` on the box
   (excludes `target/`, `node_modules/`, `.git/`). The remote `target/` is
   never deleted by re-syncs, so iteration builds are incremental. Before the
   build, the remote verifies every listed file against the captured checksum
   manifest; a mismatch aborts the bootstrap.
4. **Builds on the box** (`remote/build-memtrace-remote.sh`): installs rustup
   (minimal, stable) + build deps (`pkg-config libssl-dev cmake clang
   protobuf-compiler`), `cargo build --release` on 96 cores, then installs
   `memtrace` + every sidecar executable from `target/release` into
   `/srv/contextbench/memtrace-bin/`.
5. **Verifies before any run**: `memtrace --version` plus a smoke index of a
   toy repo in a fully isolated env (this also pre-warms the ~640MB embedding
   model with the actual binary; the npm pre-warm is skipped in source mode).
6. **Puts the built binary first on PATH for the run**: `run-remote.sh`
   prepends `/srv/contextbench/memtrace-bin` (after the nvm PATH mutation)
   and refuses to start if `command -v memtrace` resolves anywhere else.

Rebuilds are skipped when `/srv/contextbench/memtrace-bin/source-manifest.json`
already records the exact verified `source_payload_sha256` and its installed
binary still passes `memtrace --version`.

**Provenance in the results**: every run's `run_meta.json` records
`memtrace_install_mode`, the runtime `memtrace --version` string, the resolved
binary path, and (in source mode) the full `memtrace_source` manifest — HEAD
sha, `git describe --dirty`, submodule revisions, dirty/publishable state,
source-payload digest, binary sha256, rustc version, and build time. The
parallel driver's run fingerprint separately digests the resolved Memtrace
binary and adjacent source manifest before deciding whether a completed task
is resumable. A leaderboard number is never orphaned from the exact binary
that produced it.

**Privacy**: the private source never leaves your own infra — it travels
laptop → your EC2 box over ssh/rsync only, and the box is yours to
`06-teardown.sh`. Nothing is committed or published anywhere.

In source mode the default `CACHE_NAMESPACE` digests the source HEAD sha
(plus a dirty-diff sha for dirty trees) instead of the npm version pin.
The source-payload manifest and resume fingerprint are stronger than this
cache namespace: they include non-ignored untracked files too. Keep scored
runs clean; the dirty-tree namespace is only for diagnostic iteration.

## The flywheel loop (run → triage → fix → rerun)

The volume is the flywheel: datasets, venv, models, eval-repos, the rsynced
source and its incremental `target/` all persist across instances and runs.
`DATASET=train|verified|full|test` in `config.env` selects the scope of every
new run (use `train` for tuning iterations, `verified`/`full` for the real
rows).

```bash
./03-run.sh                        # 1. run the benchmark (new run-id)
./05-evaluate-and-collect.sh       # 2. evaluator + report + TRIAGE; prints the
                                   #    layer-share table (retrieval vs selector
                                   #    vs budget ...) — pick the biggest lever
# 3. fix the top layer in the private Rust repo (locally)
./02-bootstrap.sh                  # 4. source mode: re-rsync + incremental rebuild
./03-run.sh                        # 5. rerun on the same volume (fresh run-id)
```

Two run semantics, don't mix them up:

- `./03-run.sh --resume` — **finish an interrupted run**: same run-id; only
  validated successful instances whose recorded run fingerprint and artifact
  hashes still match are skipped. Failed tasks are always retried. Use after
  spot evictions/aborts.
- **After a binary/source change, start a fresh run** (plain `./03-run.sh`):
  this keeps the before/after comparison legible. Resume is fail-closed and
  will rerun successes whose binary, source manifest, policy, adapter,
  dataset, manifest, model, or behavior environment changed, so it will not
  silently preserve an old-policy row. Old runs stay on the volume for
  comparison; free the bulky `runs/` trees between stages (see below).

The driver writes `run_provenance.json` and fingerprints the driver, runner,
dataset, task manifest, reranker directory, resolved Memtrace binary and source
manifest, behavior-affecting environment, and run policy. A resume skip also
requires a successful `run_record.json` plus matching SHA-256 values for the
prediction, audit, and (when requested) query plan. A same-policy failed retry
may reuse a query plan produced before the later failure; stale plans from a
different fingerprint are archived instead of trusted.

## Per-instance watchdog

`RUN_TIMEOUT` (7200 s) is the hardened driver's last-ditch per-instance
timeout. Every runner starts in its own process session; on timeout the driver
terminates that process group, waits briefly, then kills any survivors. This
contains `runner.py` and its `memtrace mcp`/git descendants without taking down
other tasks or the driver.

On top of that, `run-remote.sh` runs a stricter wall-clock watchdog: any single
`runner.py` older than `WATCHDOG_MINUTES` (default **90**, set in `config.env`)
gets its descendant tree killed. This catches known pathological repositories
before the 2-hour driver limit. The kill is logged as `outcome=timeout` in
`results/<run-id>/watchdog.log`, a `WATCHDOG_TIMEOUT` marker lands in the
instance's run dir, and the driver classifies the nonzero worker as a failure
and continues.

Timeouts, nonzero exits, spawn errors, invalid output, missing audits/query
plans, and worker crashes all produce an atomic `failure.json`, failure audit,
and zero-context prediction stub carrying `harness_failure`. Final merge
therefore emits exactly one prediction record per manifest task, including
failures, instead of shrinking the evaluator denominator to survivors. The
driver reports successful and failed task counts separately and exits nonzero
unless every task succeeded. Its legacy empty-prediction sweep remains as a
defense for artifacts from older drivers and records any hit as
`outcome=empty_prediction` in `watchdog.log`.

The evaluate step reconciles per-task artifacts once more, runs the unmodified
upstream evaluator, and then makes `report.py` validate predictions and results
against the manifest. Evaluator errors remain in the macro as zero; any missing,
duplicate, extra, or malformed row fails closed. `--allow-partial` is an explicit
diagnostic escape hatch that zero-fills gaps and marks the row non-publishable.
Triage uses the same manifest universe. Don't set `WATCHDOG_MINUTES` low:
legit instances can run tens of minutes (dev-slice vscode: ~24 min at
concurrency 4), and a failed task is intentionally retried on `--resume`.

## Watching a run

Every progress signal above (`./04-status.sh`, `driver.log`, `watchdog.log`)
is **pull-based** — nothing pages you if the run goes quiet. On a multi-hour
unattended run on a single irreplaceable on-demand box, that is the single
most likely way to burn money without noticing (it is exactly how the
previous broken-fleet attempt was only discovered after real hours had
passed). `./07-watch.sh` closes that gap from the Mac side:

```bash
./07-watch.sh                                    # watch the latest run
POLL_MINUTES=5 STALL_MINUTES=20 ./07-watch.sh     # tighter thresholds
```

It polls `status-remote.sh` every `POLL_MINUTES` (default 10) and raises a
loud local alert (terminal bell + a bright banner + a best-effort macOS
notification/`say`) the moment any of these happen:

- **zero completions for `STALL_MINUTES`** (default 30) — a soft heuristic:
  with only `CONCURRENCY` instances in flight and legitimate instances
  taking up to `WATCHDOG_MINUTES` (90 by default), a healthy run can go
  quiet for a while by bad luck. False alarms cost a glance at the terminal;
  a missed real stall costs hours of box time, so this deliberately errs
  toward over-alerting rather than under-alerting.
- the **tmux session dies** without a recorded driver exit (the driver
  crashed instead of exiting cleanly),
- the **driver exits**, either way (it prints the final progress and rc,
  then stops watching — success is announced too, not just failure).

It only reads state over SSH; it never changes anything on the box or in
AWS, and Ctrl-C stops watching without affecting the run. Run it in its own
terminal/tab alongside (not instead of) `./04-status.sh` for the mandatory
calibration dry-run and for the full gated run.

## Gated rollout

The adapter's gate flow maps to `DATASET` in `config.env`:

| stage | DATASET | tasks | note |
|---|---|---|---|
| tuning | `train` | 450 | verified-train split |
| one-shot check | `test` | 50 | verified-test split |
| **gated run** | `verified` (default) | 500 | must clear the 10-task official-evaluator gate first: beat published GPT-5 line-context F1 0.312 (see adapter `README.md`) |
| full run | `full` | 1,136 | only after the 500-task set looks right |

The standalone adapter default is an 80-line context pack. The current scored
AWS policy deliberately sets `LINE_BUDGET=200` in the gitignored
`aws/config.env`. Do not infer the scored policy from the example-file default:
confirm the effective value in `run_meta.json`/`run_provenance.json`, keep it
fixed across comparison arms, and pass the same value to triage.

Change `DATASET`, then `./03-run.sh` (new run-id per launch; each run gets its
own `/srv/contextbench/results/<run-id>/`).

Two rules between stages:

- **A run is bound to its dataset.** `03-run.sh --resume` (and `run-remote.sh`
  via `run_meta.json`) refuse to resume a run if `DATASET` no longer matches
  the one the run was started with — flip `DATASET` back to resume, or start
  a fresh run.
- **Free disk before the next stage.** Every instance keeps its repo clone
  under `results/<run-id>/runs/<slug>/work` for the life of the run (the full
  set is ~300-450 GB of clones per run-id). After `05-evaluate-and-collect.sh`
  has collected a stage's artifacts, delete the bulky per-instance tree:
  `rm -rf /srv/contextbench/results/<old-run-id>/runs`. `run-remote.sh`
  refuses to start below a free-space floor (`DISK_FLOOR_GB`, default 150).

## Spot eviction recovery

The box runs a watcher next to the driver (poll IMDSv2
`spot/instance-action` every 30 s). On the ~2-minute notice it touches
`SPOT_INTERRUPTED` in the results dir, logs, and `sync`s. Results already live
on the persistent data volume; an eviction costs at most the in-flight
instances (~2 min of index work each).

Recovery is three commands — **02 is mandatory**: the replacement instance
has a fresh root FS (no data-volume mount, no memtrace/node/venv, no `.env`),
and skipping it makes `03-run.sh --resume` fail even though all results are
intact on the still-attached volume:

```bash
./01-provision.sh        # relaunches, pinned to the data volume's AZ, reattaches it
./02-bootstrap.sh        # re-mounts the volume + reinstalls root-FS deps + re-pushes .env
                         # (idempotent; fast on a relaunch — venv/models/datasets live on the volume)
./03-run.sh --resume     # same run-id; completed instances are skipped
```

`--resume` reads the run-id from `results/LATEST` on the volume. The driver
skips only a validated successful instance whose fingerprint and recorded
artifact hashes still match. A nonempty file alone is never trusted, and a
failure stub is never considered completed. The final `predictions.jsonl` is
rebuilt in manifest order with one real prediction or explicit zero-context
failure stub per task, so partial + resumed work merges without losing failed
tasks from the denominator.

If spot capacity moved to a different AZ than the volume:
`aws ec2 create-snapshot --volume-id <vol>`, then
`aws ec2 create-volume --snapshot-id ... --availability-zone <newAZ>`, retag
`Name=contextbench-data`, delete the old volume, re-run `01`.

## What lives where on the box

```
/home/ubuntu/contextbench-adapter/    adapter code (rsynced) + .env (scp, 600)
/srv/contextbench/                    persistent gp3 data volume (label cbdata)
├── results/<run-id>/                 manifest, run_meta, driver.log, runs/<slug>/...
│   ├── predictions.jsonl             merged predictions
│   ├── evaluation-failures.jsonl     explicit harness failures scored as zero
│   ├── results.jsonl                 evaluator output
│   ├── leaderboard-report.json       paper-compatible row (report.py)
│   ├── watchdog.log                  outcome=timeout / outcome=empty_prediction events
│   └── triage/                       triage.jsonl, triage_summary.json, triage_report.md
├── contextbench/                     upstream evaluator checkout (+ data/*.parquet)
├── venv/                             python3.11 venv (tree-sitter pins need <3.12)
├── rerank-model/                     tokenizer.json + model_int8.onnx (x86 int8, sha-checked)
├── memtrace-src/                     source mode: rsynced private repo + incremental target/
├── memtrace-bin/                     source mode: built memtrace (+sidecars) + source-manifest.json
├── graph-cache/                      reserved (see note below)
└── eval-repos/                       evaluator's shared repo clones
```

Version pins: `memtrace@0.8.21` (npm latest as of 2026-07-10, matches the dev
slice — set `MEMTRACE_VERSION` to change, or `MEMTRACE_INSTALL_MODE=source`
to build from the local private repo instead), Node 22 via nvm, Ubuntu 24.04,
reranker `cross-encoder/ms-marco-MiniLM-L12-v2` (renamed on HF: no dash in
`L12`) int8 ONNX picked by CPU flags (avx512_vnni → `qint8_avx512_vnni`, else
`quint8_avx2`), embedding model `jinaai/jina-embeddings-v2-base-code` pre-warmed
serially at bootstrap before any parallel fan-out.

### Graph cache note

`runner.py` supports `--graph-cache-dir/--cache-namespace`, but the hardened
`parallel_driver.py` does not yet forward those flags. `run-remote.sh` detects
support and will pass `/srv/contextbench/graph-cache` + `CACHE_NAMESPACE`
automatically when the driver grows that pass-through. Fingerprint-validated
driver resume already avoids re-indexing a successful completed task; the
graph cache would additionally save re-indexing for a task that finished
retrieval but failed before its success artifacts were committed. Never reuse
a cache dir across Memtrace versions — bump `CACHE_NAMESPACE` (it digests the
Memtrace version + embedding model).

## Troubleshooting

- **`MaxSpotInstanceCountExceeded` at launch** — spot vCPU quota
  (`L-34B43A08`) too low, not a capacity issue. Request 128; or set
  `USE_SPOT=0`.
- **Spot fails with capacity errors** — fallback is intentionally disabled by
  default. Re-run later or try another region. To opt in, set
  `ALLOW_ON_DEMAND_FALLBACK=1` and a positive
  `MAX_ON_DEMAND_HOURLY_USD`; provisioning still refuses the fallback when the
  live on-demand rate exceeds the cap.
- **SSH times out** — your IP changed. Re-run `01-provision.sh` (it re-adds
  your current IP to the security group and re-checks reachability).
- **`ContextBench clone has no data/full.parquet`** — the upstream repo did
  not ship the datasets in `data/`. `02-bootstrap.sh` auto-seeds from a local
  `/tmp/contextbench/data` if present; otherwise copy the `data/` dir (needs
  `full.parquet` — `train`/`test` splits lack `repo_url` and runner merges it
  from the sibling `full.parquet`) to
  `/srv/contextbench/contextbench/data/` yourself.
- **Reranker sha256 mismatch** — HF served an unexpected file; re-run
  `02-bootstrap.sh` (downloads are atomic: `.tmp` + rename). Remember plain
  `curl` without `-L` breaks on the renamed repo (307 redirect).
- **Run seems wedged** — `./04-status.sh` shows per-session rate. A wedged
  memtrace hangs one instance until the per-instance `RUN_TIMEOUT` (7200 s)
  kills its isolated process group; the run then continues and the driver
  writes a zero-context failure stub. The external watchdog normally acts
  first at `WATCHDOG_MINUTES`. The PPID-1 reaper remains a second defense for
  children orphaned by abrupt external kills or crashes.
- **Driver exit code 1** — means at least one manifest task failed even though
  `predictions.jsonl` still contains one record per task. Inspect `[FAILED]`
  lines, `driver_summary.json`, `runs/<slug>/failure.json`, and the per-instance
  `runner.log`, then use `./03-run.sh --resume` to retry failures.
- **Cost claim in the report is null** — if any audit shows
  `search_planner.source == "locked_query_plan"`, `report.py` refuses an
  average cost. This harness always runs with fresh per-instance query plans
  (`--query-plans`), which keeps costs clean; don't reuse plan files across
  runs.
- **Evaluator console numbers differ from report.py** — expected; the
  evaluator's console summary is micro-averaged and lacks F1. Only the
  `report.py` row is leaderboard-comparable.
- **Volume stuck in the wrong AZ** — see spot eviction recovery above
  (snapshot → new volume in the new AZ).
- **Instance type changed in config** — `01` refuses to reuse a tagged
  pending/running/stopped instance whose type differs from `INSTANCE_TYPE`.
  Deliberately terminate/replace that instance before provisioning the new
  type; do not expect an in-place resize from this harness.
