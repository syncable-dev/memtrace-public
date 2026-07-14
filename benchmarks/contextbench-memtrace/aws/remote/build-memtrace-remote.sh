#!/usr/bin/env bash
# build-memtrace-remote.sh — runs ON THE BOX. Source-mode install: cargo-build
# the memtrace release binary from the rsynced private source tree, verify it,
# and install it at /srv/contextbench/memtrace-bin (run-remote.sh puts that
# dir first on PATH). Invoked by 02-bootstrap.sh AFTER bootstrap-remote.sh
# (data volume mounted, apt base + build-essential present) as:
#   bash ~/contextbench-adapter/aws/remote/build-memtrace-remote.sh SRC_DIR=/srv/contextbench/memtrace-src
#
# Provenance: 02-bootstrap.sh captures git HEAD/describe/dirty state LOCALLY
# (the .git objects are never shipped) into SRC_DIR/source-manifest.json; this
# script re-publishes it to BIN_DIR/source-manifest.json enriched with build
# facts (rustc, binary sha256, built_at). run-remote.sh copies that into each
# run's run_meta.json.
#
# Idempotent: if BIN_DIR already holds a working binary built from the exact
# same verified source payload, the build is skipped.

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

for kv in "$@"; do
    case "$kv" in *=*) export "${kv%%=*}"="${kv#*=}" ;; esac
done
: "${SRC_DIR:=/srv/contextbench/memtrace-src}"
: "${BIN_DIR:=/srv/contextbench/memtrace-bin}"
: "${SMOKE_TIMEOUT:=2400}"   # first smoke index also downloads the ~640MB embed model

DATA_ROOT=/srv/contextbench
log() { printf '\n[build-memtrace] %s\n' "$*"; }

[ -d "$SRC_DIR" ] || { echo "ERROR: $SRC_DIR missing (02-bootstrap.sh rsyncs it in source mode)" >&2; exit 1; }
MANIFEST="$SRC_DIR/source-manifest.json"
[ -s "$MANIFEST" ] || { echo "ERROR: $MANIFEST missing — 02-bootstrap.sh must capture provenance before the rsync" >&2; exit 1; }
[ -f "$SRC_DIR/Cargo.toml" ] || { echo "ERROR: $SRC_DIR has no Cargo.toml — wrong MEMTRACE_SOURCE_DIR?" >&2; exit 1; }

manifest_field() { # file field -> value ('' if null/missing)
    python3 - "$1" "$2" <<'PY'
import json, sys
try:
    value = json.load(open(sys.argv[1])).get(sys.argv[2])
except Exception:
    value = None
print("" if value is None else value)
PY
}
SRC_SHA="$(manifest_field "$MANIFEST" head_sha)"
SRC_DIFF_SHA="$(manifest_field "$MANIFEST" dirty_diff_sha256_16)"
SRC_PAYLOAD_SHA="$(manifest_field "$MANIFEST" source_payload_sha256)"
[ -n "$SRC_SHA" ] || { echo "ERROR: head_sha missing from $MANIFEST" >&2; exit 1; }
[ -n "$SRC_PAYLOAD_SHA" ] || { echo "ERROR: source_payload_sha256 missing from $MANIFEST" >&2; exit 1; }

# --- skip if BIN_DIR already matches this exact source state ---------------------
# The payload hash is over per-file SHA-256 records for tracked submodule files
# plus any non-ignored untracked files, so it covers the exact rsynced source.
if [ -x "$BIN_DIR/memtrace" ] && [ -s "$BIN_DIR/source-manifest.json" ]; then
    HAVE_PAYLOAD_SHA="$(manifest_field "$BIN_DIR/source-manifest.json" source_payload_sha256)"
    if [ "$HAVE_PAYLOAD_SHA" = "$SRC_PAYLOAD_SHA" ] \
            && "$BIN_DIR/memtrace" --version >/dev/null 2>&1; then
        log "binary already built from payload=$SRC_PAYLOAD_SHA (head=$SRC_SHA) — skipping build"
        "$BIN_DIR/memtrace" --version
        exit 0
    fi
