#!/usr/bin/env bash
# 01-provision.sh — create-or-reuse (idempotent, keyed by Name tags):
#   * EC2 key pair               (KEY_NAME, imported from SSH_PUBKEY_PATH)
#   * security group             (${TAG_PREFIX}-ssh, SSH from caller IP only)
#   * persistent gp3 data volume (${TAG_PREFIX}-data, NEVER delete-on-termination)
#   * the benchmark instance     (stopped/running reuse, then guarded launch)
# Waits for running + SSH reachable, records everything in aws/state/state.json.
# Safe to re-run: if a tagged instance is stopped it starts that exact instance;
# if it is already running it reuses it. On-demand use is always cost-guarded.
# After a spot eviction, re-running this relaunches pinned to the volume's AZ
# and reattaches the (persisted) data volume. The replacement instance has a
# FRESH root FS: you must then run ./02-bootstrap.sh (remount volume, root-FS
# deps, .env) before ./03-run.sh --resume.
# shellcheck source-path=SCRIPTDIR

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

require_cmd aws "(brew install awscli)"
require_cmd python3

case "$USE_SPOT:$ALLOW_ON_DEMAND_FALLBACK" in
    0:0|0:1|1:0|1:1) ;;
    *) die "USE_SPOT and ALLOW_ON_DEMAND_FALLBACK must each be 0 or 1" ;;
esac
is_positive_number "$MAX_INSTANCE_HOURS" \
    || die "MAX_INSTANCE_HOURS must be a positive number (got '$MAX_INSTANCE_HOURS')"
if [ "$USE_SPOT" = "1" ]; then
    is_positive_number "$SPOT_MAX_HOURLY_USD" \
        || die "SPOT_MAX_HOURLY_USD must be a positive explicit ceiling"
fi

# --- 1. AMI (Ubuntu 24.04 LTS amd64, gp3) ------------------------------------
AMI_ID=$(aws ssm get-parameters \
    --names /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
    --query 'Parameters[0].Value' --output text)
[ -n "$AMI_ID" ] && [ "$AMI_ID" != "None" ] || die "could not resolve Ubuntu 24.04 AMI via SSM parameter"
info "AMI: $AMI_ID (Ubuntu 24.04 LTS amd64)"

# --- 2. key pair ---------------------------------------------------------------
if aws ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
    info "key pair '$KEY_NAME' already exists"
else
    [ -f "$SSH_PUBKEY_PATH" ] || die "missing $SSH_PUBKEY_PATH to import key pair '$KEY_NAME'"
    aws ec2 import-key-pair --key-name "$KEY_NAME" \
        --public-key-material "fileb://$SSH_PUBKEY_PATH" >/dev/null
    info "imported key pair '$KEY_NAME' from $SSH_PUBKEY_PATH"
fi

# --- 3. security group (SSH from caller IP only) --------------------------------
VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text)
[ -n "$VPC_ID" ] && [ "$VPC_ID" != "None" ] || die "no default VPC in $AWS_REGION"

SG_NAME="${TAG_PREFIX}-ssh"
SG_ID=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || printf 'None')
if [ -z "$SG_ID" ] || [ "$SG_ID" = "None" ]; then
    SG_ID=$(aws ec2 create-security-group --group-name "$SG_NAME" \
        --description "ContextBench: SSH from operator IP" --vpc-id "$VPC_ID" \
        --tag-specifications "ResourceType=security-group,Tags=[{Key=Name,Value=$SG_NAME}]" \
        --query 'GroupId' --output text)
    info "created security group $SG_ID ($SG_NAME)"
else
    info "reusing security group $SG_ID ($SG_NAME)"
fi

MYIP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
[ -n "$MYIP" ] || die "could not determine caller public IP"
# Prune stale /32 SSH rules first (IP churn across re-runs would otherwise
# accrete port-22 rules for addresses the operator no longer holds; this SG
# is dedicated to the harness, so exactly one rule is correct).
# shellcheck disable=SC2016  # backticks are a JMESPath literal, not shell
STALE_CIDRS=$(aws ec2 describe-security-groups --group-ids "$SG_ID" \
    --query 'SecurityGroups[0].IpPermissions[?FromPort==`22`].IpRanges[].CidrIp' \
    --output text 2>/dev/null | tr -s '[:space:]' '\n' | sed '/^$/d' || true)
