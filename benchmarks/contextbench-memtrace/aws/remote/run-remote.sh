#!/usr/bin/env bash
# run-remote.sh — runs ON THE BOX inside tmux. Launched by 03-run.sh with env:
#   RUN_ID DATASET BENCHMARK_LANE CONCURRENCY LINE_BUDGET SELECTOR_MODEL
#   AGENT_MODEL AGENT_HISTORY_DAYS SELECTOR_MODE
#   POST_SELECTOR_POLICY CACHE_NAMESPACE RUN_TIMEOUT WATCHDOG_MINUTES
#   CB_SEARCH_LIMIT CB_PACK_POLICY CB_QUERY_STRATEGY
#   MEMTRACE_INSTALL_MODE
# Builds the manifest from the chosen dataset parquet, starts the spot-
# interruption watcher + the per-instance watchdog, then runs
# parallel_driver.py. Always passes --resume (when the driver supports it),
# so re-running the same RUN_ID only executes missing instances.

set -euo pipefail

: "${RUN_ID:?RUN_ID required}"
: "${DATASET:=verified}"
: "${BENCHMARK_LANE:=retrieval}"
: "${MANIFEST_LIMIT:=0}"
: "${MANIFEST_SOURCE_RUN_ID:=}"
: "${CONCURRENCY:=auto}"
# Standalone fallback only. Scored AWS runs currently pass LINE_BUDGET=200 from
# the gitignored config.env; run_meta.json records the effective value.
: "${LINE_BUDGET:=80}"
: "${SELECTOR_MODEL:=gpt-5}"
: "${AGENT_MODEL:=openai/gpt-5}"
# History is optional for ContextBench: the task is solved against the exact
# base-commit checkout. Keep the generic agent fallback bounded; the Codex
# fleet launcher passes 0 explicitly so its primary treatment measures current
# code memory without paying for or depending on temporal replay.
: "${AGENT_HISTORY_DAYS:=30}"
: "${SELECTOR_MODE:=default}"
: "${POST_SELECTOR_POLICY:=off}"
: "${CACHE_NAMESPACE:=contextbench-v1}"
: "${RUN_TIMEOUT:=7200}"
: "${WATCHDOG_MINUTES:=90}"
: "${MEMTRACE_INSTALL_MODE:=npm}"
# Locked retrieval policy (2026-07-11 gate-clearing config, see
# aws/state/artifacts/LOCKED_POLICY.md): runner.py reads these two directly
# via os.environ (no argv plumbing), so they must be EXPORTED (below) to
# reach the parallel_driver.py -> runner.py subprocess chain.
: "${CB_SEARCH_LIMIT:=100}"
: "${CB_PACK_POLICY:=v5}"
: "${CB_QUERY_STRATEGY:=v3}"
# ORT's embed session defaults to 8 intra-op threads per process, unaware of
# how many sibling `memtrace mcp` processes are running concurrently — an
# independent leak from the rayon thread-cap bug below. Capped here
# (exported further down) so N concurrent instances don't ALSO oversubscribe
# on the embed lane even once rayon/taskset are under control.
: "${MEMTRACE_EMBED_INTRA_OP_THREADS:=4}"
# Layer A: OS-level core-pinning shim (see install_taskset_shim below).
# 1 = install/refresh the shim and hard-partition cores per concurrency slot
# with taskset(1); this is the primary defense against the confirmed
# `memtrace mcp` thread-pool bug (MEMTRACE_MAX_THREADS is read but the
# process's own thread count still tracks ~nproc regardless of intended
# per-slot share). 0 = run the raw binary unpinned — NOT recommended at
# CONCURRENCY>2 (see the thread-cap comment further down); only for
# debugging on a box without taskset/flock.
: "${MEMTRACE_PIN_ENABLE:=1}"

case "$BENCHMARK_LANE" in
    retrieval|agent|codex) ;;
    *) echo "ERROR: BENCHMARK_LANE must be retrieval, agent, or codex (got $BENCHMARK_LANE)" >&2; exit 2 ;;
esac
if { [ "$BENCHMARK_LANE" = "agent" ] || [ "$BENCHMARK_LANE" = "codex" ]; } && { [ "$SELECTOR_MODE" != "default" ] || [ "$POST_SELECTOR_POLICY" != "off" ]; }; then
    echo "ERROR: coding-agent lanes require SELECTOR_MODE=default and POST_SELECTOR_POLICY=off; their final context comes from the agent trajectory" >&2
    exit 2
fi
DATA_ROOT=/srv/contextbench
ADAPTER="$HOME/contextbench-adapter"
RESULTS="$DATA_ROOT/results/$RUN_ID"
mkdir -p "$RESULTS"
# A resumed run inherits the prior shell's terminal marker. Keep the legacy
# marker accurate for status/evaluation consumers; the watchdog itself uses a
# unique session-scoped marker below and never trusts this shared path.
rm -f "$RESULTS/driver_exit"

# Everything to driver.log AND the tmux pane.
exec > >(tee -a "$RESULTS/driver.log") 2>&1
echo "=== run-remote $(date -u +%Y-%m-%dT%H:%M:%SZ) run=$RUN_ID dataset=$DATASET ==="

# --- environment ------------------------------------------------------------------
export NVM_DIR="$HOME/.nvm"
set +eu
# shellcheck disable=SC1091
. "$NVM_DIR/nvm.sh"
nvm use 22 >/dev/null 2>&1
set -eu

# Binary selection — AFTER the nvm PATH mutation so the choice sticks.
# source mode: the cargo-built binary dir goes FIRST on PATH (children of the
# driver inherit it, so every runner.py instance runs the built binary even
# if an npm memtrace also exists from an earlier npm-mode session).
MEMTRACE_BIN_DIR="/srv/contextbench/memtrace-bin"
SOURCE_MANIFEST=""

