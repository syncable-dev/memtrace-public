#!/usr/bin/env bash
# 03-run.sh — start (or resume) the benchmark on the box inside tmux.
#
#   ./03-run.sh                 new run: results in /srv/contextbench/results/<run-id>/
#   ./03-run.sh --resume        resume the latest run (skips completed instances)
#   ./03-run.sh --run-id ID     target a specific run id (with or without --resume)
#
# The driver always runs with --resume semantics (parallel_driver.py skips
# instances whose prediction.jsonl is already non-empty), so re-running the
# same run-id is always safe. A spot-interruption watcher runs alongside the
# driver (see aws/remote/spot-watcher.sh).
# shellcheck source-path=SCRIPTDIR

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

RESUME=0
RUN_ID=""
while [ $# -gt 0 ]; do
    case "$1" in
        --resume) RESUME=1 ;;
        --run-id) RUN_ID="${2:?--run-id needs a value}"; shift ;;
        *) die "unknown argument: $1 (usage: 03-run.sh [--resume] [--run-id ID])" ;;
    esac
    shift
done

# Fail fast (clear message) if 01-provision.sh has not run.
IP="$(instance_ip)"

# --- refuse to double-start ---------------------------------------------------
if remote "tmux has-session -t $TMUX_SESSION" 2>/dev/null; then
    info "tmux session '$TMUX_SESSION' is already running on the box."
    info "watch it:   ./04-status.sh"
    info "attach:     ssh -i $SSH_KEY_PATH $REMOTE_USER@$IP -t tmux attach -t $TMUX_SESSION"
    exit 0
fi

# --- resolve run id --------------------------------------------------------------
# A fresh instance after a spot eviction has the data volume ATTACHED but NOT
# MOUNTED (and no deps): results/LATEST "missing" would falsely read as "the
# run is gone", and a new run would land on the ephemeral root FS. Diagnose
# that case explicitly, for every path.
remote "mountpoint -q /srv/contextbench" \
    || die "/srv/contextbench is not a mounted volume on the box — run ./02-bootstrap.sh first (mandatory after any relaunch/eviction; results from previous runs are intact on the data volume)"
if [ -z "$RUN_ID" ]; then
    if [ "$RESUME" = "1" ]; then
        RUN_ID="$(remote "cat /srv/contextbench/results/LATEST 2>/dev/null" || true)"
        [ -n "$RUN_ID" ] || die "--resume: no previous run found (results/LATEST missing on the mounted data volume)"
        info "resuming run: $RUN_ID"
    else
        RUN_ID="run-$(date -u +%Y%m%d-%H%M%S)-$DATASET"
        info "new run: $RUN_ID"
    fi
fi

# Cross-check: auto-generated run ids embed their dataset; refuse to resume a
# run under a different DATASET (run-remote.sh enforces this again from
# run_meta.json, but failing here is clearer and cheaper).
case "$RUN_ID" in
    run-*-verified|run-*-full|run-*-train|run-*-test)
        RUN_ID_DATASET="${RUN_ID##*-}"
        [ "$RUN_ID_DATASET" = "$DATASET" ] || die "run '$RUN_ID' was created with DATASET=$RUN_ID_DATASET but config now says DATASET=$DATASET — set DATASET=$RUN_ID_DATASET to resume it, or start a new run (no --resume) for $DATASET"
        ;;
esac

# --- sync latest adapter code (picks up the --resume driver patch, etc.) ----------
info "syncing adapter code to the box"
remote "mkdir -p $REMOTE_ADAPTER_DIR"
rsync_adapter

remote "test -s $REMOTE_ADAPTER_DIR/.env" \
    || die "no .env on the box — run ./02-bootstrap.sh first"

# Source mode: the run must use the source-built binary; fail here (clear
# message) rather than inside tmux if the build step never ran on this box.
# STALENESS GATE: existence is not enough — an old binary from a previous
# bootstrap silently benchmarks the WRONG code (a landed fix would be judged
# ineffective). Compare the box's build provenance against the current local
# tree (same head_sha + dirty-diff sha recipe as 02-bootstrap.sh) and refuse
# to run on mismatch unless ALLOW_STALE_SOURCE=1 is set explicitly.
if [ "$MEMTRACE_INSTALL_MODE" = "source" ]; then
    remote "test -x /srv/contextbench/memtrace-bin/memtrace && test -s /srv/contextbench/memtrace-bin/source-manifest.json" \
        || die "MEMTRACE_INSTALL_MODE=source but no built binary on the box — run ./02-bootstrap.sh (it rsyncs the source and builds)"

    REMOTE_MANIFEST="$(remote "cat /srv/contextbench/memtrace-bin/source-manifest.json")"
    read -r REMOTE_HEAD REMOTE_DIFF <<<"$(printf '%s' "$REMOTE_MANIFEST" | python3 -c '