for CIDR in $STALE_CIDRS; do
    if [ "$CIDR" != "${MYIP}/32" ]; then
        if aws ec2 revoke-security-group-ingress --group-id "$SG_ID" \
                --protocol tcp --port 22 --cidr "$CIDR" >/dev/null 2>&1; then
            info "revoked stale SSH ingress $CIDR"
        else
            warn "could not revoke stale SSH rule $CIDR — remove it manually"
        fi
    fi
done
if AUTH_OUT=$(aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
        --protocol tcp --port 22 --cidr "${MYIP}/32" 2>&1); then
    info "authorized SSH from ${MYIP}/32"
else
    if printf '%s' "$AUTH_OUT" | grep -q 'InvalidPermission.Duplicate'; then
        info "SSH ingress from ${MYIP}/32 already present"
    else
        die "authorize-security-group-ingress failed: $AUTH_OUT"
    fi
fi

# --- 4. existing data volume (determines AZ pinning) -----------------------------
VOL_NAME="${TAG_PREFIX}-data"
VOL_ID=$(aws ec2 describe-volumes \
    --filters "Name=tag:Name,Values=$VOL_NAME" "Name=status,Values=creating,available,in-use" \
    --query 'Volumes[0].VolumeId' --output text 2>/dev/null || printf 'None')
VOL_AZ=""
if [ -n "$VOL_ID" ] && [ "$VOL_ID" != "None" ]; then
    VOL_AZ=$(aws ec2 describe-volumes --volume-ids "$VOL_ID" \
        --query 'Volumes[0].AvailabilityZone' --output text)
    info "found existing data volume $VOL_ID in $VOL_AZ (will be preserved + reattached)"
else
    VOL_ID=""
fi

AZ_PIN="${AZ:-}"
if [ -n "$VOL_AZ" ]; then
    if [ -n "$AZ_PIN" ] && [ "$AZ_PIN" != "$VOL_AZ" ]; then
        die "config AZ=$AZ_PIN conflicts with existing data volume in $VOL_AZ. Unset AZ, or snapshot+recreate the volume in $AZ_PIN (see README: 'Volume stuck in the wrong AZ')."
    fi
    AZ_PIN="$VOL_AZ"
fi

# --- 5. reuse a running or stopped instance if present -----------------------------
EXISTING=$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=$TAG_PREFIX" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text | tr -s '[:space:]' '\n' | sed '/^$/d')
N_EXISTING=$(printf '%s\n' "$EXISTING" | sed '/^$/d' | wc -l | tr -d ' ')
if [ "$N_EXISTING" -gt 1 ]; then
    die "found $N_EXISTING live/stopped instances tagged '$TAG_PREFIX' ($(printf '%s' "$EXISTING" | tr '\n' ' ')) — resolve the duplicate resources before continuing"
fi
INSTANCE_ID=""
MARKET=""
if [ -n "$EXISTING" ]; then
    INSTANCE_ID="$EXISTING"
    EXISTING_TYPE=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
        --query 'Reservations[0].Instances[0].InstanceType' --output text)
    MARKET=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
        --query 'Reservations[0].Instances[0].InstanceLifecycle' --output text)
    [ "$MARKET" = "None" ] && MARKET="on-demand"
    if [ "$MARKET" = "on-demand" ]; then
        OD_RATE="$(assert_on_demand_cost_guard)"
        info "on-demand guard passed: \$$OD_RATE/hour <= \$$MAX_ON_DEMAND_HOURLY_USD/hour"
    fi
    if [ "$EXISTING_TYPE" != "$INSTANCE_TYPE" ]; then
        die "tagged instance $INSTANCE_ID is $EXISTING_TYPE but config requires $INSTANCE_TYPE; change config or deliberately replace the instance"
    fi
    EXISTING_STATE=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
        --query 'Reservations[0].Instances[0].State.Name' --output text)
    if [ "$EXISTING_STATE" = "stopping" ]; then
        info "instance $INSTANCE_ID is stopping; waiting before restart"
        aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID"
        EXISTING_STATE=stopped
    fi
    if [ "$EXISTING_STATE" = "stopped" ]; then
        info "starting preserved instance $INSTANCE_ID ($EXISTING_TYPE, $MARKET)"
        aws ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null
    else
        info "instance already $EXISTING_STATE: $INSTANCE_ID ($EXISTING_TYPE, $MARKET) — reusing"
    fi
    aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
