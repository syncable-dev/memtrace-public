#!/usr/bin/env bash
# 05-evaluate-and-collect.sh — on the box: rebuild predictions.jsonl from the
# per-instance outputs, run the UNMODIFIED upstream evaluator
# (python3.11 venv, python -m contextbench.evaluate), then report.py for the
# paper-compatible leaderboard row, then triage.py (best-effort layer
# attribution; post-hoc gold access only). Then rsync artifacts back to
# aws/state/artifacts/<run-id>/ and print the headline metrics + the triage
# layer-share table.
#
#   ./05-evaluate-and-collect.sh              latest run
#   ./05-evaluate-and-collect.sh --run-id ID  specific run
#   ./05-evaluate-and-collect.sh --allow-partial  diagnostic zero-filled row
# shellcheck source-path=SCRIPTDIR

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

RUN_ID=""
ALLOW_PARTIAL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --run-id) RUN_ID="${2:?--run-id needs a value}"; shift ;;
        --allow-partial) ALLOW_PARTIAL=1 ;;
        *) die "unknown argument: $1 (usage: 05-evaluate-and-collect.sh [--run-id ID] [--allow-partial])" ;;
    esac
    shift
done
if [ -z "$RUN_ID" ]; then
    RUN_ID="$(remote "cat /srv/contextbench/results/LATEST 2>/dev/null" || true)"
    [ -n "$RUN_ID" ] || die "no run found on the box (results/LATEST missing)"
fi
info "evaluating run: $RUN_ID"

if remote "tmux has-session -t $TMUX_SESSION" 2>/dev/null; then
    [ "$ALLOW_PARTIAL" = "1" ] || die \
        "tmux session '$TMUX_SESSION' is still running — refusing a partial leaderboard row (use --allow-partial for a zero-filled, non-publishable diagnostic)"
    warn "tmux session '$TMUX_SESSION' is still running — producing a zero-filled diagnostic explicitly marked non-publishable"
fi

# --- sync adapter (so a triage.py that landed after 03-run reaches the box) -----
info "syncing adapter code to the box"
rsync_adapter

# --- run evaluator + report + triage on the box ----------------------------------
remote "bash $REMOTE_ADAPTER_DIR/aws/remote/evaluate-remote.sh RUN_ID=$(printf '%q' "$RUN_ID") ALLOW_PARTIAL=$ALLOW_PARTIAL"

# --- collect artifacts locally ---------------------------------------------------
IP="$(instance_ip)"
DEST="$STATE_DIR/artifacts/$RUN_ID"
mkdir -p "$DEST"
info "collecting artifacts -> $DEST"
rsync -az \
    --exclude 'runs/' \
    -e "$(ssh_cmd_string)" \
    "$REMOTE_USER@$IP:/srv/contextbench/results/$RUN_ID/" \
    "$DEST/"

# --- headline metrics -----------------------------------------------------------------
REPORT="$DEST/leaderboard-report.json"
if [ -f "$REPORT" ]; then
    printf '\n=== leaderboard row (paper-compatible, from report.py) ===\n'
    python3 -m json.tool "$REPORT"
else
    die "leaderboard-report.json missing from collected artifacts — check the remote evaluate output above"
fi

# --- triage layer-share table (post-hoc failure attribution) ----------------------------
# The evaluate step runs triage.py on the box (best-effort); its artifacts
# (triage.jsonl, triage_summary.json, triage_report.md) come back inside
# $DEST/triage/ via the rsync above. Degrade gracefully if triage was absent.
TRIAGE_SUMMARY="$DEST/triage/triage_summary.json"
if [ -f "$TRIAGE_SUMMARY" ]; then
    printf '\n=== triage: layer shares (which layer loses the F1) ===\n'
    python3 - "$TRIAGE_SUMMARY" <<'PY'
import json, sys

# Schema per triage.py aggregate(): num_tasks, macro_line_f1,
# macro_cap_f1_at_budget, layer_counts, layer_share, next_lever_ranking, ...
summary = json.load(open(sys.argv[1]))
shares = summary.get("layer_share") or {}
counts = summary.get("layer_counts") or {}

if shares or counts:
    print(
        f"tasks={summary.get('num_tasks')}  "
        f"macro line F1={summary.get('macro_line_f1')}  "
        f"cap@budget={summary.get('macro_cap_f1_at_budget')}"
    )
    layers = sorted(set(shares) | set(counts), key=lambda k: -shares.get(k, 0.0))
    width = max(max(len(k) for k in layers), len("layer"))
    print(f"  {'layer':<{width}}  {'share':>7}  tasks")
    for layer in layers:
        share = shares.get(layer)
        share_txt = f"{share:.1%}" if isinstance(share, (int, float)) else "-"
        print(f"  {layer:<{width}}  {share_txt:>7}  {counts.get(layer, '-')}")
    levers = summary.get("next_lever_ranking") or []
    if levers:
        top = levers[0]
        print(
            f"next lever: {top.get('layer')} "
            f"(primary on {top.get('tasks_primary')} tasks, "
            f"est macro line-F1 gain +{top.get('est_macro_line_f1_gain')})"
        )
else:  # schema drifted — show everything rather than nothing
    json.dump(summary, sys.stdout, indent=2)
    print()
PY
    printf 'full narrative: %s/triage/triage_report.md\n' "$DEST"
else
    warn "no triage summary collected ($TRIAGE_SUMMARY missing) — triage.py absent or failed on the box (see evaluate output above)"
fi

printf '\nartifacts: %s\n' "$DEST"
warn "leaderboard numbers come from report.py above — the evaluator's own console summary is micro-averaged and lacks F1; never paste that one into a leaderboard row."
