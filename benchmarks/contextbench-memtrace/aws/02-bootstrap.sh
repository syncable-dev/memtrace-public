#!/usr/bin/env bash
# 02-bootstrap.sh — push the adapter + secrets to the box and run the remote
# bootstrap (mount data volume, apt deps, python3.11 venv, docker, Node+memtrace,
# rerank model, ContextBench checkout, embedding-model pre-warm). Idempotent.
#
# Secrets: the OpenAI key travels ONLY via scp of memtrace-public/.env to
# ~/contextbench-adapter/.env (chmod 600). It is never placed in user-data,
# state.json, or logs.
# shellcheck source-path=SCRIPTDIR

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$SCRIPT_DIR/common.sh"

require_cmd rsync
require_cmd scp

IP="$(instance_ip)"

[ -f "$ENV_FILE" ] || die "$ENV_FILE not found (needs OPENAI_API_KEY=...)"
grep -q '^OPENAI_API_KEY=..*' "$ENV_FILE" || die "OPENAI_API_KEY missing/empty in $ENV_FILE"

# --- 1. push adapter code -----------------------------------------------------
info "rsyncing adapter -> $REMOTE_USER@$IP:$REMOTE_ADAPTER_DIR"
remote "mkdir -p $REMOTE_ADAPTER_DIR"
rsync_adapter

# --- 2. run remote bootstrap ------------------------------------------------------
# Deliberately BEFORE the .env push: the remote bootstrap runs unverified
# third-party code (nvm installer, deadsnakes PPA, rustup in source mode) as
# this user, and the key is only needed at 03-run time — so the secret never
# coexists with the install steps. DATA_VOL_ID pins the data-volume device
# deterministically.
info "running remote bootstrap (this can take ~10-20 min on first run: apt, node, ~640MB embed model)"
remote "bash $REMOTE_ADAPTER_DIR/aws/remote/bootstrap-remote.sh MEMTRACE_VERSION=$(printf '%q' "$MEMTRACE_VERSION") MEMTRACE_INSTALL_MODE=$(printf '%q' "$MEMTRACE_INSTALL_MODE") DATA_VOL_ID=$(printf '%q' "$(state_get volume_id)")"

