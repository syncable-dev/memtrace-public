#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=monitor-every-10.sh
# shellcheck disable=SC1091 # resolved from this test's absolute directory
. "$SCRIPT_DIR/monitor-every-10.sh"

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/contextbench-monitor-test.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT
RUN_ID="run-monitor-test-verified"
ARCHIVE="$TMP_ROOT/valid.tar"
STAGE="$TMP_ROOT/stage"
MIRROR="$TMP_ROOT/mirror"
SNAPSHOTS="$TMP_ROOT/snapshots"

python3 - "$ARCHIVE" "$RUN_ID" <<'PY'
import hashlib
import io
import json
from pathlib import Path
import tarfile
import sys

archive = Path(sys.argv[1])
run_id = sys.argv[2]
ids = [f"Task__{index:03d}" for index in range(100)]
slug = ids[0]
record_mtime = 1_784_000_000_123_456_789
base_mtime = 1_784_000_000_000_000_000
contents = {
    "manifest.json": (json.dumps(ids) + "\n").encode(),
    "run_meta.json": b'{"run_id":"run-monitor-test-verified"}\n',
    "run_provenance.json": b'{"fingerprint":"test"}\n',
    f"runs/{slug}/run_record.json": (
        json.dumps({"instance_id": ids[0], "status": "success"}) + "\n"
    ).encode(),
    f"runs/{slug}/prediction.jsonl": (
        json.dumps({"instance_id": ids[0], "predicted_context": {}}) + "\n"
    ).encode(),
    f"runs/{slug}/prediction-audit/{slug}.json": b'{}\n',
    f"runs/{slug}/query-plan.json": b'{}\n',
}

files = []
for offset, (name, data) in enumerate(sorted(contents.items())):
    mtime_ns = record_mtime if name.endswith("/run_record.json") else base_mtime + offset
    files.append({
        "path": name,
        "size": len(data),
        "mtime_ns": mtime_ns,
        "sha256": hashlib.sha256(data).hexdigest(),
    })
payload = {
    "schema_version": 1,
    "transport": "gnu-tar-pax",
    "run_id": run_id,
    "terminal_tasks": 1,
    "terminals": [{
        "instance_id": ids[0],
        "slug": slug,
        "status": "success",
        "manifest_index": 0,
        "run_record_mtime_ns": record_mtime,
    }],
    "files": files,
}
index = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()

def add_bytes(target, name, data, mtime_ns):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    seconds, nanos = divmod(mtime_ns, 1_000_000_000)
    info.mtime = seconds
    info.pax_headers = {"mtime": f"{seconds}.{nanos:09d}"}
    target.addfile(info, io.BytesIO(data))

with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as target:
    by_name = {item["path"]: item for item in files}
    for name, data in sorted(contents.items()):
        add_bytes(target, name, data, by_name[name]["mtime_ns"])
    add_bytes(target, "__monitor_snapshot_index__.json", index, base_mtime)
PY

validate_and_extract_snapshot "$ARCHIVE" "$STAGE" "$RUN_ID" >/dev/null
COUNT="$(merge_snapshot "$STAGE" "$MIRROR" "$SNAPSHOTS")"
[ "$COUNT" = "1" ] || { echo "unexpected terminal count: $COUNT" >&2; exit 1; }
merge_snapshot "$STAGE" "$MIRROR" "$SNAPSHOTS" >/dev/null

python3 - "$MIRROR/runs/Task__000/run_record.json" <<'PY'
from pathlib import Path
import sys

expected = 1_784_000_000_123_456_789
actual = Path(sys.argv[1]).stat().st_mtime_ns
if actual != expected:
    raise SystemExit(f"nanosecond mtime mismatch: expected={expected} actual={actual}")
PY

python3 - "$ARCHIVE" "$TMP_ROOT/traversal.tar" <<'PY'
import io
from pathlib import Path
import tarfile
import sys

source, output = map(Path, sys.argv[1:])
with tarfile.open(source, "r") as original, tarfile.open(
    output, "w", format=tarfile.PAX_FORMAT
) as malicious:
    for member in original.getmembers():
        data = original.extractfile(member)
        malicious.addfile(member, data)
    extra = b"escape\n"
    member = tarfile.TarInfo("../escape")
    member.size = len(extra)
    malicious.addfile(member, io.BytesIO(extra))
PY

if validate_and_extract_snapshot \
    "$TMP_ROOT/traversal.tar" "$TMP_ROOT/traversal-stage" "$RUN_ID" \
    >/dev/null 2>&1; then
    echo "path traversal archive was accepted" >&2
    exit 1
fi

if (validate_run_id "../bad") >/dev/null 2>&1; then
    echo "unsafe run ID was accepted" >&2
    exit 1
fi
if (validate_run_id "run-LATEST") >/dev/null 2>&1; then
    echo "LATEST run ID was accepted" >&2
    exit 1
fi

# shellcheck disable=SC2034 # consumed by the sourced lock helper
MONITOR_ROOT="$TMP_ROOT/lock-root"
LOCK_HELD=0
acquire_monitor_lock
if (
    # shellcheck disable=SC2034 # consumed by the sourced lock helper
    LOCK_HELD=0
    acquire_monitor_lock
) >/dev/null 2>&1; then
    echo "duplicate monitor lock was accepted" >&2
    exit 1
fi
release_monitor_lock

TREATMENT="offline-packing-v2"
cat >"$TMP_ROOT/valid-seal.json" <<'JSON'
{
  "schema_version": 1,
  "mode": "seal-batch",
  "completed_count": 10,
  "candidate_treatment": {"name": "offline-packing-v2"}
}
JSON
validate_output "$TMP_ROOT/valid-seal.json" seal-batch 10 ""

cat >"$TMP_ROOT/wrong-seal-treatment.json" <<'JSON'
{
  "schema_version": 1,
  "mode": "seal-batch",
  "completed_count": 10,
  "candidate_treatment": {"name": "different-treatment"}
}
JSON
if validate_output "$TMP_ROOT/wrong-seal-treatment.json" seal-batch 10 "" \
    >/dev/null 2>&1; then
    echo "seal output with a different treatment was accepted" >&2
    exit 1
fi

printf 'monitor-every-10 tests passed\n'
