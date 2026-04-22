#!/usr/bin/env bash
# Bench #0 bootstrap — install the four adapters into benchmarks/.venv
# and sanity-check the target corpus. Does NOT pull docker services
# (that's `memtrace start` or `docker compose up -d` in the main README).
#
# Env vars honored by the adapters + corpora at runtime:
#   MEMPALACE_PATH   — path to a mempalace checkout (Bench #0/#1/#3-mempalace)
#   DJANGO_PATH      — path to a Django checkout    (Bench #2, Bench #3-django)
#   MEMTRACE_BIN     — memtrace binary (else `which memtrace`)
#   CGC_BIN          — cgc binary       (else `which cgc`)
#
# Bootstrap only checks MEMPALACE_PATH because that's the baseline corpus for
# the 1000-query Bench #0/#1 parity run. Set the others if you're running
# Bench #2 or #3 on Django.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
VENV="$REPO/benchmarks/.venv"

if [ ! -d "$VENV" ]; then
  echo "Creating venv at $VENV"
  python3 -m venv "$VENV"
fi

PIP="$VENV/bin/pip"

echo "→ Installing Python deps"
"$PIP" install --upgrade pip
"$PIP" install \
  "chromadb==1.5.3" \
  "sentence-transformers" \
  "codegraphcontext" \
  "neo4j" \
  "pytest" \
  "pytest-xdist"

echo "→ Checking Memtrace binary"
MT_BIN="${MEMTRACE_BIN:-$(command -v memtrace || true)}"
if [ -z "${MT_BIN}" ] && [ -x "$REPO/target/release/memtrace" ]; then
  MT_BIN="$REPO/target/release/memtrace"
fi
if [ -z "${MT_BIN}" ] || [ ! -x "${MT_BIN}" ]; then
  echo "  memtrace binary not found."
  echo "    install:  npm install -g memtrace"
  echo "    or build: cargo build --release   (and set MEMTRACE_BIN)"
  exit 1
fi
echo "  found: $MT_BIN"

echo "→ Checking mempalace checkout"
MP="${MEMPALACE_PATH:-/Users/alexthh/Desktop/ZeroToDemo/mempalace}"
if [ ! -d "$MP" ]; then
  echo "  mempalace corpus not found at $MP"
  echo "    export MEMPALACE_PATH=/path/to/mempalace"
  exit 1
fi
echo "  found: $MP"

echo "→ Optional: start GitNexus eval-server (for gitnexus adapter)"
echo "    cd $MP && npx gitnexus eval-server &"
echo ""
echo "✓ bootstrap complete"