fi

# --- 6. launch (spot, with explicit guarded on-demand fallback only) ----------------
launch_instance() {
    local err="$STATE_DIR/launch_err.txt"
    local launch_args=(
        --instance-type "$INSTANCE_TYPE"
        --image-id "$AMI_ID"
        --key-name "$KEY_NAME"
        --security-group-ids "$SG_ID"
        --count 1
        --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=${ROOT_VOLUME_GB},VolumeType=gp3,Iops=4000,Throughput=250,DeleteOnTermination=true}"
        --metadata-options "HttpTokens=required,HttpPutResponseHopLimit=2"
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${TAG_PREFIX}}]"
    )
    if [ -n "$AZ_PIN" ]; then
        launch_args+=(--placement "AvailabilityZone=$AZ_PIN")
        info "pinning launch to AZ $AZ_PIN"
    fi
    if [ "$USE_SPOT" = "1" ]; then
        info "requesting SPOT $INSTANCE_TYPE (maximum \$$SPOT_MAX_HOURLY_USD/hour; terminate on interruption)..."
        if INSTANCE_ID=$(aws ec2 run-instances "${launch_args[@]}" \
                --instance-market-options "MarketType=spot,SpotOptions={MaxPrice=$SPOT_MAX_HOURLY_USD,SpotInstanceType=one-time,InstanceInterruptionBehavior=terminate}" \
                --query 'Instances[0].InstanceId' --output text 2>"$err"); then
            MARKET="spot"
            return 0
        fi
        if grep -qE 'InsufficientInstanceCapacity|SpotMaxPriceTooLow|MaxSpotInstanceCountExceeded|Unsupported|capacity' "$err"; then
            warn "spot launch failed: $(head -1 "$err")"
            if [ "$ALLOW_ON_DEMAND_FALLBACK" != "1" ]; then
                die "spot capacity unavailable and ALLOW_ON_DEMAND_FALLBACK=0; no on-demand instance was launched"
            fi
            warn "spot unavailable; explicit on-demand fallback is enabled"
        else
            cat "$err" >&2
            die "spot launch failed with a non-capacity error (see above)"
        fi
    fi
    OD_RATE="$(assert_on_demand_cost_guard)"
    info "on-demand guard passed: \$$OD_RATE/hour <= \$$MAX_ON_DEMAND_HOURLY_USD/hour"
    info "requesting ON-DEMAND $INSTANCE_TYPE..."
    INSTANCE_ID=$(aws ec2 run-instances "${launch_args[@]}" \
        --query 'Instances[0].InstanceId' --output text)
    MARKET="on-demand"
}

if [ -z "$INSTANCE_ID" ]; then
    launch_instance
    info "launched $INSTANCE_ID ($MARKET); waiting for running state..."
    aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
fi

INSTANCE_AZ=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].Placement.AvailabilityZone' --output text)
PUBLIC_IP=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
[ -n "$PUBLIC_IP" ] && [ "$PUBLIC_IP" != "None" ] || die "instance $INSTANCE_ID has no public IP"
info "instance $INSTANCE_ID running in $INSTANCE_AZ at $PUBLIC_IP"

# --- 7. data volume: create if missing (in instance AZ), then attach -----------------
if [ -z "$VOL_ID" ]; then
    info "creating ${DATA_VOLUME_GB}GB gp3 data volume in $INSTANCE_AZ..."
    VOL_ID=$(aws ec2 create-volume --availability-zone "$INSTANCE_AZ" \
        --size "$DATA_VOLUME_GB" --volume-type gp3 --iops 6000 --throughput 500 \
        --tag-specifications "ResourceType=volume,Tags=[{Key=Name,Value=$VOL_NAME}]" \
        --query 'VolumeId' --output text)
    aws ec2 wait volume-available --volume-ids "$VOL_ID"
    info "created data volume $VOL_ID"