fi

# --- build deps -------------------------------------------------------------------
log "build deps (apt + rustup)"
APT="sudo apt-get -o DPkg::Lock::Timeout=300"
$APT install -y -qq pkg-config libssl-dev cmake clang protobuf-compiler >/dev/null

if [ ! -x "$HOME/.cargo/bin/cargo" ]; then
    curl -fsSL --retry 3 https://sh.rustup.rs \
        | sh -s -- -y --profile minimal --default-toolchain stable --no-modify-path
fi
export PATH="$HOME/.cargo/bin:$PATH"
rustc --version
cargo --version

# Node on PATH for build.rs: memtrace-mcp's build script embeds the web UI
# (npm install + npm run build -> ui/dist) at compile time; when npm is
# missing it only WARNS, and the rust-embed UiAssets derive then fails the
# build with E0599 (no ui/dist to embed). nvm-installed node is not on PATH
# in this non-interactive script, so source nvm exactly like run-remote.sh.
export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
    set +eu
    # shellcheck disable=SC1091
    . "$NVM_DIR/nvm.sh"
    nvm use 22 >/dev/null 2>&1 || nvm use default >/dev/null 2>&1
    set -eu
fi
command -v npm >/dev/null \
    || { echo "ERROR: npm not on PATH even after sourcing nvm — build.rs cannot embed the UI (bootstrap-remote.sh installs node first; run 02-bootstrap.sh)" >&2; exit 1; }
echo "node $(node --version), npm $(npm --version) (for build.rs UI embed)"

# --- build ------------------------------------------------------------------------
# target/ lives inside SRC_DIR on the data volume (excluded from the rsync,
# and rsync --delete never removes excluded paths), so rebuilds after a
# source re-sync are incremental.
log "cargo build --release (head=$SRC_SHA dirty_diff=${SRC_DIFF_SHA:-clean}, $(nproc) cores)"
( cd "$SRC_DIR" && cargo build --release )
[ -x "$SRC_DIR/target/release/memtrace" ] \
    || { echo "ERROR: build finished but target/release/memtrace is missing" >&2; exit 1; }

# --- install: memtrace + any sidecar executables ------------------------------------
# The npm package ships sidecars (memcore-server, ...) next to the main
# binary; mirror that by copying every top-level release executable.
log "installing binaries -> $BIN_DIR"
mkdir -p "$BIN_DIR"
INSTALLED=()
while IFS= read -r bin; do
    name="$(basename "$bin")"
    cp -f "$bin" "$BIN_DIR/$name.tmp"
    chmod 755 "$BIN_DIR/$name.tmp"
    mv -f "$BIN_DIR/$name.tmp" "$BIN_DIR/$name"
    INSTALLED+=("$name")
done < <(find "$SRC_DIR/target/release" -maxdepth 1 -type f -perm -u+x \
             ! -name '*.d' ! -name '*.so' ! -name '*.rlib' ! -name '.*')
[ "${#INSTALLED[@]}" -gt 0 ] || { echo "ERROR: no executables found in target/release" >&2; exit 1; }
echo "installed: ${INSTALLED[*]}"

export PATH="$BIN_DIR:$PATH"
BUILT_VERSION="$("$BIN_DIR/memtrace" --version 2>&1 | sed $'s/\\033\\[[0-9;]*m//g' | grep -m1 -E '^memtrace [0-9]+[.][0-9]+[.][0-9]+')"
[ -n "$BUILT_VERSION" ] || { echo "ERROR: could not parse memtrace semantic version" >&2; exit 1; }
echo "memtrace --version: $BUILT_VERSION"