# --- Layer A: OS-level core-pinning shim ---------------------------------------------
# Works around the confirmed, unfixed `memtrace mcp` thread-pool bug: the
# binary reads MEMTRACE_MAX_THREADS but its OWN process still ends up
# running roughly nproc threads regardless (crates/memtrace-mcp/src/
# resources.rs configure_rayon vs. main.rs's later, sometimes-too-late
# RAYON_NUM_THREADS mirror — see main.rs for the full race writeup). N
# concurrent `memtrace mcp` processes on one box therefore create ~N*nproc
# runnable threads: 5 concurrent tasks drove load average 400-560 on a
# 96-192 core box on 2026-07-11/12. taskset(1) makes this moot at the OS
# level: the scheduler will not run a pinned process's threads outside its
# assigned core range NO MATTER how many threads the process spawns.
#
# Mechanism: replace $MEMTRACE_BIN_DIR/memtrace (the cargo-built binary,
# renamed to memtrace.real alongside it) with a small shim that flock(1)s
# across MEMTRACE_PIN_SLOTS pre-created per-slot lockfiles to claim a free,
# non-overlapping core range, then `exec taskset -c <lo>-<hi> memtrace.real
# "$@"`. The flock'd fd survives exec (fds are not close-on-exec by default)
# and is released automatically on ANY exit — including a SIGKILL from this
# script's own watchdog/orphan-reaper — so no explicit unlock/cleanup path
# is needed. MEMTRACE_PIN_SLOTS / MEMTRACE_PIN_CORES_PER_SLOT are computed
# and exported later, next to the (now redundant-but-kept-as-defense-in-
# depth) MEMTRACE_MAX_THREADS block, since both need CONC/nproc.
install_taskset_shim() {
    local shim="$MEMTRACE_BIN_DIR/memtrace" real="$MEMTRACE_BIN_DIR/memtrace.real"
    # build-memtrace-remote.sh (a REBUILD, e.g. after ALLOW_STALE_SOURCE or a
    # fresh source push) installs every target/release executable straight
    # to "$BIN_DIR/$name" — i.e. it always writes to $shim directly, with NO
    # knowledge of the shim/real split, and will happily clobber an
    # installed shim script back to a raw ELF binary. So "memtrace.real
    # already exists" is NOT a safe signal that it is still current —
    # inspect what is ACTUALLY at $shim right now, every time:
    if [ -f "$shim" ] && head -c4 "$shim" 2>/dev/null | grep -q $'\x7fELF'; then
        # $shim is a real ELF binary: either the very first install, or a
        # rebuild clobbered it back to raw. Either way it is the FRESHEST
        # binary — promote it, overwriting any older/stale memtrace.real.
        mv -f "$shim" "$real"
    elif [ -f "$shim" ] && grep -q 'MEMTRACE-TASKSET-SHIM' "$shim" 2>/dev/null; then
        : # already our shim, no rebuild has clobbered it since — $real is current
    elif [ ! -f "$shim" ] && [ -f "$real" ]; then
        : # shim path empty but $real present (unexpected but recoverable) — rewrite below
    else
        echo "ERROR: cannot install taskset shim: $shim is neither a recognizable ELF binary nor an existing MEMTRACE-TASKSET-SHIM, and $real does not exist as a fallback. Restore the built binary via 02-bootstrap.sh and retry."
        exit 1
    fi
    [ -f "$real" ] || { echo "ERROR: cannot install taskset shim: $real still missing after detection"; exit 1; }
    chmod +x "$real"
    # Written fresh every run (idempotent + picks up shim fixes without a
    # re-bootstrap): a tmp-file + mv keeps any process that already exec'd
    # the OLD shim inode unaffected (Linux keeps the old inode alive for
    # existing open/exec'd references).
    cat > "$shim.new" <<'SHIM_EOF'
#!/usr/bin/env bash
# MEMTRACE-TASKSET-SHIM v1 — installed by run-remote.sh install_taskset_shim().
# Do not edit on the box; it is overwritten every run. See run-remote.sh for
# the full rationale.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_BIN="$SELF_DIR/memtrace.real"

# Disabled, or the caller never set slot config (e.g. npm-mode leftovers,
# or a human invoking the shim directly outside run-remote.sh) — run
# unpinned rather than fail closed for a benign case.
if [ "${MEMTRACE_PIN_ENABLE:-0}" != "1" ] || [ -z "${MEMTRACE_PIN_SLOTS:-}" ] || [ -z "${MEMTRACE_PIN_CORES_PER_SLOT:-}" ]; then
    exec "$REAL_BIN" "$@"
fi

SLOTS="$MEMTRACE_PIN_SLOTS"
CORES_PER_SLOT="$MEMTRACE_PIN_CORES_PER_SLOT"
LOCKDIR="${MEMTRACE_PIN_LOCKDIR:-$SELF_DIR/pin-slots}"
mkdir -p "$LOCKDIR" 2>/dev/null || true
TOTAL_CORES="$(nproc)"

i=0
while [ "$i" -lt "$SLOTS" ]; do
    lockfile="$LOCKDIR/slot-$i.lock"
    if exec {LOCKFD}>"$lockfile" 2>/dev/null && flock -n "$LOCKFD"; then
        lo=$((i * CORES_PER_SLOT))
        hi=$((lo + CORES_PER_SLOT - 1))
        # Last slot absorbs any remainder cores (nproc not evenly divisible
        # by SLOTS) so no cores sit permanently idle.
        if [ "$i" -eq $((SLOTS - 1)) ] && [ $((TOTAL_CORES - 1)) -gt "$hi" ]; then
            hi=$((TOTAL_CORES - 1))
        fi
        exec taskset -c "${lo}-${hi}" "$REAL_BIN" "$@"
    fi
    exec {LOCKFD}>&- 2>/dev/null || true
    i=$((i + 1))
done

# All SLOTS busy — should not happen if the caller's concurrency <= SLOTS.
# Fail LOUDLY rather than silently run unpinned, which is exactly the
# oversubscription this shim exists to prevent.
echo "memtrace-shim: FATAL no free pin slot (0..$((SLOTS - 1)) all locked) — refusing to run unpinned" >&2
exit 97
SHIM_EOF
    chmod +x "$shim.new"
    mv "$shim.new" "$shim"
}

if [ "$MEMTRACE_INSTALL_MODE" = "source" ]; then
    [ -f "$MEMTRACE_BIN_DIR/memtrace" ] || [ -f "$MEMTRACE_BIN_DIR/memtrace.real" ] \
        || { echo "ERROR: MEMTRACE_INSTALL_MODE=source but $MEMTRACE_BIN_DIR/memtrace missing (run 02-bootstrap.sh)"; exit 1; }
    if [ "$MEMTRACE_PIN_ENABLE" = "1" ]; then
        if ! command -v taskset >/dev/null 2>&1 || ! command -v flock >/dev/null 2>&1; then
            echo "ERROR: MEMTRACE_PIN_ENABLE=1 but taskset/flock not found on PATH (util-linux missing? run 02-bootstrap.sh, which now installs util-linux). Set MEMTRACE_PIN_ENABLE=0 to run WITHOUT core pinning — NOT recommended at CONCURRENCY>2, see the thread-cap comment below."
            exit 1
        fi
        install_taskset_shim
        echo "taskset shim: installed at $MEMTRACE_BIN_DIR/memtrace (real binary: $MEMTRACE_BIN_DIR/memtrace.real)"
    else
        echo "WARNING: MEMTRACE_PIN_ENABLE=0 — running the RAW binary unpinned. At CONCURRENCY>2 this WILL thrash (N concurrent memtrace processes each try to use ~nproc threads); only use this for single-instance debugging."
    fi
    [ -x "$MEMTRACE_BIN_DIR/memtrace" ] \
        || { echo "ERROR: $MEMTRACE_BIN_DIR/memtrace missing/not executable after shim install"; exit 1; }
    export PATH="$MEMTRACE_BIN_DIR:$PATH"
    RESOLVED="$(command -v memtrace)"
    [ "$RESOLVED" = "$MEMTRACE_BIN_DIR/memtrace" ] \
        || { echo "ERROR: source mode resolved memtrace to $RESOLVED, expected $MEMTRACE_BIN_DIR/memtrace"; exit 1; }
    SOURCE_MANIFEST="$MEMTRACE_BIN_DIR/source-manifest.json"
    [ -s "$SOURCE_MANIFEST" ] || { echo "ERROR: $SOURCE_MANIFEST missing (rerun 02-bootstrap.sh)"; exit 1; }
