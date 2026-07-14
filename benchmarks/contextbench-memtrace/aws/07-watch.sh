#!/usr/bin/env bash
# 07-watch.sh — from the Mac: poll the run's progress on a fixed cadence and
# raise a loud LOCAL alert (terminal bell + bright banner + best-effort macOS
# notification/speech) the moment progress stalls, the tmux session dies
# unexpectedly, or the driver exits — instead of the operator finding out by
# happening to run ./04-status.sh hours later.
#
# Closes a gap flagged by adversarial review (2026-07-12): every existing
# progress signal in this harness (04-status.sh / status-remote.sh) is
# pull-based — nothing pages or alerts on zero delta. Given a multi-hour
# unattended run on a single irreplaceable on-demand box and zero tolerance
# for a repeat of the "launch and discover it's broken hours later" incident,
# running this loop is the recommended way to babysit any run — especially
# the mandatory pre-500-task calibration dry-run under the locked config.
#
#   ./07-watch.sh                  watch the latest run, default cadence
#   ./07-watch.sh --run-id ID      watch a specific run
#   POLL_MINUTES=5 STALL_MINUTES=20 ./07-watch.sh   tighter thresholds
#
# This script only reads state over SSH (via status-remote.sh) — it never
# changes anything on the box or in AWS. Ctrl-C stops watching at any time;
# the run itself is completely unaffected.
# shellcheck source-path=SCRIPTDIR

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

RUN_ID=""
while [ $# -gt 0 ]; do
    case "$1" in
        --run-id) RUN_ID="${2:?--run-id needs a value}"; shift ;;
        *) die "unknown argument: $1 (usage: 07-watch.sh [--run-id ID])" ;;
    esac
    shift
done

: "${POLL_MINUTES:=10}"    # how often to check
# Soft heuristic, deliberately erring toward over-alerting: with only
# CONCURRENCY slots in flight and legitimate instances running up to
# WATCHDOG_MINUTES (default 90), a completely healthy run CAN go quiet for a
# while by bad luck. False alarms here cost a glance at the terminal; a
# missed real stall costs hours of on-demand box time — the asymmetry the
# user explicitly does not want to repeat.
: "${STALL_MINUTES:=30}"
[[ "$POLL_MINUTES" =~ ^[0-9]+$ ]] || die "POLL_MINUTES must be a positive integer"
[[ "$STALL_MINUTES" =~ ^[0-9]+$ ]] || die "STALL_MINUTES must be a positive integer"

alert() { # loud, best-effort, multi-channel; never fails the loop
    local msg="$*"
    printf '\a\a\a'
    printf '\033[1;41;97m ALERT \033[0m \033[1;31m%s\033[0m\n' "$msg" >&2
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification $(printf '%q' "$msg") with title \"ContextBench\" sound name \"Basso\"" \
            >/dev/null 2>&1 || true
    fi
    if command -v say >/dev/null 2>&1; then
        say "ContextBench alert" >/dev/null 2>&1 || true
    fi
}

IP="$(instance_ip)"
info "watching run on $IP — poll every ${POLL_MINUTES}m, alert after ${STALL_MINUTES}m with zero progress"
info "(Ctrl-C stops watching; the run on the box keeps going either way)"

LAST_COMPLETED=""
LAST_CHANGE_EPOCH="$(date +%s)"

while true; do
    OUT="$(remote "bash $REMOTE_ADAPTER_DIR/aws/remote/status-remote.sh $(printf '%q' "$TMUX_SESSION") $(printf '%q' "$RUN_ID")" 2>&1)" || {
        alert "SSH/status check failed — box may be unreachable. Last output: ${OUT:-<none>}"
        sleep "$((POLL_MINUTES * 60))"
        continue
    }

    RUN_LINE="$(printf '%s\n' "$OUT" | grep -m1 '^=== run:' || true)"
    TMUX_LINE="$(printf '%s\n' "$OUT" | grep -m1 '^tmux' || true)"
    PROGRESS_LINE="$(printf '%s\n' "$OUT" | grep -m1 '^progress:' || true)"
    DRIVER_LINE="$(printf '%s\n' "$OUT" | grep -m1 '^driver  :' || true)"
    COMPLETED="$(printf '%s' "$PROGRESS_LINE" | grep -oE '^progress: [0-9]+' | grep -oE '[0-9]+' || true)"
    NOW="$(date +%s)"

    printf '%s  %s | %s | %s\n' "$(date -u +%H:%M:%SZ)" "${RUN_LINE:-(no run line)}" "${TMUX_LINE:-?}" "${PROGRESS_LINE:-(no progress line yet)}"

    if [ -n "$DRIVER_LINE" ]; then
        info "driver has exited: $DRIVER_LINE — stopping watch (run ./05-evaluate-and-collect.sh next)"
        if printf '%s\n' "$DRIVER_LINE" | grep -q 'rc=0'; then
            alert "run finished OK: $PROGRESS_LINE"
        else
            alert "run exited with a NONZERO code: $DRIVER_LINE"
        fi
        exit 0
    fi

    if ! printf '%s' "$TMUX_LINE" | grep -q RUNNING; then
        alert "tmux session '$TMUX_SESSION' is not running and no driver exit was recorded — it may have crashed. SSH in and check driver.log."
    fi

    if [ -n "$COMPLETED" ]; then
        if [ "$COMPLETED" != "$LAST_COMPLETED" ]; then
            LAST_COMPLETED="$COMPLETED"
            LAST_CHANGE_EPOCH="$NOW"
        else
            STALLED_FOR=$(( (NOW - LAST_CHANGE_EPOCH) / 60 ))
            if [ "$STALLED_FOR" -ge "$STALL_MINUTES" ]; then
                alert "ZERO progress for ${STALLED_FOR}m (still: $PROGRESS_LINE) — check ./04-status.sh and driver.log now."
            fi
        fi
    fi

    sleep "$((POLL_MINUTES * 60))"
done
