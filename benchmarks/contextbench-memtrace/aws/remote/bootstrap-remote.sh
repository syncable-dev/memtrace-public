#!/usr/bin/env bash
# bootstrap-remote.sh — runs ON THE BOX (Ubuntu 24.04) as the benchmark user.
# Invoked by 02-bootstrap.sh as:
#   bash ~/contextbench-adapter/aws/remote/bootstrap-remote.sh MEMTRACE_VERSION=x.y.z
# Idempotent: every step checks before it mutates. mkfs is guarded by blkid.

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

for kv in "$@"; do
    case "$kv" in *=*) export "${kv%%=*}"="${kv#*=}" ;; esac
done
: "${MEMTRACE_VERSION:=0.8.21}"
: "${MEMTRACE_INSTALL_MODE:=npm}"   # npm | source (source: build-memtrace-remote.sh installs the binary)
: "${DATA_VOL_ID:=}"    # EBS volume id (vol-...) passed by 02-bootstrap.sh

DATA_ROOT=/srv/contextbench
ADAPTER="$HOME/contextbench-adapter"
NVM_VERSION=v0.40.1
NODE_MAJOR=22
CONTEXTBENCH_REPO=https://github.com/EuniAI/ContextBench.git
HF_BASE=https://huggingface.co/cross-encoder/ms-marco-MiniLM-L12-v2/resolve/main
TOKENIZER_BYTES=711396
SHA_QINT8_AVX512_VNNI=148a402605f68037609b081d3f4e85154f49cd89f9e93990cfbe8120ae34410c
SHA_QUINT8_AVX2=0a9906ae940e137b83d512c8d0f5c6ce9980bf487b168d40b9522b4747c4c89b

log() { printf '\n[bootstrap] %s\n' "$*"; }

# --- 1. mount the persistent data volume at /srv/contextbench -------------------
log "data volume"
if mountpoint -q "$DATA_ROOT"; then
    echo "already mounted"
else
    sudo mkdir -p "$DATA_ROOT"
    TARGET=""
    # Preferred: resolve the device DETERMINISTICALLY from the attached EBS
    # volume id (udev publishes /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol...).
    # This can never pick an instance-store disk.
    if [ -n "$DATA_VOL_ID" ]; then
        BYID="/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${DATA_VOL_ID/-/}"
        for _ in 1 2 3 4 5; do
            [ -e "$BYID" ] && break
            sleep 2
        done
        if [ -e "$BYID" ]; then
            TARGET="$(readlink -f "$BYID")"
            echo "data volume $DATA_VOL_ID resolved via $BYID -> $TARGET"
        else
            echo "WARN: $BYID not found (non-nvme instance?) — falling back to EBS-only disk scan" >&2
        fi
    fi
    if [ -z "$TARGET" ]; then
        ROOT_SRC="$(findmnt -n -o SOURCE /)"
        ROOT_DISK="$(lsblk -no PKNAME "$ROOT_SRC" 2>/dev/null | head -1 || true)"
        for dev in $(lsblk -dno NAME,TYPE | awk '$2=="disk"{print $1}'); do
            [ "$dev" = "$ROOT_DISK" ] && continue
            # Only ever consider EBS disks: a blank NVMe INSTANCE-STORE disk
            # (model 'Amazon EC2 NVMe Instance Storage' on *d/i families) must
            # never be formatted as the "persistent" volume — everything on it
            # dies at exactly the eviction the volume exists to survive.
            MODEL="$(lsblk -dno MODEL "/dev/$dev" 2>/dev/null || true)"
            case "$MODEL" in
                *"Elastic Block Store"*) ;;
                *) continue ;;
            esac
            # skip disks that have partitions (never a blank EBS data volume)
            if [ "$(lsblk -no NAME "/dev/$dev" | wc -l)" -gt 1 ]; then
                continue
            fi
            LABEL="$(sudo blkid -o value -s LABEL "/dev/$dev" 2>/dev/null || true)"
            FSTYPE="$(sudo blkid -o value -s TYPE "/dev/$dev" 2>/dev/null || true)"
            if [ "$LABEL" = "cbdata" ]; then
                TARGET="/dev/$dev"
                break
            fi
            if [ -z "$FSTYPE" ] && [ -z "$TARGET" ]; then
                TARGET="/dev/$dev"
            fi
        done
    fi
    [ -n "$TARGET" ] || { echo "ERROR: no candidate data volume found (attach it via 01-provision.sh)" >&2; exit 1; }
    FSTYPE="$(sudo blkid -o value -s TYPE "$TARGET" 2>/dev/null || true)"
    if [ -z "$FSTYPE" ]; then
        echo "formatting blank volume $TARGET as ext4 (label cbdata) — FIRST TIME ONLY"
        sudo mkfs.ext4 -q -L cbdata "$TARGET"
    else
        LABEL="$(sudo blkid -o value -s LABEL "$TARGET" 2>/dev/null || true)"
        if [ "$LABEL" != "cbdata" ]; then
            echo "ERROR: $TARGET already has a filesystem ($FSTYPE, label '$LABEL') that is not ours — refusing to touch it" >&2
            exit 1
        fi
        echo "reusing existing cbdata filesystem on $TARGET (no mkfs)"
    fi
    if ! grep -q 'LABEL=cbdata' /etc/fstab; then
        echo 'LABEL=cbdata /srv/contextbench ext4 defaults,nofail 0 2' | sudo tee -a /etc/fstab >/dev/null
    fi
    sudo mount -a
    mountpoint -q "$DATA_ROOT" || { echo "ERROR: mount failed" >&2; exit 1; }