fi
command -v memtrace >/dev/null || { echo "ERROR: memtrace not on PATH (run 02-bootstrap.sh)"; exit 1; }
MEMTRACE_VERSION_RUNTIME="$(memtrace --version 2>&1 | sed $'s/\\033\\[[0-9;]*m//g' | grep -m1 -E '^memtrace [0-9]+[.][0-9]+[.][0-9]+')"
[ -n "$MEMTRACE_VERSION_RUNTIME" ] || { echo "ERROR: could not parse memtrace semantic version"; exit 1; }
echo "memtrace: $(command -v memtrace) ($MEMTRACE_VERSION_RUNTIME, install_mode=$MEMTRACE_INSTALL_MODE)"
export MEMTRACE_TELEMETRY=off
# The box has no memtrace license (no credentials file; .env only carries
# OPENAI_API_KEY) and indexing + the embed lane are auth-gated. runner.py
# passes the ambient environment through to every memtrace child, so the
# dev bypass here covers all instances.
export MEMTRACE_DEV=1
# Cortex is universal-on since 0.8.3: "off" gates the sidecar spawn, and the
# store-dir override keeps any client socket lookups away from a real
# ~/.memtrace/cortex-store — a benchmark box wants neither the overhead nor
# the ingest noise.
export MEMTRACE_CORTEX=off
export MEMCORTEX_STORE_DIR="$DATA_ROOT/cortex-store"

VENV_PY="$DATA_ROOT/venv/bin/python"
[ -x "$VENV_PY" ] || { echo "ERROR: venv missing (run 02-bootstrap.sh)"; exit 1; }
if [ "$BENCHMARK_LANE" = "agent" ]; then
    "$VENV_PY" -c 'import docker; client = docker.from_env(); assert client.ping()' \
        || { echo "ERROR: agent lane requires the Python Docker SDK and daemon access" >&2; exit 2; }
fi

# OPENAI_API_KEY: exported directly here rather than relying on runner.py's
# own `load_env_file(Path(".env"))` (relative to its cwd, i.e. --chdir
# "$ADAPTER" — fragile: any future change to the driver's cwd/spawn path
# silently breaks it with no signal until every instance fails selection).
# grep+export straight from the file, matching the pattern already proven
# to work in today's shard-orchestrator launches (aws/shard-orchestrator.py)
# without ever echoing any portion of the credential into the run log.
OPENAI_API_KEY="$(grep '^OPENAI_API_KEY=' "$ADAPTER/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
[ -n "$OPENAI_API_KEY" ] || { echo "ERROR: OPENAI_API_KEY missing/empty in $ADAPTER/.env (run 02-bootstrap.sh)"; exit 1; }
export OPENAI_API_KEY
echo "OPENAI_API_KEY: loaded and exported (value not logged)"

# --- dataset ----------------------------------------------------------------------
case "$DATASET" in
    verified) PARQUET=contextbench_verified.parquet ;;
    full)     PARQUET=full.parquet ;;
    train)    PARQUET=contextbench_verified_train.parquet ;;
    test)     PARQUET=contextbench_verified_test.parquet ;;
    *) echo "ERROR: unknown DATASET '$DATASET' (verified|full|train|test)"; exit 2 ;;
esac
GOLD="$DATA_ROOT/contextbench/data/$PARQUET"
[ -f "$GOLD" ] || { echo "ERROR: $GOLD missing (run 02-bootstrap.sh)"; exit 1; }

# A run is permanently bound to the dataset it started with. Refuse to resume
# an existing RUN_ID under a different DATASET: rebinding the gold parquet
# would evaluate old predictions against the wrong gold (train is a subset of
# verified, so the result would LOOK clean and be wrong).
META="$RESULTS/run_meta.json"
if [ -s "$META" ]; then
    STORED_DATASET="$("$VENV_PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("dataset",""))' "$META")"
    if [ -n "$STORED_DATASET" ] && [ "$STORED_DATASET" != "$DATASET" ]; then
        echo "ERROR: run $RUN_ID was started with DATASET=$STORED_DATASET but the current config says DATASET=$DATASET."
        echo "       Set DATASET=$STORED_DATASET to resume this run, or start a new run (fresh RUN_ID) for $DATASET."
        exit 2
    fi
    STORED_LANE="$("$VENV_PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("benchmark_lane","retrieval"))' "$META")"
    STORED_AGENT_MODEL="$("$VENV_PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("agent_model","openai/gpt-5"))' "$META")"
    STORED_AGENT_HISTORY_DAYS="$("$VENV_PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("agent_history_days",30))' "$META")"
    if [ "$STORED_LANE" != "$BENCHMARK_LANE" ] || { { [ "$BENCHMARK_LANE" = "agent" ] || [ "$BENCHMARK_LANE" = "codex" ]; } && { [ "$STORED_AGENT_MODEL" != "$AGENT_MODEL" ] || [ "$STORED_AGENT_HISTORY_DAYS" != "$AGENT_HISTORY_DAYS" ]; }; }; then
        echo "ERROR: run $RUN_ID is bound to lane=$STORED_LANE agent_model=$STORED_AGENT_MODEL history_days=$STORED_AGENT_HISTORY_DAYS."
        echo "       Agent-policy changes require a fresh RUN_ID; they may not be mixed into a resumed run."
        exit 2
    fi
    STORED_POST_SELECTOR_POLICY="$("$VENV_PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("post_selector_policy","off"))' "$META")"
    if [ "$STORED_POST_SELECTOR_POLICY" != "$POST_SELECTOR_POLICY" ]; then
        echo "ERROR: run $RUN_ID was started with POST_SELECTOR_POLICY=$STORED_POST_SELECTOR_POLICY but the current config says POST_SELECTOR_POLICY=$POST_SELECTOR_POLICY."
        echo "       Post-selector policy changes require a fresh RUN_ID; they may not be mixed into a resumed run."
        exit 2
    fi
    STORED_PACK_POLICY="$("$VENV_PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("cb_pack_policy","v1"))' "$META")"
    STORED_QUERY_STRATEGY="$("$VENV_PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("cb_query_strategy","head"))' "$META")"
    STORED_SEARCH_LIMIT="$("$VENV_PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("cb_search_limit",20))' "$META")"
    if [ "$STORED_PACK_POLICY" != "$CB_PACK_POLICY" ] || [ "$STORED_QUERY_STRATEGY" != "$CB_QUERY_STRATEGY" ] || [ "$STORED_SEARCH_LIMIT" != "$CB_SEARCH_LIMIT" ]; then
        echo "ERROR: run $RUN_ID is bound to CB_PACK_POLICY=$STORED_PACK_POLICY CB_QUERY_STRATEGY=$STORED_QUERY_STRATEGY CB_SEARCH_LIMIT=$STORED_SEARCH_LIMIT."
        echo "       Retrieval-policy changes require a fresh RUN_ID; they may not be mixed into a resumed run."
        exit 2
    fi
fi

