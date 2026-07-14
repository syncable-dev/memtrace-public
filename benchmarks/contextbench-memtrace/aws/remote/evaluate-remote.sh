#!/usr/bin/env bash
# evaluate-remote.sh — runs ON THE BOX. Rebuilds the merged predictions from
# per-instance files (authoritative even after a resumed/interrupted run),
# runs the unmodified upstream evaluator, then report.py.
# Usage: evaluate-remote.sh RUN_ID=<id>

set -euo pipefail

for kv in "$@"; do
    case "$kv" in *=*) export "${kv%%=*}"="${kv#*=}" ;; esac
done

DATA_ROOT=/srv/contextbench
ADAPTER="$HOME/contextbench-adapter"
VENV_PY="$DATA_ROOT/venv/bin/python"

: "${RUN_ID:=$(cat "$DATA_ROOT/results/LATEST" 2>/dev/null)}"
: "${ALLOW_PARTIAL:=0}"
case "$ALLOW_PARTIAL" in 0|1) ;; *) echo "ERROR: ALLOW_PARTIAL must be 0 or 1"; exit 2 ;; esac
[ -n "${RUN_ID:-}" ] || { echo "ERROR: RUN_ID not given and no results/LATEST"; exit 1; }
RESULTS="$DATA_ROOT/results/$RUN_ID"
[ -d "$RESULTS" ] || { echo "ERROR: $RESULTS does not exist"; exit 1; }
[ -x "$VENV_PY" ] || { echo "ERROR: venv missing (run 02-bootstrap.sh)"; exit 1; }
if [ ! -s "$RESULTS/driver_exit" ] && [ "$ALLOW_PARTIAL" != "1" ]; then
    echo "ERROR: driver_exit is missing — refusing an unfinished run (set ALLOW_PARTIAL=1 for a non-publishable diagnostic)" >&2
    exit 1
fi

GOLD="$("$VENV_PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["gold_parquet"])' "$RESULTS/run_meta.json")"
SELECTOR="$("$VENV_PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("selector_model","gpt-5"))' "$RESULTS/run_meta.json")"
LINE_BUDGET="$("$VENV_PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("line_budget",80))' "$RESULTS/run_meta.json")"
[ -f "$GOLD" ] || { echo "ERROR: gold parquet $GOLD missing"; exit 1; }

# --- 1. rebuild one prediction per manifest task, in manifest order ----------------
echo "[evaluate] reconciling per-instance predictions against the manifest"
rm -f "$RESULTS/results.jsonl" "$RESULTS/leaderboard-report.json"
"$VENV_PY" "$ADAPTER/reconcile_predictions.py" --results-dir "$RESULTS"

# --- 2. upstream evaluator (unmodified; needs cwd = checkout for the package import) ---
echo "[evaluate] running python -m contextbench.evaluate (gold: $GOLD)"
cd "$DATA_ROOT/contextbench"
"$VENV_PY" -m contextbench.evaluate \
    --gold "$GOLD" \
    --pred "$RESULTS/predictions.jsonl" \
    --out "$RESULTS/results.jsonl" \
    --cache "$DATA_ROOT/eval-repos"

# --- 3. paper-compatible report ------------------------------------------------------
echo "[evaluate] running report.py"
cd "$ADAPTER"
REPORT_ARGS=(
    report.py \
    --predictions "$RESULTS/predictions.jsonl" \
    --results "$RESULTS/results.jsonl" \
    --manifest "$RESULTS/manifest.json" \
    --audit-dir "$RESULTS/predictions-audit" \
    --model "Memtrace + $SELECTOR" \
    --output "$RESULTS/leaderboard-report.json"
)
[ "$ALLOW_PARTIAL" = "1" ] && REPORT_ARGS+=(--allow-partial)
"$VENV_PY" "${REPORT_ARGS[@]}"

echo "[evaluate] leaderboard-report.json:"
"$VENV_PY" -m json.tool "$RESULTS/leaderboard-report.json"

# --- 4. post-hoc triage (layer attribution: retrieval/selector/budget/...) -----------
# Gold data is read ONLY here, AFTER predictions exist — it never feeds
# retrieval. Triage is best-effort: a missing or failing triage.py must never
# fail the evaluation (the leaderboard row above already landed).
if [ -f "$ADAPTER/triage.py" ]; then
    echo "[evaluate] running triage.py"
    mkdir -p "$RESULTS/triage"
    set +e
    # --audit-dir points at runs/ (NOT the merged predictions-audit/): the
    # flat dir only contains instances that FINISHED, so hung/watchdog-killed
    # instances would be invisible to triage. runs/ has a dir for every
    # attempted instance (find_audit handles that layout), and --manifest
    # makes the run manifest the authoritative task universe so even
    # never-attempted instances are counted.
    TRIAGE_MANIFEST_ARGS=()
    [ -s "$RESULTS/manifest.json" ] && TRIAGE_MANIFEST_ARGS=(--manifest "$RESULTS/manifest.json")
    "$VENV_PY" "$ADAPTER/triage.py" \
        --gold "$GOLD" \
        --predictions "$RESULTS/predictions.jsonl" \
        --results "$RESULTS/results.jsonl" \
        --audit-dir "$RESULTS/runs" \
        "${TRIAGE_MANIFEST_ARGS[@]}" \
        --line-budget "$LINE_BUDGET" \
        --out-dir "$RESULTS/triage"
    TRIAGE_RC=$?
    set -e
    if [ "$TRIAGE_RC" -ne 0 ]; then
        echo "[evaluate] WARN: triage.py exited rc=$TRIAGE_RC — evaluation results above are unaffected" >&2
    else
        echo "[evaluate] triage artifacts: $(find "$RESULTS/triage" -maxdepth 1 -type f -exec basename {} \; 2>/dev/null | sort | tr '\n' ' ')"
    fi
else
    echo "[evaluate] WARN: $ADAPTER/triage.py not present — skipping triage (rsync the adapter after it lands, then re-run 05)" >&2
fi
sync
