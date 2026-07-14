#!/usr/bin/env bash
# 00-preflight.sh — local checks before spending money on AWS.
# Verifies: aws CLI + credentials, vCPU quotas (spot + on-demand),
# OPENAI_API_KEY in memtrace-public/.env, SSH keys, local tooling.
# Exits nonzero if any required check fails; prints a summary table.
# shellcheck source-path=SCRIPTDIR

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

ROWS=()
FAILED=0

add_row() { # name status detail
    ROWS+=("$1|$2|$3")
    if [ "$2" = "FAIL" ]; then
        FAILED=1
    fi
}

# --- local tooling ---------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
    add_row "python3 (local)" PASS "$(python3 --version 2>&1)"
else
    add_row "python3 (local)" FAIL "install python3 (needed for state.json helpers)"
fi

if command -v rsync >/dev/null 2>&1; then
    add_row "rsync (local)" PASS "found"
else
    add_row "rsync (local)" FAIL "install rsync"
fi

CREDS_OK=0
if command -v aws >/dev/null 2>&1; then
    add_row "aws CLI" PASS "$(aws --version 2>&1 | head -1)"
    if IDENTITY=$(aws sts get-caller-identity --query 'Arn' --output text 2>/dev/null); then
        add_row "AWS credentials" PASS "$IDENTITY (region $AWS_REGION)"
        CREDS_OK=1
    else
        add_row "AWS credentials" FAIL "aws sts get-caller-identity failed — run 'aws configure' or 'aws configure sso'"
    fi
else
    add_row "aws CLI" FAIL "not installed — brew install awscli"
    add_row "AWS credentials" FAIL "skipped (no aws CLI)"
fi

# --- vCPU quotas -------------------------------------------------------------
REQUIRED_VCPUS=96
if [ "$CREDS_OK" = "1" ]; then
    if VCPUS=$(aws ec2 describe-instance-types --instance-types "$INSTANCE_TYPE" \
            --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus' --output text 2>/dev/null) \
            && [ "$VCPUS" != "None" ] && [ -n "$VCPUS" ]; then
        REQUIRED_VCPUS="$VCPUS"
    fi
    add_row "instance type" PASS "$INSTANCE_TYPE = $REQUIRED_VCPUS vCPUs"

    check_quota() { # quota_code label required_or_optional
        local code="$1" label="$2" mode="$3" value remediation
        remediation="aws service-quotas request-service-quota-increase --service-code ec2 --quota-code $code --desired-value 128"
        if ! value=$(aws service-quotas get-service-quota --service-code ec2 \
                --quota-code "$code" --query 'Quota.Value' --output text 2>/dev/null); then
            add_row "$label" WARN "could not read quota (servicequotas:GetServiceQuota permission?) — verify manually: $remediation"
            return 0
        fi
        if awk -v q="$value" -v n="$REQUIRED_VCPUS" 'BEGIN{exit !(q+0 >= n+0)}'; then
            add_row "$label" PASS "quota=$value >= $REQUIRED_VCPUS"
        elif [ "$mode" = "required" ]; then
            add_row "$label" FAIL "quota=$value < $REQUIRED_VCPUS. Request an increase NOW (can take hours-days): $remediation"
        else
            add_row "$label" WARN "quota=$value < $REQUIRED_VCPUS. Spot disabled or fallback-only; to enable: $remediation"
        fi
    }

    # Only require quota for a market the configured launch path can use.
    if [ "$USE_SPOT" = "1" ]; then
        check_quota "L-34B43A08" "spot vCPU quota (L-34B43A08)" required
    else
        check_quota "L-34B43A08" "spot vCPU quota (L-34B43A08)" optional
    fi
    if [ "$USE_SPOT" = "0" ] || [ "$ALLOW_ON_DEMAND_FALLBACK" = "1" ]; then
        check_quota "L-1216C47A" "on-demand vCPU quota (L-1216C47A)" required
    else
        check_quota "L-1216C47A" "on-demand vCPU quota (L-1216C47A)" optional
    fi
else
    add_row "vCPU quotas" FAIL "skipped (no AWS credentials)"
fi

# --- spending guard -----------------------------------------------------------
if ! is_positive_number "$MAX_INSTANCE_HOURS"; then
    add_row "automatic stop ceiling" FAIL "MAX_INSTANCE_HOURS must be a positive number (got '$MAX_INSTANCE_HOURS')"
else
    add_row "automatic stop ceiling" PASS "instance stops within $MAX_INSTANCE_HOURS hour(s) of provision/start"
fi
if [ "$USE_SPOT" = "1" ]; then
    if is_positive_number "$SPOT_MAX_HOURLY_USD"; then
        add_row "spot hourly ceiling" PASS "maximum bid \$$SPOT_MAX_HOURLY_USD/hour"
    else
        add_row "spot hourly ceiling" FAIL "set SPOT_MAX_HOURLY_USD to a positive explicit ceiling"
    fi
fi
if [ "$USE_SPOT" = "0" ] || [ "$ALLOW_ON_DEMAND_FALLBACK" = "1" ]; then
    if [ "$CREDS_OK" = "1" ] && is_positive_number "$MAX_ON_DEMAND_HOURLY_USD"; then
        if RATE="$(on_demand_hourly_rate "$INSTANCE_TYPE" 2>/dev/null)"; then
            if awk -v rate="$RATE" -v cap="$MAX_ON_DEMAND_HOURLY_USD" 'BEGIN { exit !(rate <= cap) }'; then
                add_row "on-demand hourly ceiling" PASS "current \$$RATE/hour <= cap \$$MAX_ON_DEMAND_HOURLY_USD/hour"
            else
                add_row "on-demand hourly ceiling" FAIL "current \$$RATE/hour exceeds cap \$$MAX_ON_DEMAND_HOURLY_USD/hour"
            fi
        else
            add_row "on-demand hourly ceiling" FAIL "could not resolve current AWS price for $INSTANCE_TYPE"
        fi
    else
        add_row "on-demand hourly ceiling" FAIL "set MAX_ON_DEMAND_HOURLY_USD to a positive explicit ceiling"
    fi
fi

# --- Memtrace install mode -----------------------------------------------------
case "$MEMTRACE_INSTALL_MODE" in
    npm)
        add_row "memtrace install mode" PASS "npm pin $MEMTRACE_VERSION"
        ;;
    source)
        if [ ! -d "$MEMTRACE_SOURCE_DIR" ]; then
            add_row "memtrace install mode" FAIL "source mode: MEMTRACE_SOURCE_DIR=$MEMTRACE_SOURCE_DIR does not exist"
        elif ! git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" rev-parse HEAD >/dev/null 2>&1; then
            add_row "memtrace install mode" FAIL "source mode: $MEMTRACE_SOURCE_DIR is not a git repo (provenance capture needs HEAD)"
        else
            # --no-optional-locks: never write .git/index in the actively-edited repo
            SRC_SHA="$(git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" rev-parse --short=12 HEAD)"
            DIRTY_N="$(git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
            if [ "$DIRTY_N" -gt 0 ]; then
                if [ "$ALLOW_DIRTY_SOURCE" = "1" ]; then
                    add_row "memtrace install mode" WARN "diagnostic dirty-source override: HEAD $SRC_SHA + $DIRTY_N dirty file(s)"
                else
                    add_row "memtrace install mode" FAIL "source tree has $DIRTY_N dirty file(s); use a clean worktree (or explicitly set ALLOW_DIRTY_SOURCE=1 for a non-publishable diagnostic run)"
                fi
            else
                add_row "memtrace install mode" PASS "source mode: clean tree at $SRC_SHA"
            fi
        fi
        ;;
    *)
        add_row "memtrace install mode" FAIL "MEMTRACE_INSTALL_MODE must be npm|source (got '$MEMTRACE_INSTALL_MODE')"
        ;;