# Disk floor: every instance keeps a full repo clone under runs/<slug>/work
# for the life of the run (~300-450GB for the 1,136-task full set). Fail
# BEFORE starting rather than corrupting per-instance outputs mid-run.
# Free old space with: rm -rf $DATA_ROOT/results/<old-run-id>/runs
: "${DISK_FLOOR_GB:=150}"
FREE_GB="$(df -BG --output=avail "$DATA_ROOT" | tail -1 | tr -dc '0-9')"
if [ -n "$FREE_GB" ] && [ "$FREE_GB" -lt "$DISK_FLOOR_GB" ]; then
    echo "ERROR: only ${FREE_GB}GB free on $DATA_ROOT (< ${DISK_FLOOR_GB}GB floor)."
    echo "       Delete finished run trees (rm -rf $DATA_ROOT/results/<old-run-id>/runs) or grow the volume."
    echo "       Override the floor with DISK_FLOOR_GB=<n> if you know what you are doing."
    exit 2
fi
echo "disk: ${FREE_GB}GB free on $DATA_ROOT (floor ${DISK_FLOOR_GB}GB)"

# --- concurrency ---------------------------------------------------------------------
if [ "$CONCURRENCY" = "auto" ]; then
    CORES="$(nproc)"
    MEM_GB="$(awk '/MemTotal/{printf "%d", $2/1024/1024}' /proc/meminfo)"
    BY_CPU=$((CORES / 4))
    BY_MEM=$(((MEM_GB - 8) / 4))
    CONC=$((BY_CPU < BY_MEM ? BY_CPU : BY_MEM))
    [ "$CONC" -ge 1 ] || CONC=1
    echo "auto concurrency: min($CORES/4, ($MEM_GB-8)/4) = $CONC"
else
    CONC="$CONCURRENCY"
fi
[[ "$CONC" =~ ^[0-9]+$ ]] || { echo "ERROR: CONCURRENCY must be an integer or 'auto' (got '$CONCURRENCY')"; exit 2; }
[[ "$LINE_BUDGET" =~ ^[0-9]+$ ]] || { echo "ERROR: LINE_BUDGET must be an integer (got '$LINE_BUDGET')"; exit 2; }
[[ "$RUN_TIMEOUT" =~ ^[0-9]+$ ]] || { echo "ERROR: RUN_TIMEOUT must be an integer (got '$RUN_TIMEOUT')"; exit 2; }
[[ "$WATCHDOG_MINUTES" =~ ^[0-9]+$ ]] || { echo "ERROR: WATCHDOG_MINUTES must be an integer (got '$WATCHDOG_MINUTES')"; exit 2; }
[[ "$MANIFEST_LIMIT" =~ ^[0-9]+$ ]] || { echo "ERROR: MANIFEST_LIMIT must be a non-negative integer (got '$MANIFEST_LIMIT')"; exit 2; }
[[ -z "$MANIFEST_SOURCE_RUN_ID" || "$MANIFEST_SOURCE_RUN_ID" =~ ^[A-Za-z0-9_.-]+$ ]] \
    || { echo "ERROR: MANIFEST_SOURCE_RUN_ID contains unsafe characters"; exit 2; }
if [ "$MANIFEST_LIMIT" -gt 0 ] && [ -n "$MANIFEST_SOURCE_RUN_ID" ]; then
    echo "ERROR: MANIFEST_LIMIT and MANIFEST_SOURCE_RUN_ID are mutually exclusive"
    exit 2
fi
case "$SELECTOR_MODE" in
    default|guarded) ;;
    *) echo "ERROR: SELECTOR_MODE must be default|guarded (got '$SELECTOR_MODE')"; exit 2 ;;
esac
case "$POST_SELECTOR_POLICY" in
    off|offline-packing-v2|offline-packing-v3|offline-packing-v4) ;;
    *) echo "ERROR: POST_SELECTOR_POLICY must be off|offline-packing-v2|offline-packing-v3|offline-packing-v4 (got '$POST_SELECTOR_POLICY')"; exit 2 ;;
esac
case "$CB_PACK_POLICY" in
    v1|v2|v3|v4|v5) ;;
    *) echo "ERROR: CB_PACK_POLICY must be v1|v2|v3|v4|v5 (got '$CB_PACK_POLICY')"; exit 2 ;;
esac
case "$CB_QUERY_STRATEGY" in
    head|v2|v3|v4|v5) ;;
    *) echo "ERROR: CB_QUERY_STRATEGY must be head|v2|v3|v4|v5 (got '$CB_QUERY_STRATEGY')"; exit 2 ;;
esac

# --- manifest (all instance ids from the gold parquet) --------------------------------
MANIFEST="$RESULTS/manifest.json"
if [ ! -s "$MANIFEST" ]; then
    MANIFEST_SOURCE=""
    if [ -n "$MANIFEST_SOURCE_RUN_ID" ]; then
        MANIFEST_SOURCE="$DATA_ROOT/results/$MANIFEST_SOURCE_RUN_ID/manifest.json"
        [ -s "$MANIFEST_SOURCE" ] || { echo "ERROR: source manifest $MANIFEST_SOURCE is missing/empty"; exit 2; }
    fi
    "$VENV_PY" - "$GOLD" "$MANIFEST" "$MANIFEST_LIMIT" "$MANIFEST_SOURCE" <<'PY'
import json, random, sys
from pathlib import Path
import pyarrow.parquet as pq
table = pq.read_table(sys.argv[1], columns=["instance_id"])
gold_ids = [str(v) for v in table.column("instance_id").to_pylist()]
gold_set = set(gold_ids)
limit = int(sys.argv[3])
source = Path(sys.argv[4]) if sys.argv[4] else None
if source:
    ids = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(ids, list) or not ids or not all(isinstance(v, str) and v for v in ids):
        raise SystemExit(f"source manifest is not a non-empty string list: {source}")
    if len(ids) != len(set(ids)):
        raise SystemExit(f"source manifest contains duplicate instance IDs: {source}")
    unknown = sorted(set(ids) - gold_set)
    if unknown:
        raise SystemExit(f"source manifest has {len(unknown)} IDs outside the selected dataset: {unknown[:5]}")
    label = f"paired source={source.parent.name}"
else:
    ids = list(gold_ids)
# Dilute dataset-correlated slowness: the source parquet is NOT randomly
# ordered — it is grouped into a handful of large contiguous per-source-
# dataset blocks (verified-500 is exactly 4 blocks: SWE-Bench-Verified=174,
# SWE-Bench-Pro=54, SWE-PolyBench=116, Multi-SWE-Bench=156, confirmed by
# direct load 2026-07-12). parallel_driver.py's threading.Semaphore serves
# waiters in roughly thread-start order, and threads are started in this
# same manifest order, so an unshuffled manifest means every one of the N
# concurrency slots draws from the SAME slow dataset block at the same time
# for extended stretches instead of that slowness being diluted across the
# run — a correlated-tail-latency risk on top of the per-instance watchdog.
# Fixed seed: reproducible manifest.json if this file is ever regenerated.
    random.Random(42).shuffle(ids)
    if limit:
        ids = ids[:limit]
    label = f"shuffled, seed=42, limit={limit or 'all'}"
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(ids, fh, indent=0)
print(f"manifest: {len(ids)} instances ({label})")
PY
fi
TOTAL="$("$VENV_PY" -c 'import json,sys;print(len(json.load(open(sys.argv[1]))))' "$MANIFEST")"

