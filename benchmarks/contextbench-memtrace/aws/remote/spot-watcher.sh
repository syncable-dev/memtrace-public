#!/usr/bin/env bash
# spot-watcher.sh — runs ON THE BOX next to the driver (started by run-remote.sh).
# Polls the IMDSv2 spot instance-action endpoint every 30s. On a termination
# notice (~2 min warning): touch a sentinel in the results dir, log, and sync.
# Results already live on the persistent data volume, so sync is the main
# flush; the driver's --resume path redoes only in-flight instances.
#
# On on-demand instances the endpoint always 404s and this loops harmlessly.

set -euo pipefail

RESULTS="${1:?usage: spot-watcher.sh <results-dir>}"
IMDS=http://169.254.169.254/latest
SENTINEL="$RESULTS/SPOT_INTERRUPTED"

echo "[spot-watcher] started $(date -u +%Y-%m-%dT%H:%M:%SZ) watching for interruption"
while true; do
    TOKEN="$(curl -sf -X PUT "$IMDS/api/token" \
        -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' 2>/dev/null || true)"
    if [ -n "$TOKEN" ]; then
        if BODY="$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" \
                "$IMDS/meta-data/spot/instance-action" 2>/dev/null)"; then
            echo "[spot-watcher] INTERRUPTION NOTICE $(date -u +%Y-%m-%dT%H:%M:%SZ): $BODY"
            touch "$SENTINEL"
            sync
            echo "[spot-watcher] sentinel written to $SENTINEL; results dir synced"
            exit 0
        fi
    fi
    sleep 30
done