fi
sudo chown "$(id -u):$(id -g)" "$DATA_ROOT"
mkdir -p "$DATA_ROOT/results" "$DATA_ROOT/graph-cache" "$DATA_ROOT/rerank-model" "$DATA_ROOT/eval-repos"

# --- 2. apt packages --------------------------------------------------------------
# DPkg::Lock::Timeout: on a fresh boot cloud-init/unattended-upgrades often
# still holds the dpkg lock exactly when 02-bootstrap runs — wait, don't die.
log "apt packages"
APT="sudo apt-get -o DPkg::Lock::Timeout=300"
$APT update -y -qq
$APT install -y -qq git ca-certificates curl build-essential tmux rsync unzip \
    software-properties-common util-linux >/dev/null

# taskset(1)/flock(1) (util-linux) back run-remote.sh's core-pinning shim —
# the primary defense against the confirmed `memtrace mcp` thread-pool bug
# at CONCURRENCY>1. util-linux is normally already present on Ubuntu server
# images (it is effectively un-removable — dpkg-essential), so this install
# is a no-op in practice; the explicit check below turns a missing/broken
# AMI into a loud bootstrap failure instead of a silent run-remote.sh
# failure hours later.
if ! command -v taskset >/dev/null 2>&1 || ! command -v flock >/dev/null 2>&1; then
    echo "ERROR: taskset/flock still missing after installing util-linux — unexpected AMI, cannot support core-pinned concurrency" >&2
    exit 1
fi
echo "taskset/flock: OK ($(command -v taskset), $(command -v flock))"

if ! command -v python3.11 >/dev/null 2>&1; then
    log "python 3.11 via deadsnakes (Ubuntu 24.04 ships 3.12; upstream evaluator pins tree-sitter for <3.12)"
    sudo add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
    $APT update -y -qq
    $APT install -y -qq python3.11 python3.11-venv python3.11-dev >/dev/null
fi
python3.11 --version

if ! command -v docker >/dev/null 2>&1; then
    log "docker (for the later pass@1 agent lane)"
    $APT install -y -qq docker.io >/dev/null
    sudo usermod -aG docker "$USER"
fi

# --- 3. Node via nvm + memtrace pin -------------------------------------------------
log "node + memtrace@$MEMTRACE_VERSION"
export NVM_DIR="$HOME/.nvm"
if [ ! -s "$NVM_DIR/nvm.sh" ]; then
    curl -fsSL "https://raw.githubusercontent.com/nvm-sh/nvm/$NVM_VERSION/install.sh" | bash