# --- per-process thread cap (rayon) + OS-level core pinning (taskset) ----------------
# Each `memtrace mcp` sizes its GLOBAL rayon pool to ALL perf cores
# (crates/memtrace-mcp/src/resources.rs configure_rayon, IndexMode::Explicit:
# threads = topo.perf_cores) with no awareness of sibling processes, so N
# concurrent instances run N*cores runnable threads: on the 2026-07-11 E4 run
# 15 instances drove load ~1100 on 96 cores and indexing throughput collapsed.
# The binary honors MEMTRACE_MAX_THREADS (resources.rs) as an override, and
# runner.py passes the ambient env through to every memtrace child — cap each
# instance to its fair share of the box. min(CONC, TOTAL) is the real degree
# of parallelism; floor 4 keeps small shares workable. Explicit
# MEMTRACE_MAX_THREADS from the caller wins (but the pin slot geometry below
# ALWAYS uses this same per-instance share as its taskset core count, whether
# auto-computed or caller-supplied, so the app-level cap and the OS-level
# pin never disagree).
_PAR=$(( CONC < TOTAL ? CONC : TOTAL ))
[ "$_PAR" -ge 1 ] || _PAR=1
if [ -z "${MEMTRACE_MAX_THREADS:-}" ]; then
    MEMTRACE_MAX_THREADS=$(( $(nproc) / _PAR ))
    [ "$MEMTRACE_MAX_THREADS" -ge 4 ] || MEMTRACE_MAX_THREADS=4
fi
export MEMTRACE_MAX_THREADS
echo "thread cap: MEMTRACE_MAX_THREADS=$MEMTRACE_MAX_THREADS per instance (nproc=$(nproc), parallelism=min($CONC,$TOTAL)=$_PAR)"

# Layer A continued: export the slot geometry the taskset shim (installed
# above, source mode only) reads at invocation time. MEMTRACE_PIN_SLOTS is
# the real degree of parallelism (same $_PAR as the thread cap above, NOT
# raw $CONCURRENCY) so slot count and per-slot core count are always
# consistent with each other and with nproc: SLOTS * CORES_PER_SLOT <= nproc
# (the shim's last-slot remainder logic absorbs any leftover cores).
export MEMTRACE_PIN_SLOTS="$_PAR"
export MEMTRACE_PIN_CORES_PER_SLOT="$MEMTRACE_MAX_THREADS"
export MEMTRACE_PIN_LOCKDIR="$MEMTRACE_BIN_DIR/pin-slots"
export MEMTRACE_PIN_ENABLE
# Layer C: cap ORT's embed-session intra-op thread pool (independent of the
# rayon fix above; ORT defaults to 8 threads/process regardless of
# concurrency). runner.py -> memtrace inherit this through the same ambient-
# env pass-through as MEMTRACE_MAX_THREADS.
export MEMTRACE_EMBED_INTRA_OP_THREADS
# Locked retrieval policy pass-through (see the top-of-file defaults).
# POST_SELECTOR_POLICY is the canonical run-bound value recorded in
# run_meta.json. Override any inherited CB_POST_SELECTOR_POLICY so the
# driver's environment default can never enable a different policy while
# metadata and the fresh-run guard still say "off".
export CB_SEARCH_LIMIT CB_PACK_POLICY CB_QUERY_STRATEGY
export CB_POST_SELECTOR_POLICY="$POST_SELECTOR_POLICY"
if [ "$MEMTRACE_INSTALL_MODE" = "source" ] && [ "$MEMTRACE_PIN_ENABLE" = "1" ]; then
    mkdir -p "$MEMTRACE_PIN_LOCKDIR"
    _s=0
    while [ "$_s" -lt "$MEMTRACE_PIN_SLOTS" ]; do
        touch "$MEMTRACE_PIN_LOCKDIR/slot-$_s.lock"
        _s=$((_s + 1))
    done
    unset _s
    echo "taskset pin: MEMTRACE_PIN_SLOTS=$MEMTRACE_PIN_SLOTS x MEMTRACE_PIN_CORES_PER_SLOT=$MEMTRACE_PIN_CORES_PER_SLOT cores each -> $MEMTRACE_PIN_LOCKDIR ($MEMTRACE_PIN_SLOTS lockfiles pre-created)"
fi

# --- Layer A independent floor ------------------------------------------------------
# The shim install, the taskset/flock presence check, and even the "this
# WILL thrash" warning above all live inside the `MEMTRACE_INSTALL_MODE =
# source` branch — today's config.env hardcodes source mode, but that is a
# single string compare away from silently disabling the ENTIRE safety
# mechanism (a stale env var, a copy-pasted config.env, a future npm-mode
# emergency hotfix run). This check is independent of install mode: it
# inspects whatever `memtrace` ACTUALLY resolves to right now and refuses to
# proceed at a concurrency where unpinned execution is known to thrash
# (confirmed: 5 concurrent tasks drove load average 400-560 on a 96-192 core
# box on 2026-07-11/12) unless that resolved binary really is the taskset
# shim. Fails loudly here rather than letting CONCURRENCY>2 run unpinned
# with zero warning.
if [ "$MEMTRACE_PIN_ENABLE" = "1" ] && [ "$_PAR" -gt 2 ]; then
    RESOLVED_MEMTRACE="$(command -v memtrace || true)"
    if [ -z "$RESOLVED_MEMTRACE" ] || ! grep -q 'MEMTRACE-TASKSET-SHIM' "$RESOLVED_MEMTRACE" 2>/dev/null; then
        echo "FATAL: MEMTRACE_PIN_ENABLE=1 and effective per-instance parallelism=$_PAR (>2) but 'memtrace' resolves to '${RESOLVED_MEMTRACE:-<not found>}', which is NOT the taskset shim (install_mode=$MEMTRACE_INSTALL_MODE). Running CONCURRENCY>2 unpinned WILL thrash. Refusing to start. Fix: use MEMTRACE_INSTALL_MODE=source (the only mode that installs the shim today), or explicitly set MEMTRACE_PIN_ENABLE=0 to knowingly accept an unpinned run (only sane at CONCURRENCY<=2)."
        exit 1
    fi
    echo "pin floor: verified taskset shim active at $RESOLVED_MEMTRACE (independent check, install_mode=$MEMTRACE_INSTALL_MODE)"
fi
unset _PAR
echo "locked policy: lane=$BENCHMARK_LANE agent_model=$AGENT_MODEL agent_history_days=$AGENT_HISTORY_DAYS CB_SEARCH_LIMIT=$CB_SEARCH_LIMIT CB_PACK_POLICY=$CB_PACK_POLICY CB_QUERY_STRATEGY=$CB_QUERY_STRATEGY MEMTRACE_EMBED_INTRA_OP_THREADS=$MEMTRACE_EMBED_INTRA_OP_THREADS SELECTOR_MODEL=$SELECTOR_MODEL SELECTOR_MODE=$SELECTOR_MODE POST_SELECTOR_POLICY=$POST_SELECTOR_POLICY LINE_BUDGET=$LINE_BUDGET"

