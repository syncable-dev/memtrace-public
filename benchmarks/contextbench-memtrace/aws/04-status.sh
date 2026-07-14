#!/usr/bin/env bash
# 04-status.sh — from the Mac: show driver progress, tmux liveness, load,
# memory, disk, recent failures and an ETA for the run on the box.
#
#   ./04-status.sh              status of the latest run
#   ./04-status.sh --run-id ID  status of a specific run
# shellcheck source-path=SCRIPTDIR

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

RUN_ID=""
while [ $# -gt 0 ]; do
    case "$1" in
        --run-id) RUN_ID="${2:?--run-id needs a value}"; shift ;;
        *) die "unknown argument: $1 (usage: 04-status.sh [--run-id ID])" ;;
    esac
    shift
done

remote "bash $REMOTE_ADAPTER_DIR/aws/remote/status-remote.sh $(printf '%q' "$TMUX_SESSION") $(printf '%q' "$RUN_ID")"