fi
# nvm is not clean under set -eu; relax around it.
set +eu
# shellcheck disable=SC1091
. "$NVM_DIR/nvm.sh"
nvm install "$NODE_MAJOR" >/dev/null 2>&1
nvm use "$NODE_MAJOR" >/dev/null 2>&1
set -eu
command -v node >/dev/null || { echo "ERROR: node install failed" >&2; exit 1; }
echo "node $(node --version), npm $(npm --version)"

if [ "$MEMTRACE_INSTALL_MODE" = "source" ]; then
    # Source mode: the binary is built by aws/remote/build-memtrace-remote.sh
    # (invoked by 02-bootstrap.sh right after this script). Installing the npm
    # pin too would only create a second memtrace on PATH to get wrong.
    echo "MEMTRACE_INSTALL_MODE=source — skipping npm memtrace install (build-memtrace-remote.sh provides the binary)"
else
    # Exact version-token compare (a substring match would let e.g. an installed
    # 0.8.21 silently satisfy a pinned 0.8.2).
    HAVE_VER="$( (memtrace --version 2>/dev/null || true) | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 )"
    if [ "$HAVE_VER" = "$MEMTRACE_VERSION" ]; then
        echo "memtrace $MEMTRACE_VERSION already installed"
    else
        # Do NOT pass --omit=optional: the linux-x64 platform binary arrives
        # via optionalDependencies (postinstall self-heals if dropped).
        npm install -g "memtrace@$MEMTRACE_VERSION"
    fi
    # Running --version also self-repairs exec bits on the platform binaries.
    memtrace --version
fi

# --- 4. reranker model (hard requirement; runner refuses lexical fallback) ------------
log "reranker model -> $DATA_ROOT/rerank-model"
RERANK_DIR="$DATA_ROOT/rerank-model"
TOK="$RERANK_DIR/tokenizer.json"
MODEL="$RERANK_DIR/model_int8.onnx"
if grep -q avx512_vnni /proc/cpuinfo; then
    MODEL_URL="$HF_BASE/onnx/model_qint8_avx512_vnni.onnx"
    MODEL_SHA="$SHA_QINT8_AVX512_VNNI"
    echo "CPU has AVX-512 VNNI -> qint8_avx512_vnni quantization"
else
    MODEL_URL="$HF_BASE/onnx/model_quint8_avx2.onnx"
    MODEL_SHA="$SHA_QUINT8_AVX2"
    echo "no AVX-512 VNNI -> quint8_avx2 quantization"
fi
model_ok=0
if [ -f "$MODEL" ] && echo "$MODEL_SHA  $MODEL" | sha256sum -c --status; then
    model_ok=1
fi
if [ "$model_ok" -ne 1 ]; then
    curl -fL --retry 3 -o "$MODEL.tmp" "$MODEL_URL"
    echo "$MODEL_SHA  $MODEL.tmp" | sha256sum -c --status \
        || { echo "ERROR: rerank model sha256 mismatch" >&2; rm -f "$MODEL.tmp"; exit 1; }
    mv "$MODEL.tmp" "$MODEL"
fi
if [ ! -f "$TOK" ] || [ "$(wc -c <"$TOK")" != "$TOKENIZER_BYTES" ]; then
    curl -fL --retry 3 -o "$TOK.tmp" "$HF_BASE/tokenizer.json"
    [ "$(wc -c <"$TOK.tmp")" = "$TOKENIZER_BYTES" ] \
        || { echo "ERROR: tokenizer.json size mismatch" >&2; rm -f "$TOK.tmp"; exit 1; }
    mv "$TOK.tmp" "$TOK"
fi
ls -l "$RERANK_DIR"

# --- 5. ContextBench checkout + python3.11 venv ------------------------------------------
log "ContextBench evaluator"
CB_DIR="$DATA_ROOT/contextbench"
if [ ! -d "$CB_DIR/.git" ]; then
    git clone --quiet "$CONTEXTBENCH_REPO" "$CB_DIR"