completed_count() {
    find "$RESULTS/runs" -mindepth 2 -maxdepth 2 -name prediction.jsonl -size +0c 2>/dev/null | wc -l | tr -d ' '
}
echo "$(date +%s) $(completed_count)" > "$RESULTS/session_start"

# --- run metadata (no secrets) -----------------------------------------------------------
# Values travel as argv (quoted heredoc): a quote in SELECTOR_MODEL or a typo
# in a numeric var must not become a Python SyntaxError. dataset/gold are
# guaranteed consistent with any prior session by the guard above.
# Binary provenance: install mode + the runtime `memtrace --version` are always
# recorded; in source mode the full build manifest (HEAD sha, git describe,
# dirty state, binary sha256, rustc) is embedded under "memtrace_source".
"$VENV_PY" - "$RESULTS/run_meta.json" "$RUN_ID" "$DATASET" "$GOLD" \
    "$BENCHMARK_LANE" "$SELECTOR_MODEL" "$AGENT_MODEL" "$AGENT_HISTORY_DAYS" "$SELECTOR_MODE" "$CACHE_NAMESPACE" "$CONC" "$LINE_BUDGET" "$RUN_TIMEOUT" \
    "$WATCHDOG_MINUTES" "$MEMTRACE_INSTALL_MODE" "$MEMTRACE_VERSION_RUNTIME" \
    "$(command -v memtrace)" "$SOURCE_MANIFEST" \
    "$MEMTRACE_PIN_ENABLE" "$MEMTRACE_PIN_SLOTS" "$MEMTRACE_PIN_CORES_PER_SLOT" \
    "$MEMTRACE_MAX_THREADS" "$MEMTRACE_EMBED_INTRA_OP_THREADS" "$CB_SEARCH_LIMIT" "$CB_PACK_POLICY" "$CB_QUERY_STRATEGY" "$POST_SELECTOR_POLICY" \
    "$MANIFEST_LIMIT" "$MANIFEST_SOURCE_RUN_ID" "$TOTAL" <<'PY'
import json, sys
(path, run_id, dataset, gold, benchmark_lane, selector, agent_model, agent_history_days,
 selector_mode, namespace, conc, line_budget, timeout,
 watchdog_min, install_mode, mt_version, mt_binary, source_manifest,
 pin_enable, pin_slots, pin_cores_per_slot, max_threads, embed_threads,
 search_limit, pack_policy, query_strategy, post_selector_policy, manifest_limit,
 manifest_source_run_id, manifest_instances) = sys.argv[1:31]
meta = {
    "run_id": run_id,
    "dataset": dataset,
    "gold_parquet": gold,
    "benchmark_lane": benchmark_lane,
    "selector_model": selector,
    "agent_model": agent_model,
    "agent_history_days": int(agent_history_days),
    "selector_mode": selector_mode,
    "cache_namespace": namespace,
    "concurrency": int(conc),
    "line_budget": int(line_budget),
    "timeout": int(timeout),
    "watchdog_minutes": int(watchdog_min),
    "memtrace_install_mode": install_mode,
    "memtrace_version": mt_version,
    "memtrace_binary": mt_binary,
    "cb_search_limit": int(search_limit),
    "cb_pack_policy": pack_policy,
    "cb_query_strategy": query_strategy,
    "post_selector_policy": post_selector_policy,
    "manifest_instances": int(manifest_instances),
    "manifest_limit_requested": int(manifest_limit),
    "manifest_source_run_id": manifest_source_run_id or None,
    "core_pinning": {
        "enabled": pin_enable == "1",
        "slots": int(pin_slots) if pin_slots else None,
        "cores_per_slot": int(pin_cores_per_slot) if pin_cores_per_slot else None,
        "memtrace_max_threads": int(max_threads),
        "memtrace_embed_intra_op_threads": int(embed_threads),
    },
}
if source_manifest:
    with open(source_manifest) as fh:
        meta["memtrace_source"] = json.load(fh)
with open(path, "w") as fh:
    json.dump(meta, fh, indent=2)
PY

# Read and later signal an exact Linux process identity. Cleanup never uses a
# bare PID: a child that exited early must not let PID reuse target an unrelated
# benchmark worker (or any other process on the box).
process_start_ticks() {
    "$VENV_PY" - "$1" <<'PY'
import sys
from pathlib import Path

pid = int(sys.argv[1])
row = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
closing = row.rfind(")")
tail = row[closing + 1 :].split()
if closing < 1 or len(tail) < 20:
    raise SystemExit(f"cannot parse /proc identity for pid {pid}")
print(int(tail[19]))
PY
}

signal_exact_process() {
    "$VENV_PY" - "$1" "$2" "$3" <<'PY'
import os, signal, sys
from pathlib import Path

pid, expected_start, signal_number = map(int, sys.argv[1:])
try:
    descriptor = os.pidfd_open(pid, 0)
except ProcessLookupError:
    raise SystemExit(0)
try:
    try:
        row = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(0)
    closing = row.rfind(")")
    tail = row[closing + 1 :].split()
    observed_start = int(tail[19])
    if observed_start != expected_start:
        print(
            f"[cleanup] skip stale pid {pid}: expected start {expected_start}, "
            f"observed {observed_start}",
            file=sys.stderr,
        )
        raise SystemExit(0)
    signal.pidfd_send_signal(descriptor, signal_number, None, 0)
finally:
    os.close(descriptor)
PY
}

reap_orphans_once() {
    "$VENV_PY" <<'PY'
import os, signal, time
from pathlib import Path


def parse_stat(row: str) -> tuple[int, int]:
    closing = row.rfind(")")
    tail = row[closing + 1 :].split()
    if closing < 1 or len(tail) < 20:
        raise ValueError("malformed proc stat")
    return int(row[: row.find("(")].strip()), int(tail[1])


def is_memtrace_mcp(argv: list[str]) -> bool:
    return any(
        Path(argument).name in {"memtrace", "memtrace.real"}
        and index + 1 < len(argv)
        and argv[index + 1] == "mcp"
        for index, argument in enumerate(argv)
    )


for proc in Path("/proc").iterdir():
    if not proc.name.isdigit():
        continue
    pid = int(proc.name)
    try:
        descriptor = os.pidfd_open(pid, 0)
    except (PermissionError, ProcessLookupError):
        continue
    try:
        try:
            observed_pid, ppid = parse_stat((proc / "stat").read_text(encoding="utf-8"))
            argv = [
                part.decode("utf-8", errors="replace")
                for part in (proc / "cmdline").read_bytes().split(b"\0")
                if part
            ]
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, OSError):
            continue
        if observed_pid != pid or ppid != 1 or not is_memtrace_mcp(argv):
            continue
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL, None, 0)
        except (PermissionError, ProcessLookupError):
            continue
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        print(f"[reaper] {stamp} killed verified orphaned memtrace mcp pid={pid}")
    finally:
        os.close(descriptor)
PY
}