import json, sys
m = json.load(sys.stdin)
print(m.get("head_sha") or "", m.get("dirty_diff_sha256_16") or "-")
')"
    if [ "$REMOTE_DIFF" = "-" ]; then REMOTE_DIFF=""; fi

    # --no-optional-locks: never write .git/index in the actively-edited repo.
    LOCAL_HEAD="$(git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" rev-parse HEAD 2>/dev/null || true)"
    LOCAL_DIFF=""
    if [ -n "$(git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" status --porcelain 2>/dev/null)" ]; then
        LOCAL_DIFF="$(git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" diff HEAD 2>/dev/null | shasum -a 256 | cut -c1-16)"
    fi
    if [ "$LOCAL_HEAD" != "$REMOTE_HEAD" ] || [ "$LOCAL_DIFF" != "$REMOTE_DIFF" ]; then
        if [ "${ALLOW_STALE_SOURCE:-0}" = "1" ]; then
            warn "ALLOW_STALE_SOURCE=1: running the BOX's binary (head=${REMOTE_HEAD:0:12} diff=${REMOTE_DIFF:-clean}), NOT the current local tree (head=${LOCAL_HEAD:0:12} diff=${LOCAL_DIFF:-clean})"
        else
            die "stale source binary on the box: built from head=${REMOTE_HEAD:0:12} diff=${REMOTE_DIFF:-clean} but local tree is head=${LOCAL_HEAD:0:12} diff=${LOCAL_DIFF:-clean} — run ./02-bootstrap.sh to rebuild, or ALLOW_STALE_SOURCE=1 ./03-run.sh to knowingly benchmark the box's binary"
        fi
    fi

    # Provenance-consistent cache namespace: derive it from the REMOTE
    # manifest (what actually runs) so run_meta.json can never record a
    # cache_namespace sha that contradicts its own memtrace_source manifest.
    # Only auto-derived namespaces are overridden; explicit user values win.
    case "$CACHE_NAMESPACE" in
        contextbench-src*-jina-code-768-v1)
            _remote_dirty_suffix=""
            [ -n "$REMOTE_DIFF" ] && _remote_dirty_suffix="-dirty$(printf '%s' "$REMOTE_DIFF" | cut -c1-8)"
            CACHE_NAMESPACE="contextbench-src$(printf '%s' "$REMOTE_HEAD" | cut -c1-12)${_remote_dirty_suffix}-jina-code-768-v1"
            unset _remote_dirty_suffix
            ;;
    esac
fi

# --- launch in tmux ----------------------------------------------------------------
remote "mkdir -p /srv/contextbench/results && printf '%s\n' $(printf '%q' "$RUN_ID") > /srv/contextbench/results/LATEST"

INNER="RUN_ID=$(printf '%q' "$RUN_ID")"
INNER+=" DATASET=$(printf '%q' "$DATASET")"
INNER+=" MANIFEST_LIMIT=$(printf '%q' "$MANIFEST_LIMIT")"
INNER+=" MANIFEST_SOURCE_RUN_ID=$(printf '%q' "$MANIFEST_SOURCE_RUN_ID")"
INNER+=" CONCURRENCY=$(printf '%q' "$CONCURRENCY")"
INNER+=" LINE_BUDGET=$(printf '%q' "$LINE_BUDGET")"
INNER+=" SELECTOR_MODEL=$(printf '%q' "$SELECTOR_MODEL")"
INNER+=" SELECTOR_MODE=$(printf '%q' "$SELECTOR_MODE")"
INNER+=" POST_SELECTOR_POLICY=$(printf '%q' "$POST_SELECTOR_POLICY")"
INNER+=" CACHE_NAMESPACE=$(printf '%q' "$CACHE_NAMESPACE")"
INNER+=" RUN_TIMEOUT=$(printf '%q' "$RUN_TIMEOUT")"
INNER+=" WATCHDOG_MINUTES=$(printf '%q' "$WATCHDOG_MINUTES")"
INNER+=" MEMTRACE_INSTALL_MODE=$(printf '%q' "$MEMTRACE_INSTALL_MODE")"
# Locked retrieval policy (LOCKED_POLICY.md) — runner.py reads these two
# straight from os.environ. run-remote.sh has matching "${VAR:=...}"
# defaults, but forward explicitly here too so a config.env override
# actually reaches the box instead of silently being shadowed by the
# remote-side default.
INNER+=" CB_SEARCH_LIMIT=$(printf '%q' "$CB_SEARCH_LIMIT")"
INNER+=" CB_PACK_POLICY=$(printf '%q' "$CB_PACK_POLICY")"
INNER+=" CB_QUERY_STRATEGY=$(printf '%q' "$CB_QUERY_STRATEGY")"
# Core-pinning opt-out escape hatch (see run-remote.sh install_taskset_shim);
# forwarded only if the operator set it explicitly, so run-remote.sh's own
# default (1 = pin enabled) applies otherwise.
if [ -n "${MEMTRACE_PIN_ENABLE:-}" ]; then
    INNER+=" MEMTRACE_PIN_ENABLE=$(printf '%q' "$MEMTRACE_PIN_ENABLE")"
fi
INNER+=" bash $REMOTE_ADAPTER_DIR/aws/remote/run-remote.sh"

remote "tmux new-session -d -s $TMUX_SESSION $(printf '%q' "$INNER")"

info "benchmark started in tmux session '$TMUX_SESSION' (run: $RUN_ID, dataset: $DATASET)"
printf '\n  watch progress : ./04-status.sh\n'
printf '  attach         : ssh -i %s %s@%s -t tmux attach -t %s\n' \
    "$SSH_KEY_PATH" "$REMOTE_USER" "$IP" "$TMUX_SESSION"
printf '  driver log     : ssh -i %s %s@%s tail -f /srv/contextbench/results/%s/driver.log\n' \
    "$SSH_KEY_PATH" "$REMOTE_USER" "$IP" "$RUN_ID"
printf '  when finished  : ./05-evaluate-and-collect.sh\n\n'