# --- 2b. source mode: ship the private repo source + build on the box ----------------
# The source never leaves the user's own infra: laptop -> their EC2 box, over
# ssh/rsync only. .git objects are NOT shipped; provenance (HEAD sha, describe,
# submodules, publishable/dirty state, and source-payload checksum) is captured
# locally BEFORE the rsync into source-manifest.json.
if [ "$MEMTRACE_INSTALL_MODE" = "source" ]; then
    [ -d "$MEMTRACE_SOURCE_DIR" ] || die "MEMTRACE_INSTALL_MODE=source but MEMTRACE_SOURCE_DIR=$MEMTRACE_SOURCE_DIR does not exist"
    # --no-optional-locks on every git call: the private repo is actively
    # edited by another agent, and a plain status/diff opportunistically
    # WRITES .git/index (their commands would fail on index.lock).
    git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" rev-parse HEAD >/dev/null 2>&1 \
        || die "source mode: $MEMTRACE_SOURCE_DIR is not a git repo (cannot record provenance)"

    SRC_HEAD="$(git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" rev-parse HEAD)"
    SRC_DESCRIBE="$(git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" describe --tags --dirty --always 2>/dev/null || echo unknown)"
    SRC_DIRTY_COUNT="$(git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
    if [ "$SRC_DIRTY_COUNT" -gt 0 ] && [ "$ALLOW_DIRTY_SOURCE" != "1" ]; then
        die "source tree has $SRC_DIRTY_COUNT dirty file(s); refusing a non-reproducible build (use a clean worktree)"
    fi
    if git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" submodule status --recursive | grep -q '^-'; then
        die "source submodules are not initialized; run git submodule update --init --recursive in $MEMTRACE_SOURCE_DIR"
    fi
    SRC_DIFF_SHA=""
    if [ "$SRC_DIRTY_COUNT" -gt 0 ]; then
        SRC_DIFF_SHA="$(git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" diff HEAD 2>/dev/null | shasum -a 256 | cut -c1-16)"
        warn "ALLOW_DIRTY_SOURCE=1: shipping a diagnostic dirty tree ($SRC_DIRTY_COUNT files, diff sha $SRC_DIFF_SHA)"
    fi
    PAYLOAD_LIST_LOCAL="$STATE_DIR/source-files.list0"
    CHECKSUM_LOCAL="$STATE_DIR/source-files.sha256"
    (
        cd "$MEMTRACE_SOURCE_DIR"
        { git --no-optional-locks ls-files --recurse-submodules -z; git --no-optional-locks ls-files --others --exclude-standard -z; }
    ) >"$PAYLOAD_LIST_LOCAL"
    [ -s "$PAYLOAD_LIST_LOCAL" ] || die "source payload file list is empty"
    (
        cd "$MEMTRACE_SOURCE_DIR"
        xargs -0 shasum -a 256 <"$PAYLOAD_LIST_LOCAL"
    ) >"$CHECKSUM_LOCAL"
    [ -s "$CHECKSUM_LOCAL" ] || die "source payload checksum list is empty"
    SRC_FILE_COUNT="$(awk 'END { print NR }' "$CHECKSUM_LOCAL")"
    SRC_PAYLOAD_SHA="$(shasum -a 256 "$CHECKSUM_LOCAL" | cut -d' ' -f1)"
    SRC_SUBMODULES="$(git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" submodule status --recursive)"
    MANIFEST_LOCAL="$STATE_DIR/source-manifest.json"
    python3 - "$MANIFEST_LOCAL" "$SRC_HEAD" "$SRC_DESCRIBE" "$SRC_DIRTY_COUNT" "$SRC_DIFF_SHA" "$MEMTRACE_SOURCE_DIR" "$SRC_PAYLOAD_SHA" "$SRC_FILE_COUNT" "$SRC_SUBMODULES" <<'PY'
import json, socket, sys, time
path, head, describe, dirty, diff_sha, src, payload_sha, file_count, submodules = sys.argv[1:10]
manifest = {
    "install_mode": "source",
    "head_sha": head,
    "git_describe": describe,
    "dirty_file_count": int(dirty),
    "dirty_diff_sha256_16": diff_sha or None,
    "source_dir_local": src,
    "source_payload_sha256": payload_sha,
    "source_file_count": int(file_count),
    "submodules": submodules.splitlines(),
    "publishable_source": int(dirty) == 0,
    "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "captured_on": socket.gethostname(),
}
with open(path, "w") as fh:
    json.dump(manifest, fh, indent=2)
    fh.write("\n")
