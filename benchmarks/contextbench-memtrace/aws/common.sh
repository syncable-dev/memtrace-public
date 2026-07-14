#!/usr/bin/env bash
# common.sh — shared helpers for the ContextBench AWS harness.
# Sourced by the numbered scripts; not meant to be executed directly.
# shellcheck source-path=SCRIPTDIR

set -euo pipefail

# --- paths ---------------------------------------------------------------
AWS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER_DIR="$(cd "$AWS_DIR/.." && pwd)"
REPO_ROOT="$(cd "$ADAPTER_DIR/../.." && pwd)"
STATE_DIR="$AWS_DIR/state"
STATE_FILE="$STATE_DIR/state.json"
# shellcheck disable=SC2034  # used by the sourcing scripts (00, 02)
ENV_FILE="$REPO_ROOT/.env"
mkdir -p "$STATE_DIR"

# --- config --------------------------------------------------------------
# User config first (its values win), then the example fills remaining
# defaults via ': ${VAR:=default}'.
if [ -f "$AWS_DIR/config.env" ]; then
    # shellcheck disable=SC1091
    . "$AWS_DIR/config.env"
fi
# shellcheck source=config.env.example
# shellcheck disable=SC1091  # resolved relative to AWS_DIR at runtime
. "$AWS_DIR/config.env.example"

export AWS_DEFAULT_REGION="$AWS_REGION"
if [ -n "${AWS_PROFILE:-}" ]; then
    export AWS_PROFILE
else
    unset AWS_PROFILE 2>/dev/null || true
fi

# --- logging (stderr, so $(...) captures stay clean) ----------------------
info() { printf '\033[1;34m[info]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1 ${2:-}"
}

is_positive_number() {
    awk -v value="$1" 'BEGIN { exit !(value ~ /^[0-9]+([.][0-9]+)?$/ && value + 0 > 0) }'
}

on_demand_hourly_rate() { # instance type -> current Linux shared-tenancy USD/hour
    local instance_type="$1"
    aws pricing get-products \
        --service-code AmazonEC2 \
        --region us-east-1 \
        --filters \
            "Type=TERM_MATCH,Field=instanceType,Value=$instance_type" \
            "Type=TERM_MATCH,Field=location,Value=US East (N. Virginia)" \
            "Type=TERM_MATCH,Field=operatingSystem,Value=Linux" \
            "Type=TERM_MATCH,Field=tenancy,Value=Shared" \
            "Type=TERM_MATCH,Field=preInstalledSw,Value=NA" \
            "Type=TERM_MATCH,Field=capacitystatus,Value=Used" \
        --format-version aws_v1 --max-results 100 --output json | \
        python3 -c '
import json, sys
payload = json.load(sys.stdin)
rates = []
for raw in payload.get("PriceList", []):
    product = json.loads(raw)
    for term in product.get("terms", {}).get("OnDemand", {}).values():
        for dimension in term.get("priceDimensions", {}).values():
            unit = dimension.get("unit")
            usd = dimension.get("pricePerUnit", {}).get("USD")
            if unit == "Hrs" and usd is not None:
                rates.append(float(usd))
if len(rates) != 1:
    raise SystemExit(f"expected one hourly rate, found {len(rates)}")
print(f"{rates[0]:.6f}")
'
}

assert_on_demand_cost_guard() { # prints the verified hourly rate
    is_positive_number "$MAX_ON_DEMAND_HOURLY_USD" \
        || die "on-demand is possible but MAX_ON_DEMAND_HOURLY_USD=$MAX_ON_DEMAND_HOURLY_USD is not a positive explicit ceiling"
    local rate
    # shellcheck disable=SC2153  # INSTANCE_TYPE is loaded from config.env(.example)
    rate="$(on_demand_hourly_rate "$INSTANCE_TYPE")" \
        || die "could not resolve the current on-demand hourly price for $INSTANCE_TYPE"
    awk -v rate="$rate" -v cap="$MAX_ON_DEMAND_HOURLY_USD" \
        'BEGIN { exit !(rate <= cap) }' \
        || die "$INSTANCE_TYPE currently costs \$$rate/hour on-demand, above MAX_ON_DEMAND_HOURLY_USD=\$$MAX_ON_DEMAND_HOURLY_USD"
    printf '%s\n' "$rate"
}

# --- path normalization ----------------------------------------------------
expand_path() {
    # shellcheck disable=SC2088  # literal-tilde PATTERN match is the point
    case "$1" in
        "~/"*) printf '%s/%s\n' "$HOME" "${1#\~/}" ;;
        *) printf '%s\n' "$1" ;;
    esac
}
SSH_KEY_PATH="$(expand_path "$SSH_KEY_PATH")"
SSH_PUBKEY_PATH="$(expand_path "$SSH_PUBKEY_PATH")"

# --- state.json helpers ----------------------------------------------------
state_set() { # state_set key value [key value ...]; value __DELETE__ removes key
    python3 - "$STATE_FILE" "$@" <<'PY'
import json, os, sys, time
path, pairs = sys.argv[1], sys.argv[2:]
data = {}
if os.path.exists(path):
    with open(path) as fh:
        content = fh.read().strip()
    if content:
        data = json.loads(content)
for key, value in zip(pairs[0::2], pairs[1::2]):
    if value == "__DELETE__":
        data.pop(key, None)
    else:
        data[key] = value
data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)
PY
}

state_get() { # state_get key -> value (empty if missing)
    [ -f "$STATE_FILE" ] || { printf '\n'; return 0; }
    python3 - "$STATE_FILE" "$1" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception:
    print("")
PY
}

# --- ssh / rsync -----------------------------------------------------------
SSH_OPTS=(
    -i "$SSH_KEY_PATH"
    -o StrictHostKeyChecking=accept-new
    -o ConnectTimeout=10
    -o ServerAliveInterval=30
    -o BatchMode=yes
)

instance_ip() {
    local ip
    ip="$(state_get public_ip)"
    [ -n "$ip" ] || die "no public_ip in $STATE_FILE — run 01-provision.sh first"
    printf '%s\n' "$ip"
}

# remote "single command string"
remote() {
    local ip
    ip="$(instance_ip)"
    # shellcheck disable=SC2029  # client-side expansion is intentional
    ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$ip" "$@"
}

ssh_cmd_string() { # for rsync -e
    local out="ssh" opt
    for opt in "${SSH_OPTS[@]}"; do
        out+=" $(printf '%q' "$opt")"
    done
    printf '%s\n' "$out"
}

# Push the adapter directory (runner.py, parallel_driver.py, report.py,
# aws/remote/*, ...) to the box. Excludes secrets and local-only state.
# --delete keeps the box in sync, but excluded paths (.env) are protected.
rsync_adapter() {
    local ip
    ip="$(instance_ip)"
    rsync -az --delete \
        --exclude '.env' \
        --exclude '__pycache__' \
        --exclude '.DS_Store' \
        --exclude 'aws/state' \
        --exclude 'aws/config.env' \
        --exclude 'work/' \
        -e "$(ssh_cmd_string)" \
        "$ADAPTER_DIR/" "$REMOTE_USER@$ip:$REMOTE_ADAPTER_DIR/"
}
