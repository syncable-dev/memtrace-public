#!/usr/bin/env bash
# status-remote.sh — runs ON THE BOX. Prints run progress + host health.
# Usage: status-remote.sh <tmux-session> [run-id]

set -euo pipefail

TMUX_SESSION="${1:-contextbench}"
RUN_ID="${2:-}"
DATA_ROOT=/srv/contextbench

if [ -z "$RUN_ID" ]; then
    RUN_ID="$(cat "$DATA_ROOT/results/LATEST" 2>/dev/null || true)"
fi
[ -n "$RUN_ID" ] || { echo "no run found (no $DATA_ROOT/results/LATEST) — start one with 03-run.sh"; exit 1; }
RESULTS="$DATA_ROOT/results/$RUN_ID"
[ -d "$RESULTS" ] || { echo "run dir $RESULTS does not exist"; exit 1; }

echo "=== run: $RUN_ID ==="
if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "tmux    : RUNNING (attach: tmux attach -t $TMUX_SESSION)"
else
    echo "tmux    : not running"
fi
if [ -f "$RESULTS/driver_exit" ]; then
    echo "driver  : exited rc=$(cat "$RESULTS/driver_exit")"
fi
if [ -f "$RESULTS/SPOT_INTERRUPTED" ]; then
    echo "!!      : SPOT INTERRUPTION NOTICE received ($(date -u -r "$RESULTS/SPOT_INTERRUPTED" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)) — after eviction: ./01-provision.sh && ./02-bootstrap.sh && ./03-run.sh --resume"
fi

TOTAL="?"
if [ -f "$RESULTS/manifest.json" ]; then
    TOTAL="$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(len(d["tasks"]) if isinstance(d,dict) else len(d))' "$RESULTS/manifest.json" 2>/dev/null || echo '?')"
fi
COMPLETED="$(find "$RESULTS/runs" -mindepth 2 -maxdepth 2 -name prediction.jsonl -size +0c 2>/dev/null | wc -l | tr -d ' ')"
echo "progress: $COMPLETED / $TOTAL predictions"

if [ -f "$RESULTS/session_start" ] && [ "$TOTAL" != "?" ]; then
    read -r T0 C0 < "$RESULTS/session_start"
    NOW="$(date +%s)"
    awk -v t0="$T0" -v c0="$C0" -v now="$NOW" -v c="$COMPLETED" -v total="$TOTAL" 'BEGIN {
        elapsed = now - t0
        done = c - c0
        if (done > 0 && elapsed > 0) {
            rate = done / elapsed
            remaining = (total - c) / rate
            printf "rate    : %.1f tasks/hour (this session)\n", rate * 3600
            printf "ETA     : ~%.1f h remaining\n", remaining / 3600
        } else {
            printf "ETA     : n/a (no completions yet this session; %d min elapsed)\n", elapsed / 60
        }
    }'
fi

echo "--- recent failures ---"
# TimeoutExpired: a timed-out instance surfaces as an uncaught traceback in
# driver.log (the stock driver prints no [FAILED] line for timeouts).
grep -E '\[FAILED\]|TimeoutExpired' "$RESULTS/driver.log" 2>/dev/null | tail -5 || true
if [ -s "$RESULTS/watchdog.log" ]; then
    echo "--- watchdog (timeouts + rc=0 empty predictions) ---"
    tail -5 "$RESULTS/watchdog.log"
fi
echo "--- last driver output ---"
tail -n 5 "$RESULTS/driver.log" 2>/dev/null || echo "(no driver.log yet)"
echo "--- host ---"
uptime
free -h | sed -n '1,2p'
df -h / "$DATA_ROOT" | sed -n '1,3p'
# pgrep -fc prints '0' AND exits 1 on no match, so an '|| echo 0' fallback
# would render '0 0'; count lines instead. Match both the unpinned "memtrace
# mcp" cmdline and the taskset-shim's "memtrace.real mcp" (does not contain
# "memtrace mcp" as a substring — see run-remote.sh reap_orphans()); under
# the actual locked production config only the .real form ever appears, so
# without this a healthy 12-wide run would always show "0" here.
echo "memtrace mcp processes: $({ pgrep -f 'memtrace mcp'; pgrep -f 'memtrace\.real mcp'; } 2>/dev/null | sort -un | wc -l | tr -d ' ')"
