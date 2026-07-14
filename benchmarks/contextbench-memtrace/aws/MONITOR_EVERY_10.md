# Every-10 AWS evaluation monitor

`monitor-every-10.sh` watches one explicit AWS run and evaluates each newly
completed group of ten against one explicit old control artifact. It never
uses `LATEST`, never stops or kills the remote run, and never copies worktrees
or large runner logs.

```bash
./aws/monitor-every-10.sh \
  --run-id run-YYYYMMDD-HHMMSS-verified \
  --control /absolute/path/to/old-control-artifact \
  --checkpoint-root /absolute/path/to/checkpoints-for-this-run \
  --runtime-manifest /absolute/path/to/runtime-manifest.json \
  --evaluator /absolute/path/to/contextbench/evaluate.py \
  --cache-dir /absolute/path/to/evaluator-cache
```

The AWS host, user, and SSH key come from `aws/config.env` and
`aws/state/state.json`. Use `--once` for one poll. Use `--poll-seconds N` to
change the default 30-second interval.

For every poll, the remote host validates the run ID, manifest universe,
terminal IDs, task path segments, and regular-file boundaries before GNU tar
is allowed to archive anything. The archive uses PAX format. The local side
rejects absolute paths, `..`, links, devices, unindexed members, hash changes,
and any loss of the authoritative remote nanosecond mtimes. The cumulative
mirror is create-once; a terminal artifact that changes after first capture is
an error.

For each exact batch the monitor runs `freeze-checkpoints`, `seal-batch`, and
`compare-checkpoint` with `--candidate-treatment offline-packing-v2`. Receipts,
snapshots, and comparisons are immutable. A valid comparison whose gate fails
is recorded as a regression and monitoring continues; invalid comparator
output, SSH failure, tar validation failure, or evaluator failure stops the
monitor while leaving AWS untouched. An atomic local lock prevents two
monitors from evaluating the same run.

## N=100 handoff

The tenth batch is sealed normally. The N=100 comparator additionally requires
the explicit, complete collected candidate artifact: official full results,
report, triage, driver summary, and all terminal task artifacts. Supply it with:

```bash
./aws/monitor-every-10.sh ... \
  --complete-candidate-artifact /absolute/path/to/collected-run \
  --once
```

If it is not available, the monitor exits `3` after writing
`N100-HANDOFF.json`. That is a collection handoff, not a failed evaluation and
not permission to rerun any benchmark task. Exit `2` means monitoring failed
closed. Exit `0` means the requested polling/comparison work completed; the
comparison JSON remains the source of truth for PASS versus regression.