WATCHER_PID=""
WATCHER_START_TICKS=""
REAPER_PID=""
REAPER_START_TICKS=""
WATCHDOG_PID=""
WATCHDOG_START_TICKS=""
WATCHDOG_EXIT_MARKER=""

# Install cleanup before the first background child. Every identity is
# initialized, so a startup/readiness failure under `set -e` cannot bypass
# exact child cleanup or accidentally signal an unverified PID.
# shellcheck disable=SC2329  # invoked via the EXIT trap
cleanup() {
    rc=$?
    if [ -n "$WATCHDOG_EXIT_MARKER" ]; then
        echo "$rc" > "$WATCHDOG_EXIT_MARKER"
    fi
    if [ -n "$WATCHER_PID" ] && [ -n "$WATCHER_START_TICKS" ]; then
        signal_exact_process "$WATCHER_PID" "$WATCHER_START_TICKS" 15 || true
    fi
    if [ -n "$REAPER_PID" ] && [ -n "$REAPER_START_TICKS" ]; then
        signal_exact_process "$REAPER_PID" "$REAPER_START_TICKS" 15 || true
    fi
    if [ -n "$WATCHDOG_PID" ] && [ -n "$WATCHDOG_START_TICKS" ]; then
        signal_exact_process "$WATCHDOG_PID" "$WATCHDOG_START_TICKS" 15 || true
    fi
    echo "$rc" > "$RESULTS/driver_exit"
    reap_orphans_once || true
    sync
    echo "=== run-remote done rc=$rc $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
}
trap cleanup EXIT

# --- spot interruption watcher ------------------------------------------------------------
bash "$ADAPTER/aws/remote/spot-watcher.sh" "$RESULTS" >>"$RESULTS/spot-watcher.log" 2>&1 &
WATCHER_PID=$!
WATCHER_START_TICKS="$(process_start_ticks "$WATCHER_PID")"

# --- periodic orphan reaper -----------------------------------------------------------------
# The hardened driver normally terminates an isolated runner process group, so
# runner.py and its 'memtrace mcp' descendants exit together. Keep a PPID-1
# reaper as defense in depth for an abrupt external-watchdog kill or driver
# crash that races descendant cleanup; an orphaned embedded MemDB can otherwise
# retain multi-GB RSS for the rest of the run. Live instances' mcp processes
# still have runner.py as parent and are never selected here.
# shellcheck disable=SC2329  # runs in the background subshell below
reap_orphans() {
    while sleep 60; do
        reap_orphans_once
    done
}
reap_orphans &
REAPER_PID=$!
REAPER_START_TICKS="$(process_start_ticks "$REAPER_PID")"

# --- per-instance watchdog ---------------------------------------------------------------
# The driver has its own two-hour timeout. This stricter guard uses /proc
# monotonic start ticks and two consecutive over-limit observations so one
# invalid elapsed-time sample cannot turn a brand-new task into a benchmark
# failure.
WATCHDOG_LOG="$RESULTS/watchdog.log"
WATCHDOG_SESSION_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
WATCHDOG_SESSION_DIR="$RESULTS/watchdog-sessions/$WATCHDOG_SESSION_ID"
WATCHDOG_EXIT_MARKER="$WATCHDOG_SESSION_DIR/driver_exit"
WATCHDOG_SESSION_FILE="$WATCHDOG_SESSION_DIR/session.json"
mkdir -p "$WATCHDOG_SESSION_DIR"
WATCHDOG_OWNER_START_TICKS="$("$VENV_PY" - "$$" <<'PY'
import sys
from pathlib import Path

pid = int(sys.argv[1])
row = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
closing = row.rfind(")")
tail = row[closing + 1 :].split()
if closing < 1 or len(tail) < 20:
    raise SystemExit("cannot parse watchdog owner /proc stat")
print(int(tail[19]))
PY
)"
"$VENV_PY" "$ADAPTER/aws/remote/watchdog_remote.py" \
    --results "$RESULTS" \
    --limit-seconds "$((WATCHDOG_MINUTES * 60))" \
    --owner-pid "$$" \
    --owner-start-ticks "$WATCHDOG_OWNER_START_TICKS" \
    --exit-marker "$WATCHDOG_EXIT_MARKER" \
    --session-id "$WATCHDOG_SESSION_ID" \
    --session-file "$WATCHDOG_SESSION_FILE" &
WATCHDOG_PID=$!
WATCHDOG_START_TICKS="$(process_start_ticks "$WATCHDOG_PID" 2>/dev/null || true)"
for _ in {1..50}; do
    if [ -s "$WATCHDOG_SESSION_FILE" ] && kill -0 "$WATCHDOG_PID" 2>/dev/null; then
        break
    fi
    sleep 0.1
done
if [ ! -s "$WATCHDOG_SESSION_FILE" ] || ! kill -0 "$WATCHDOG_PID" 2>/dev/null; then
    [ -n "$WATCHDOG_START_TICKS" ] \
        && signal_exact_process "$WATCHDOG_PID" "$WATCHDOG_START_TICKS" 15 \
        || true
    echo "ERROR: watchdog failed its startup/readiness handshake; see $WATCHDOG_SESSION_DIR/stdout/log output and $WATCHDOG_LOG"
    exit 1
fi
if ! WATCHDOG_VERIFIED_START_TICKS="$("$VENV_PY" - \
    "$WATCHDOG_SESSION_FILE" \
    "$WATCHDOG_PID" \
    "$$" \
    "$WATCHDOG_OWNER_START_TICKS" \
    "$ADAPTER/aws/remote/watchdog_remote.py" <<'PY'
import hashlib, json, os, sys
from pathlib import Path

(
    session_path,
    expected_pid,
    expected_owner_pid,
    expected_owner_start,
    script_path,
) = sys.argv[1:]
session = json.loads(Path(session_path).read_text(encoding="utf-8"))
script_sha = hashlib.sha256(Path(script_path).read_bytes()).hexdigest()
checks = {
    "watchdog_pid": session.get("watchdog_pid") == int(expected_pid),
    "owner_pid": session.get("owner_pid") == int(expected_owner_pid),
    "owner_start_ticks": session.get("owner_start_ticks") == int(expected_owner_start),
    "watchdog_sha256": session.get("watchdog_sha256") == script_sha,
}
failed = [name for name, passed in checks.items() if not passed]
if failed:
    raise SystemExit("watchdog readiness mismatch: " + ",".join(failed))
pid = int(expected_pid)
expected_start = int(session["watchdog_start_ticks"])
descriptor = os.pidfd_open(pid, 0)
try:
    row = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    closing = row.rfind(")")
    tail = row[closing + 1 :].split()
    state = tail[0]
    observed_start = int(tail[19])
    if observed_start != expected_start or state == "Z":
        raise SystemExit(
            f"watchdog readiness identity/state mismatch: "
            f"expected={pid}:{expected_start} observed={pid}:{observed_start} state={state}"
        )
finally:
    os.close(descriptor)
print(expected_start)
PY
)"; then
    [ -n "$WATCHDOG_START_TICKS" ] \
        && signal_exact_process "$WATCHDOG_PID" "$WATCHDOG_START_TICKS" 15 \
        || true
    echo "ERROR: watchdog provenance/identity readiness verification failed"
    exit 1