# --- ONNX Runtime dylib (embed engine) ----------------------------------------------
# On linux-x64 the binary loads ONNX Runtime DYNAMICALLY: at startup it looks
# for libonnxruntime.so NEXT TO the executable and sets ORT_DYLIB_PATH
# (main.rs bundled_ort_dylib_path/bridge_ort_dylib_env). cargo build does NOT
# produce that .so — the npm platform package ships it. Without it every
# index "succeeds" but the embed lane dies at runtime ("Failed to load ONNX
# Runtime dylib: dlopen failed") and embedding.written=0 for the whole run
# (found on the 2026-07-10 shakedown). Fetch the exact lib the npm release
# ships (best parity with production) and drop it beside the built binary.
if [ ! -s "$BIN_DIR/libonnxruntime.so" ]; then
    log "fetching libonnxruntime.so from @memtrace/linux-x64 (npm platform package)"
    command -v npm >/dev/null \
        || { echo "ERROR: npm not on PATH — cannot fetch @memtrace/linux-x64 for libonnxruntime.so" >&2; exit 1; }
    ORT_TMP="$(mktemp -d)"
    ( cd "$ORT_TMP" \
        && npm pack "@memtrace/linux-x64" --silent >/dev/null \
        && tar xzf memtrace-linux-x64-*.tgz package/bin/libonnxruntime.so )
    [ -s "$ORT_TMP/package/bin/libonnxruntime.so" ] \
        || { echo "ERROR: could not extract libonnxruntime.so from @memtrace/linux-x64" >&2; rm -rf "$ORT_TMP"; exit 1; }
    mv "$ORT_TMP/package/bin/libonnxruntime.so" "$BIN_DIR/libonnxruntime.so"
    chmod 644 "$BIN_DIR/libonnxruntime.so"
    rm -rf "$ORT_TMP"
fi
echo "libonnxruntime.so: $(stat -c '%s bytes' "$BIN_DIR/libonnxruntime.so")"

# --- smoke test: index a toy repo in a fully isolated env ---------------------------
# Doubles as the embedding-model pre-warm (bootstrap-remote.sh defers it in
# source mode) so the parallel fan-out never cold-starts the model download.
log "smoke index (isolated env; first run downloads the ~640MB embed model)"
SMOKE="$DATA_ROOT/build-smoke"
rm -rf "$SMOKE"
mkdir -p "$SMOKE/repo"
(
    cd "$SMOKE/repo"
    git init -q
    git config user.email bench@localhost
    git config user.name bench
    # The fixture must be NON-TRIVIAL: the embed stage skips trivial symbols
    # (a one-line `def smoke(): return 1` yields candidates=1,
    # total_post_skip=0, written=0 — indistinguishable from a broken embed
    # lane). Several real functions with docstrings guarantee written>0 on a
    # healthy box.
    cat > smoke.py <<'PYSRC'
import json
from pathlib import Path

def load_config(path):
    """Load a JSON config file and validate required keys."""
    data = json.loads(Path(path).read_text())
    for key in ("name", "version", "entries"):
        if key not in data:
            raise KeyError(f"missing required config key: {key}")
    return data

def summarize_entries(entries):
    """Aggregate entry counts by category with a stable ordering."""
    counts = {}
    for entry in entries:
        category = entry.get("category", "unknown")
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items()))

class ConfigStore:
    """In-memory store for validated configs, keyed by name."""
    def __init__(self):
        self._configs = {}

    def add(self, config):
        self._configs[config["name"]] = config

    def lookup(self, name):
        if name not in self._configs:
            raise LookupError(f"no config named {name}")
        return self._configs[name]