fi
# Volumes attached via attach-volume (not launch block-device-mapping) are
# never delete-on-termination: the volume survives spot eviction by default.
ATTACHED_TO=$(aws ec2 describe-volumes --volume-ids "$VOL_ID" \
    --query 'Volumes[0].Attachments[0].InstanceId' --output text)
if [ "$ATTACHED_TO" = "$INSTANCE_ID" ]; then
    info "data volume $VOL_ID already attached to $INSTANCE_ID"
elif [ "$ATTACHED_TO" = "None" ] || [ -z "$ATTACHED_TO" ]; then
    aws ec2 wait volume-available --volume-ids "$VOL_ID"
    aws ec2 attach-volume --volume-id "$VOL_ID" --instance-id "$INSTANCE_ID" \
        --device /dev/sdf >/dev/null
    aws ec2 wait volume-in-use --volume-ids "$VOL_ID"
    info "attached data volume $VOL_ID at /dev/sdf"
else
    die "data volume $VOL_ID is attached to another instance ($ATTACHED_TO) — tear that down first"
fi

# --- 8. wait for SSH -------------------------------------------------------------------
info "waiting for SSH on $PUBLIC_IP..."
TRIES=0
until ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$PUBLIC_IP" true 2>/dev/null; do
    TRIES=$((TRIES + 1))
    if [ "$TRIES" -ge 60 ]; then
        die "SSH not reachable after 5 minutes. Either: (a) your IP changed — re-run 01-provision.sh (SG $SG_ID allows ${MYIP}/32); or (b) $SSH_KEY_PATH is passphrase-protected and not loaded in ssh-agent (BatchMode=yes cannot prompt) — run: ssh-add $SSH_KEY_PATH"
    fi
    sleep 5
done
info "SSH is up"

# A local terminal disappearing must not leave a large on-demand box running.
# EC2's instance-initiated behavior is pinned to stop, then Ubuntu schedules a
# shutdown that is replaced on every successful provision/start.
aws ec2 modify-instance-attribute --instance-id "$INSTANCE_ID" \
    --instance-initiated-shutdown-behavior Value=stop
STOP_MINUTES=$(python3 -c 'import math,sys; print(max(1, math.ceil(float(sys.argv[1]) * 60)))' "$MAX_INSTANCE_HOURS")
# Do not use common.sh's remote() here: state.json still contains the prior
# public IP until the state write below, and a stopped instance receives a new
# address on start. Use the freshly queried address from this provision run.
# shellcheck disable=SC2029  # STOP_MINUTES is intentionally expanded locally
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$PUBLIC_IP" \
    "sudo shutdown -c >/dev/null 2>&1 || true; sudo shutdown -h +$STOP_MINUTES 'ContextBench automatic AWS cost ceiling reached' >/dev/null"
info "automatic stop scheduled in $STOP_MINUTES minutes"

# --- 9. record state ---------------------------------------------------------------------
state_set \
    region "$AWS_REGION" \
    instance_id "$INSTANCE_ID" \
    instance_type "$INSTANCE_TYPE" \
    market "$MARKET" \
    public_ip "$PUBLIC_IP" \
    az "$INSTANCE_AZ" \
    ami_id "$AMI_ID" \
    volume_id "$VOL_ID" \
    sg_id "$SG_ID" \
    key_name "$KEY_NAME"
state_set \
    max_instance_hours "$MAX_INSTANCE_HOURS" \
    max_on_demand_hourly_usd "$MAX_ON_DEMAND_HOURLY_USD" \
    automatic_stop_minutes "$STOP_MINUTES"

info "state written to $STATE_FILE"
printf '\n  instance : %s (%s, %s)\n  ip       : %s\n  data vol : %s (%s GB, persists across termination)\n\n' \
    "$INSTANCE_ID" "$INSTANCE_TYPE" "$MARKET" "$PUBLIC_IP" "$VOL_ID" "$DATA_VOLUME_GB"
info "next: ./02-bootstrap.sh"