fi
WATCHDOG_START_TICKS="$WATCHDOG_VERIFIED_START_TICKS"
unset WATCHDOG_VERIFIED_START_TICKS
echo "watchdog: per-instance limit ${WATCHDOG_MINUTES}min (pidfd/two-sample) session=$WATCHDOG_SESSION_ID -> $WATCHDOG_LOG"

# Legacy post-run defense: the hardened driver validates every successful
# prediction and emits a failure stub for missing/empty output. Retain this
# sweep so an artifact produced by an older/stale driver is still classified
# distinctly from a timeout instead of being trusted as success.
scan_empty_predictions() {
    [ -s "$RESULTS/driver_summary.json" ] || return 0
    "$VENV_PY" - "$RESULTS/driver_summary.json" "$WATCHDOG_LOG" <<'PY'
import json, sys, time
from pathlib import Path
summary_path, log_path = sys.argv[1:3]
summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))
stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
lines = []
for iid, info in sorted(summary.get("per_instance", {}).items()):
    if info.get("skipped") or info.get("returncode") != 0:
        continue
    pred = Path(info.get("prediction", ""))
    if not pred.is_file() or pred.stat().st_size == 0:
        lines.append(
            f"{stamp} outcome=empty_prediction instance={iid} rc=0 "
            f"log={info.get('log', '')}\n"
        )
if lines:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.writelines(lines)
    print(f"[watchdog] {len(lines)} instance(s) exited rc=0 with EMPTY/missing "
          f"prediction (silent failure) — details in watchdog.log")
PY
}

# --- driver ------------------------------------------------------------------------------------
DRIVER_ARGS=(
    "$ADAPTER/parallel_driver.py"
    --dataset "$GOLD"
    --manifest "$MANIFEST"
    --output-dir "$RESULTS"
    --lane "$BENCHMARK_LANE"
    --concurrency "$CONC"
    --line-budget "$LINE_BUDGET"
    --selector-model "$SELECTOR_MODEL"
    --rerank-model-dir "$DATA_ROOT/rerank-model"
    --reinclude-tracked-dirs
)
if [ "$BENCHMARK_LANE" = "agent" ]; then
    BASE_AGENT_CONFIG="$DATA_ROOT/contextbench/agent-frameworks/mini-swe-agent/multi-poly-pro-verified/configs/swebench_following_context.yaml"
    [ -f "$BASE_AGENT_CONFIG" ] \
        || { echo "ERROR: ContextBench agent config missing: $BASE_AGENT_CONFIG"; exit 2; }
    DRIVER_ARGS+=(
        --contextbench-root "$DATA_ROOT/contextbench"
        --base-agent-config "$BASE_AGENT_CONFIG"
        --agent-model "$AGENT_MODEL"
        --history-days "$AGENT_HISTORY_DAYS"
        --graph-cache-dir "$DATA_ROOT/graph-cache-agent"
        --cache-namespace "$CACHE_NAMESPACE-agent-hierarchy-v2"
    )
elif [ "$BENCHMARK_LANE" = "codex" ]; then
    CODEX_BINARY="$DATA_ROOT/codex-bin/codex"
    MEMTRACE_SKILLS_DIR="$DATA_ROOT/memtrace-src/installer/plugins/memtrace-skills/skills"
    [ -x "$CODEX_BINARY" ] \
        || { echo "ERROR: Codex CLI missing: $CODEX_BINARY (rerun bootstrap)"; exit 2; }
    [ -f "$MEMTRACE_SKILLS_DIR/memtrace-first/SKILL.md" ] \
        || { echo "ERROR: shipped Memtrace skills missing: $MEMTRACE_SKILLS_DIR"; exit 2; }
    DRIVER_ARGS+=(
        --agent-model "${AGENT_MODEL#openai/}"
        --history-days "$AGENT_HISTORY_DAYS"
        --graph-cache-dir "$DATA_ROOT/graph-cache-agent"
        --cache-namespace "$CACHE_NAMESPACE"
        --codex-binary "$CODEX_BINARY"
        --memtrace-binary "$MEMTRACE_BIN_DIR/memtrace"
        --memtrace-skills-dir "$MEMTRACE_SKILLS_DIR"
    )
fi
# Guarded selector: only forwarded when requested, and refuse to run silently
# in default mode if the adapter on the box predates the flag.
if [ "$SELECTOR_MODE" != "default" ]; then
    grep -q -- '"--selector-mode"' "$ADAPTER/parallel_driver.py" \
        || { echo "ERROR: SELECTOR_MODE=$SELECTOR_MODE but parallel_driver.py has no --selector-mode pass-through (stale adapter on the box?)"; exit 2; }
    DRIVER_ARGS+=(--selector-mode "$SELECTOR_MODE")
fi
if [ "$POST_SELECTOR_POLICY" != "off" ]; then
    grep -q -- '"--post-selector-policy"' "$ADAPTER/parallel_driver.py" \
        || { echo "ERROR: POST_SELECTOR_POLICY=$POST_SELECTOR_POLICY but parallel_driver.py has no --post-selector-policy pass-through (stale adapter on the box?)"; exit 2; }
    DRIVER_ARGS+=(--post-selector-policy "$POST_SELECTOR_POLICY")
fi
if [ "$BENCHMARK_LANE" != "codex" ]; then
    DRIVER_ARGS+=(--query-plans)
fi
DRIVER_ARGS+=(--timeout "$RUN_TIMEOUT" --chdir "$ADAPTER")
if grep -q -- '--resume' "$ADAPTER/parallel_driver.py"; then
    DRIVER_ARGS+=(--resume)
fi
# The two lanes keep separate graph-cache roots and policy namespaces.
if [ "$BENCHMARK_LANE" = "retrieval" ]; then
    grep -q -- '"--graph-cache-dir"' "$ADAPTER/parallel_driver.py" \
        || { echo "ERROR: retrieval lane requires graph-cache pass-through in parallel_driver.py"; exit 2; }
    DRIVER_ARGS+=(--graph-cache-dir "$DATA_ROOT/graph-cache" --cache-namespace "$CACHE_NAMESPACE")
fi

echo "driver: lane=$BENCHMARK_LANE concurrency=$CONC total=$TOTAL timeout=${RUN_TIMEOUT}s selector=$SELECTOR_MODEL agent=$AGENT_MODEL history=${AGENT_HISTORY_DAYS}d mode=$SELECTOR_MODE post_selector=$POST_SELECTOR_POLICY"
set +e
"$VENV_PY" "${DRIVER_ARGS[@]}"
DRIVER_RC=$?
set -e
scan_empty_predictions
if [ -s "$WATCHDOG_LOG" ]; then
    echo "watchdog events this run:"
    grep -c 'outcome=timeout' "$WATCHDOG_LOG" | xargs -I{} echo "  timeouts          : {}" || true
    grep -c 'outcome=empty_prediction' "$WATCHDOG_LOG" | xargs -I{} echo "  empty predictions : {}" || true
fi
echo "parallel_driver.py exited rc=$DRIVER_RC ($(completed_count)/$TOTAL predictions)"
exit "$DRIVER_RC"