fi
VENV="$DATA_ROOT/venv"
if [ ! -x "$VENV/bin/python" ]; then
    python3.11 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$CB_DIR/requirements.txt" pandas pyarrow
MINI_AGENT="$CB_DIR/agent-frameworks/mini-swe-agent/multi-poly-pro-verified/mini-swe-agent"
[ -f "$MINI_AGENT/pyproject.toml" ] \
    || { echo "ERROR: vendored mini-SWE-agent is missing: $MINI_AGENT" >&2; exit 1; }
"$VENV/bin/pip" install -q -e "$MINI_AGENT" "docker==7.2.0"
"$VENV/bin/python" -c 'import tree_sitter_languages, pyarrow, pandas, litellm, minisweagent, docker; print("evaluator + agent deps OK")'

# --- 6. pre-warm the embedding model ONCE (before any parallel fan-out) --------------------
# First index downloads jina-embeddings-v2-base-code (~640MB) into
# ~/.memtrace/fastembed_cache. 24 concurrent cold starts would contend on the
# hf-hub blob locks, so warm it serially here.
log "embedding model pre-warm"
CACHE_SNAP="$HOME/.memtrace/fastembed_cache/models--jinaai--jina-embeddings-v2-base-code/snapshots"
if [ "$MEMTRACE_INSTALL_MODE" = "source" ]; then
    # No binary exists yet in source mode; build-memtrace-remote.sh runs its
    # own smoke index in an isolated env, which performs this same warm-up
    # with the ACTUAL binary the benchmark will run.
    echo "source mode: pre-warm deferred to build-memtrace-remote.sh smoke index"
elif [ -d "$CACHE_SNAP" ] && [ -n "$(find "$CACHE_SNAP" -mindepth 1 -print -quit 2>/dev/null)" ]; then
    echo "embedding model cache already warm"
else
    WARM="$DATA_ROOT/warmup"
    rm -rf "$WARM"
    mkdir -p "$WARM"
    (
        cd "$WARM"
        git init -q
        git config user.email bench@localhost
        git config user.name bench
        printf 'def warmup():\n    return 1\n' > warmup.py
        git add -A
        git commit -qm warmup
    )
    # MEMTRACE_DEV=1: no license on the box; indexing + embed are auth-gated
    # (same rationale as build-memtrace-remote.sh smoke index / run-remote.sh).
    env CI=1 MEMTRACE_HEADLESS=1 MEMTRACE_DEV=1 MEMTRACE_MEMDB_MODE=embedded \
        MEMTRACE_MEMDB_LOOPBACK_PORT=0 \
        MEMTRACE_MEMDB_DATA_DIR="$WARM/memdb" MEMTRACE_DATA_DIR="$WARM/state" \
        MEMTRACE_NO_RTK_INIT=1 MEMTRACE_NO_RTK_PROMPT=1 MEMTRACE_TELEMETRY=off \
        timeout 2400 memtrace index "$WARM"
    if [ ! -d "$CACHE_SNAP" ] || [ -z "$(find "$CACHE_SNAP" -mindepth 1 -print -quit 2>/dev/null)" ]; then
        echo "ERROR: pre-warm ran but $CACHE_SNAP is missing/empty" >&2
        exit 1
    fi
    echo "embedding model cache warmed"
fi

# --- 7. summary ---------------------------------------------------------------------------
log "SUMMARY"
if [ "$MEMTRACE_INSTALL_MODE" = "source" ]; then
    echo "  memtrace : (source mode — built next by build-memtrace-remote.sh)"
else
    echo "  memtrace : $(memtrace --version 2>/dev/null | head -1)"
fi
echo "  node     : $(node --version)"
echo "  python   : $(python3.11 --version)  venv: $("$VENV/bin/python" --version)"
echo "  docker   : $(docker --version 2>/dev/null || echo 'installed (group takes effect next login)')"
echo "  rerank   : $(ls "$RERANK_DIR")"
echo "  adapter  : $ADAPTER"
echo "  data     : $(df -h "$DATA_ROOT" | tail -1)"
echo "bootstrap OK"