PY

    REMOTE_SRC_DIR=/srv/contextbench/memtrace-src
    info "streaming exact Git-derived source payload -> $REMOTE_USER@$IP:$REMOTE_SRC_DIR ($SRC_FILE_COUNT files)"
    remote "mkdir -p $REMOTE_SRC_DIR; find $REMOTE_SRC_DIR -mindepth 1 -maxdepth 1 ! -name target -exec rm -rf -- {} +"
    # Use one NUL-delimited Git-derived list for BOTH the archive and hashes.
    # This includes intentionally tracked files even when a .gitignore rule
    # also matches them, includes initialized submodule contents, and excludes
    # ignored build caches by construction. The prior rsync filter disagreed
    # with its checksum universe (notably tracked node_modules and .env.example
    # files), making a reproducible bootstrap impossible. Keep target/ above as
    # the only remote incremental cache; every other top-level entry was reset.
    # shellcheck disable=SC2029  # REMOTE_SRC_DIR is intentionally expanded locally
    (
        cd "$MEMTRACE_SOURCE_DIR"
        COPYFILE_DISABLE=1 tar --no-xattrs --null -T "$PAYLOAD_LIST_LOCAL" -czf -
    ) | ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$IP" \
        "tar -xzf - -C $REMOTE_SRC_DIR"

    # The manifest/checksum list was captured BEFORE rsync. Any local drift or
    # transfer mismatch is fatal: a benchmark binary must map to one payload.
    POST_HEAD="$(git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" rev-parse HEAD)"
    POST_DIFF_SHA=""
    if [ -n "$(git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" status --porcelain 2>/dev/null)" ]; then
        POST_DIFF_SHA="$(git --no-optional-locks -C "$MEMTRACE_SOURCE_DIR" diff HEAD 2>/dev/null | shasum -a 256 | cut -c1-16)"
    fi
    if [ "$POST_HEAD" != "$SRC_HEAD" ] || [ "$POST_DIFF_SHA" != "$SRC_DIFF_SHA" ]; then
        die "source tree changed during transfer; re-run 02-bootstrap.sh from a stable clean worktree"
    fi
    scp "${SSH_OPTS[@]}" -q "$MANIFEST_LOCAL" "$REMOTE_USER@$IP:$REMOTE_SRC_DIR/source-manifest.json"
    scp "${SSH_OPTS[@]}" -q "$CHECKSUM_LOCAL" "$REMOTE_USER@$IP:$REMOTE_SRC_DIR/source-files.sha256"
    remote "cd $REMOTE_SRC_DIR && sha256sum -c source-files.sha256 >/dev/null" \
        || die "remote source payload does not match the local checksum manifest"
    info "source payload verified: $SRC_PAYLOAD_SHA"

    info "building memtrace from source on the box (rustup + cargo release; ~96 cores)"
    remote "bash $REMOTE_ADAPTER_DIR/aws/remote/build-memtrace-remote.sh SRC_DIR=$(printf '%q' "$REMOTE_SRC_DIR")"
fi

# --- 3. push .env (secret) out-of-band, only after bootstrap+build succeeded ---------
info "copying .env (chmod 600, never in user-data/logs)"
scp "${SSH_OPTS[@]}" -q "$ENV_FILE" "$REMOTE_USER@$IP:$REMOTE_ADAPTER_DIR/.env"
remote "chmod 600 $REMOTE_ADAPTER_DIR/.env"

# --- 4. datasets: verify, and seed from the local checkout if the clone lacks them ---
# full.parquet is always required (train/test splits lack repo_url and the
# runner merges it from the sibling full.parquet); the configured DATASET's
# own parquet must be present too, or 03-run only fails later inside tmux.
case "$DATASET" in
    verified) DATASET_PARQUET=contextbench_verified.parquet ;;
    full)     DATASET_PARQUET=full.parquet ;;
    train)    DATASET_PARQUET=contextbench_verified_train.parquet ;;
    test)     DATASET_PARQUET=contextbench_verified_test.parquet ;;
    *) die "unknown DATASET '$DATASET' in config (verified|full|train|test)" ;;
esac
DATA_DIR=/srv/contextbench/contextbench/data
if remote "test -f $DATA_DIR/full.parquet && test -f $DATA_DIR/$DATASET_PARQUET"; then
    info "ContextBench datasets present on the box (full.parquet + $DATASET_PARQUET)"
else
    warn "ContextBench clone is missing $DATA_DIR/full.parquet and/or $DATA_DIR/$DATASET_PARQUET"
    if [ -d /tmp/contextbench/data ]; then
        info "seeding datasets from local /tmp/contextbench/data ..."
        rsync -az -e "$(ssh_cmd_string)" \
            /tmp/contextbench/data/ \
            "$REMOTE_USER@$IP:$DATA_DIR/"
        remote "test -f $DATA_DIR/full.parquet && test -f $DATA_DIR/$DATASET_PARQUET" \
            || die "dataset seed failed (needs both full.parquet and $DATASET_PARQUET for DATASET=$DATASET)"
        info "datasets seeded"
    else
        die "no local /tmp/contextbench/data to seed from — fetch the ContextBench data/ dir (incl. full.parquet + $DATASET_PARQUET) onto the box manually (see README troubleshooting)"
    fi
fi

info "bootstrap complete — next: ./03-run.sh"