PYSRC
    git add -A
    git commit -qm smoke
)
# The smoke MUST exercise the exact lane the benchmark uses: runner.py talks
# to `memtrace mcp` (stdio JSON-RPC) and calls the index_directory tool with
# skip_embed:false, which embeds INLINE and reports truthful embed stats.
# The `memtrace index` CLI is the WRONG lane here — it defers embedding to
# the daemon's embed worker and exits without ever touching the model, so a
# CLI-based smoke passes/fails on nothing (found on the 2026-07-10 shakedown).
# MEMTRACE_DEV=1: the box has no license (no ~/.config/memtrace/credentials.json,
# no MEMTRACE_LICENSE_KEY in .env) and the embed lane is auth-gated. Dev
# bypass is the documented headless path for benchmarking your own build on
# your own box; run-remote.sh sets the same for the benchmark itself.
env CI=1 MEMTRACE_HEADLESS=1 MEMTRACE_DEV=1 MEMTRACE_MEMDB_MODE=embedded \
    MEMTRACE_MEMDB_LOOPBACK_PORT=0 \
    MEMTRACE_MEMDB_DATA_DIR="$SMOKE/memdb" MEMTRACE_DATA_DIR="$SMOKE/state" \
    MEMTRACE_NO_RTK_INIT=1 MEMTRACE_NO_RTK_PROMPT=1 MEMTRACE_TELEMETRY=off \
    MEMTRACE_CORTEX=off MEMCORTEX_STORE_DIR="$SMOKE/cortex-store" \
    PATH="$BIN_DIR:$PATH" \
    timeout "$SMOKE_TIMEOUT" python3 - "$SMOKE/repo" <<'PY' \
    || { echo "ERROR: MCP smoke index with the built binary failed" >&2; exit 1; }
import json, subprocess, sys
repo = sys.argv[1]
proc = subprocess.Popen(
    ["memtrace", "mcp"], cwd=repo,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, text=True, bufsize=1,
)
def rpc(payload):
    proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    proc.stdin.flush()
    if "id" not in payload:
        return None
    line = proc.stdout.readline()
    if not line:
        raise SystemExit("memtrace mcp exited before replying")
    resp = json.loads(line)
    if "error" in resp:
        raise SystemExit(f"MCP error: {resp['error']}")
    return resp
rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {},
    "clientInfo": {"name": "contextbench-smoke", "version": "0.1"}}})
rpc({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
resp = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
    "name": "index_directory",
    "arguments": {"path": repo, "clear_existing": True,
                  "defer_replay": True, "skip_embed": False}}})
result = resp.get("result", {})
text = "\n".join(c.get("text", "") for c in result.get("content", [])
                 if c.get("type") == "text")
if result.get("isError"):
    raise SystemExit(f"index_directory failed: {text[:2000]}")
stats = json.loads(text) if text else {}
embed = stats.get("embedding") or {}
written = embed.get("written", stats.get("embeddings_created", 0))
print(f"smoke index_directory: embedding stats = {embed or stats}")
if not isinstance(written, (int, float)) or written <= 0:
    raise SystemExit(f"index completed WITHOUT embeddings (written={written!r}) — "
                     "the semantic lane is broken on this box")
proc.terminate()
PY
CACHE_SNAP="$HOME/.memtrace/fastembed_cache/models--jinaai--jina-embeddings-v2-base-code/snapshots"
if [ ! -d "$CACHE_SNAP" ] || [ -z "$(find "$CACHE_SNAP" -mindepth 1 -print -quit 2>/dev/null)" ]; then
    echo "ERROR: smoke index ran but the embedding-model cache $CACHE_SNAP is missing/empty" >&2
    exit 1
fi
echo "smoke index OK (embedding.written > 0), embedding model cache warm"

# --- publish provenance ---------------------------------------------------------------
log "writing $BIN_DIR/source-manifest.json"
BIN_SHA256="$(sha256sum "$BIN_DIR/memtrace" | cut -d' ' -f1)"
python3 - "$MANIFEST" "$BIN_DIR/source-manifest.json" \
    "$BUILT_VERSION" "$(rustc --version)" "$BIN_SHA256" "${INSTALLED[*]}" <<'PY'
import json, sys, time
src, dst, version, rustc, sha, installed = sys.argv[1:7]
manifest = json.load(open(src))
manifest.update({
    "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "memtrace_version_output": version,
    "rustc": rustc,
    "binary_sha256": sha,
    "installed_binaries": installed.split(),
})
with open(dst, "w") as fh:
    json.dump(manifest, fh, indent=2)
    fh.write("\n")
PY

log "OK — binary at $BIN_DIR/memtrace ($BUILT_VERSION)"
