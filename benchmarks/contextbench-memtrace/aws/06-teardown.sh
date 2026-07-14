#!/usr/bin/env bash
# 06-teardown.sh — terminate the instance. The data volume is PRESERVED by
# default (it holds results + caches and is never delete-on-termination).
#
#   ./06-teardown.sh                interactive confirm, keep data volume
#   ./06-teardown.sh --yes          no prompt, keep data volume
#   ./06-teardown.sh --delete-data  ALSO delete the data volume (final cleanup)
#   ./06-teardown.sh --delete-sg    also delete the security group
# shellcheck source-path=SCRIPTDIR

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

require_cmd aws

# Fail closed BEFORE touching anything: an AWS API error (expired SSO session
# after a 12h run, throttling, wrong AWS_PROFILE) must never be mistaken for
# "instance already terminated" — that would skip termination, wipe
# state.json, print success, and leave a $4.93/hr box billing forever.
info "verifying AWS credentials"
aws sts get-caller-identity --query Arn --output text >/dev/null \
    || die "AWS credentials not usable (expired SSO session? wrong AWS_PROFILE?) — NOTHING was terminated; fix credentials (e.g. 'aws sso login') and re-run"

ASSUME_YES=0
DELETE_DATA=0
DELETE_SG=0
while [ $# -gt 0 ]; do
    case "$1" in
        --yes) ASSUME_YES=1 ;;
        --delete-data) DELETE_DATA=1 ;;
        --delete-sg) DELETE_SG=1 ;;
        *) die "unknown argument: $1 (usage: 06-teardown.sh [--yes] [--delete-data] [--delete-sg])" ;;
    esac
    shift
done

INSTANCE_ID="$(state_get instance_id)"
VOL_ID="$(state_get volume_id)"
SG_ID="$(state_get sg_id)"

# Fall back to tag lookup if state.json is missing/stale. Keep ALL matching
# ids (separate reservations print one per line) — terminate every one below.
if [ -z "$INSTANCE_ID" ]; then
    INSTANCE_ID=$(aws ec2 describe-instances \
        --filters "Name=tag:Name,Values=$TAG_PREFIX" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
        --query 'Reservations[].Instances[].InstanceId' --output text | tr -s '[:space:]' '\n' | sed '/^$/d')
fi
if [ -z "$VOL_ID" ]; then
    VOL_ID=$(aws ec2 describe-volumes \
        --filters "Name=tag:Name,Values=${TAG_PREFIX}-data" "Name=status,Values=available,in-use" \
        --query 'Volumes[0].VolumeId' --output text 2>/dev/null || true)
    [ "$VOL_ID" = "None" ] && VOL_ID=""
fi

# --- terminate instance(s) --------------------------------------------------------
# state_set __DELETE__ only runs after EVERY id is confirmed terminated or
# confirmed NotFound; any other API error dies with state.json untouched.
if [ -n "$INSTANCE_ID" ]; then
    N_IDS=$(printf '%s\n' "$INSTANCE_ID" | sed '/^$/d' | wc -l | tr -d ' ')
    if [ "$N_IDS" -gt 1 ]; then
        warn "found $N_IDS instances tagged '$TAG_PREFIX' — processing ALL of them"
    fi
    # shellcheck disable=SC2086  # word-splitting the id list is intentional
    for ID in $INSTANCE_ID; do
        ERR_FILE="$STATE_DIR/teardown_err.txt"
        if ! STATE=$(aws ec2 describe-instances --instance-ids "$ID" \
                --query 'Reservations[0].Instances[0].State.Name' --output text 2>"$ERR_FILE"); then
            if grep -q 'InvalidInstanceID.NotFound' "$ERR_FILE"; then
                STATE="gone"
            else
                cat "$ERR_FILE" >&2
                die "could not query instance $ID — refusing to assume it is terminated (state.json untouched); fix the error above and re-run"
            fi
        fi
        if [ "$STATE" = "terminated" ] || [ "$STATE" = "gone" ] || [ "$STATE" = "None" ]; then
            info "instance $ID already terminated/gone"
        else
            if [ "$ASSUME_YES" != "1" ]; then
                printf 'Terminate instance %s (%s)? Results on the data volume survive. [y/N] ' \
                    "$ID" "$STATE"
                read -r REPLY
                case "$REPLY" in
                    y|Y|yes|YES) ;;
                    *) die "aborted (nothing terminated)" ;;
                esac
            fi
            info "terminating $ID ..."
            aws ec2 terminate-instances --instance-ids "$ID" >/dev/null
            aws ec2 wait instance-terminated --instance-ids "$ID"
            info "instance terminated"
        fi
    done
    state_set instance_id __DELETE__ public_ip __DELETE__ az __DELETE__ market __DELETE__
else
    info "no instance to terminate"
fi

# --- data volume ------------------------------------------------------------------
if [ -n "$VOL_ID" ]; then
    if [ "$DELETE_DATA" = "1" ]; then
        if [ "$ASSUME_YES" != "1" ]; then
            printf 'DELETE data volume %s and ALL results/caches on it? This cannot be undone. [y/N] ' "$VOL_ID"
            read -r REPLY
            case "$REPLY" in
                y|Y|yes|YES) ;;
                *) die "aborted (volume kept)" ;;
            esac
        fi
        info "waiting for $VOL_ID to detach..."
        aws ec2 wait volume-available --volume-ids "$VOL_ID"
        aws ec2 delete-volume --volume-id "$VOL_ID"
        info "data volume $VOL_ID DELETED"
        state_set volume_id __DELETE__
    else
        # Idle cost = $0.08/GB-mo storage + ~$30/mo provisioned performance
        # (3000 IOPS over baseline @ $0.005 + 375 MB/s over baseline @ $0.04).
        IDLE_COST=$((DATA_VOLUME_GB * 8 / 100 + 30))
        warn "data volume $VOL_ID PRESERVED (results + caches intact; ~\$${IDLE_COST}/month while it idles: \$0.08/GB-mo for ${DATA_VOLUME_GB}GB gp3 + ~\$30/mo for the provisioned 6000 IOPS / 500 MB/s)"
        warn "resume later : ./01-provision.sh && ./02-bootstrap.sh && ./03-run.sh --resume"
        warn "delete later : ./06-teardown.sh --delete-data   (or: aws ec2 delete-volume --volume-id $VOL_ID)"
    fi
else
    info "no data volume found"
fi

# --- security group ------------------------------------------------------------------
if [ "$DELETE_SG" = "1" ]; then
    # Mirror the volume fallback: if state.json lacks sg_id, look it up by name.
    if [ -z "$SG_ID" ]; then
        SG_ID=$(aws ec2 describe-security-groups \
            --filters "Name=group-name,Values=${TAG_PREFIX}-ssh" \
            --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)
        [ "$SG_ID" = "None" ] && SG_ID=""
    fi
    if [ -z "$SG_ID" ]; then
        info "--delete-sg: no security group '${TAG_PREFIX}-ssh' found (nothing to delete)"
    else
        info "deleting security group $SG_ID"
        if aws ec2 delete-security-group --group-id "$SG_ID" 2>/dev/null; then
            state_set sg_id __DELETE__
            info "security group deleted"
        else
            warn "could not delete $SG_ID yet (instance ENI may still be releasing) — retry in a minute"
        fi
    fi
fi

info "teardown complete"