esac

# --- OPENAI_API_KEY ----------------------------------------------------------
# Never print the value; only check presence + non-empty.
if [ -f "$ENV_FILE" ] && grep -q '^OPENAI_API_KEY=..*' "$ENV_FILE"; then
    add_row "OPENAI_API_KEY in .env" PASS "present in $ENV_FILE (value not shown)"
else
    add_row "OPENAI_API_KEY in .env" FAIL "add OPENAI_API_KEY=... to $ENV_FILE (gitignored)"
fi

# --- SSH keys ---------------------------------------------------------------
# All ssh/scp/rsync use BatchMode=yes (no prompts), so a passphrase-protected
# key that is not in an ssh-agent fails AFTER the paid instance is launched,
# with a misleading "SSH not reachable" symptom. Verify usability here.
if [ -f "$SSH_KEY_PATH" ]; then
    if ssh-keygen -y -P '' -f "$SSH_KEY_PATH" >/dev/null 2>&1; then
        add_row "SSH private key" PASS "$SSH_KEY_PATH (no passphrase)"
    elif [ -f "$SSH_PUBKEY_PATH" ] \
            && KEY_FINGERPRINT="$(ssh-keygen -lf "$SSH_PUBKEY_PATH" -E sha256 2>/dev/null | awk 'NR == 1 { print $2 }')" \
            && [ -n "$KEY_FINGERPRINT" ] \
            && ssh-add -l -E sha256 2>/dev/null | awk '{ print $2 }' | grep -Fqx "$KEY_FINGERPRINT"; then
        add_row "SSH private key" PASS "$SSH_KEY_PATH (passphrase-protected; exact key is loaded in ssh-agent)"
    else
        add_row "SSH private key" FAIL "$SSH_KEY_PATH is passphrase-protected and this exact key is not loaded in ssh-agent — run: ssh-add $SSH_KEY_PATH (BatchMode=yes cannot prompt for the passphrase)"
    fi
else
    add_row "SSH private key" FAIL "missing $SSH_KEY_PATH — set SSH_KEY_PATH in config.env or ssh-keygen -t ed25519"
fi
if [ -f "$SSH_PUBKEY_PATH" ]; then
    add_row "SSH public key" PASS "$SSH_PUBKEY_PATH"
else
    add_row "SSH public key" FAIL "missing $SSH_PUBKEY_PATH (needed to import the EC2 key pair)"
fi

# --- summary ------------------------------------------------------------------
printf '\n%-38s %-5s %s\n' "CHECK" "STAT" "DETAIL"
printf '%-38s %-5s %s\n' "--------------------------------------" "-----" "------"
for row in "${ROWS[@]}"; do
    IFS='|' read -r name status detail <<<"$row"
    printf '%-38s %-5s %s\n' "$name" "$status" "$detail"
done
printf '\n'

if [ "$FAILED" = "1" ]; then
    die "preflight FAILED — fix the items above, then re-run 00-preflight.sh"
fi
info "preflight passed — next: ./01-provision.sh"
