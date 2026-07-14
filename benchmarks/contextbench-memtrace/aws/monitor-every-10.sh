#!/usr/bin/env bash
# Poll one explicit remote ContextBench run, freeze every exact batch of ten,
# and evaluate/compare it against one explicit historical control artifact.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPARE_RUNS="$ADAPTER_DIR/compare_runs.py"
TREATMENT="${TREATMENT:-offline-packing-v2}"
EXPECTED_TOTAL="${EXPECTED_TOTAL:-100}"
SNAPSHOT_ONLY="${SNAPSHOT_ONLY:-0}"
SNAPSHOT_INDEX="__monitor_snapshot_index__.json"

monitor_info() { printf '[monitor] %s\n' "$*" >&2; }
monitor_warn() { printf '[monitor:warn] %s\n' "$*" >&2; }
monitor_fail() { printf '[monitor:fail] %s\n' "$*" >&2; exit 2; }

usage() {
    cat <<'EOF'
Usage:
  monitor-every-10.sh \
    --run-id RUN_ID \
    --control /absolute/old-control-artifact \
    --checkpoint-root /absolute/checkpoint-root \
    --runtime-manifest /absolute/runtime-manifest.json \
    --evaluator /absolute/contextbench/evaluate.py \
    --cache-dir /absolute/evaluator-cache \
    [--complete-candidate-artifact /absolute/collected-run] \
    [--poll-seconds 30] [--once]

The AWS target and SSH key come from aws/config.env plus aws/state/state.json.
The run ID is always explicit; this script never follows results/LATEST.
EOF
}

validate_run_id() {
    local value="${1:-}"
    [[ "$value" =~ ^run-[A-Za-z0-9][A-Za-z0-9._-]{0,122}$ ]] \
        || monitor_fail "invalid run ID (expected one safe run-* path segment): $value"
    [[ "$value" != *..* ]] || monitor_fail "run ID may not contain '..': $value"
    case "$value" in
        *[Ll][Aa][Tt][Ee][Ss][Tt]*) monitor_fail "run ID may not contain LATEST: $value" ;;
    esac
}

canonical_path() { # path kind(file|dir|dir-maybe)
    python3 - "$1" "$2" <<'PY'
from pathlib import Path
import sys

raw, kind = sys.argv[1:]
path = Path(raw).expanduser()
if not path.is_absolute():
    raise SystemExit(f"path must be absolute: {raw}")
if any(part.casefold() == "latest" for part in path.parts):
    raise SystemExit(f"path may not contain a LATEST component: {raw}")
if kind == "file":
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise SystemExit(f"not a file: {resolved}")
elif kind == "dir":
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise SystemExit(f"not a directory: {resolved}")
elif kind == "dir-maybe":
    resolved = path.resolve(strict=False)
    if resolved.exists() and not resolved.is_dir():
        raise SystemExit(f"not a directory: {resolved}")
else:
    raise SystemExit(f"unknown path kind: {kind}")
print(resolved)
PY
}

acquire_monitor_lock() {
    LOCK_DIR="$MONITOR_ROOT/.lock"
    mkdir -p "$MONITOR_ROOT"
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        monitor_fail "another monitor (or an uncleared stale lock) owns $LOCK_DIR"
    fi
    printf 'pid=%s\nhost=%s\nrun_id=%s\n' "$$" "$(hostname)" "$RUN_ID" > "$LOCK_DIR/owner"
    LOCK_HELD=1
}

release_monitor_lock() {
    if [ "${LOCK_HELD:-0}" = "1" ]; then
        rm -f "$LOCK_DIR/owner"
        rmdir "$LOCK_DIR" 2>/dev/null || true
        LOCK_HELD=0
    fi
}

write_or_verify_binding() {
    python3 - \
        "$MONITOR_ROOT/monitor-binding.json" "$RUN_ID" "$SSH_TARGET" \
        "$CONTROL" "$CHECKPOINT_ROOT" "$RUNTIME_MANIFEST" "$EVALUATOR" \
        "$CACHE_DIR" "$COMPARE_RUNS" "$TREATMENT" "$EXPECTED_TOTAL" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys

(output, run_id, ssh_target, control, checkpoint_root, runtime_manifest,
 evaluator, cache_dir, comparator, treatment, expected_total) = sys.argv[1:]

def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

payload = {
    "schema_version": 1,
    "mode": "contextbench-every-10-monitor-binding",
    "run_id": run_id,
    "ssh_target": ssh_target,
    "control": control,
    "checkpoint_root": checkpoint_root,
    "runtime_manifest": runtime_manifest,
    "evaluator": evaluator,
    "cache_dir": cache_dir,
    "comparator": comparator,
    "candidate_treatment": treatment,
    "expected_total": int(expected_total),
    "identities": {
        "control_manifest_sha256": digest(str(Path(control) / "manifest.json")),
        "runtime_manifest_sha256": digest(runtime_manifest),
        "evaluator_sha256": digest(evaluator),
        "comparator_sha256": digest(comparator),
    },
}
encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
path = Path(output)
if path.exists():
    if path.read_bytes() != encoded:
        raise SystemExit(f"monitor binding changed: {path}")
else:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as target:
        target.write(encoded)
        target.flush()
        os.fsync(target.fileno())
PY
}

collect_remote_snapshot() {
    local archive="$1"
    # shellcheck disable=SC2029 # safe run ID is intentionally passed to remote bash
    if ! "$SSH_BIN" "${SSH_OPTS[@]}" "$SSH_TARGET" \
        "bash -s -- $(printf '%q' "$RUN_ID") $(printf '%q' "$EXPECTED_TOTAL")" > "$archive" <<'REMOTE'
set -euo pipefail

run_id="${1:?run ID required}"
expected_total="${2:?expected task count required}"
case "$run_id" in
    run-[A-Za-z0-9]*) ;;
    *) echo "invalid remote run ID" >&2; exit 64 ;;
esac
case "$run_id" in
    *[!A-Za-z0-9._-]*|*..*|*[Ll][Aa][Tt][Ee][Ss][Tt]*)
        echo "unsafe remote run ID" >&2
        exit 64
        ;;
esac

tar --version 2>/dev/null | head -1 | grep -q 'GNU tar' \
    || { echo "remote GNU tar is required" >&2; exit 69; }

base="$(realpath -e /srv/contextbench/results)"
candidate="$base/$run_id"
[ ! -L "$candidate" ] || { echo "remote run path is a symlink" >&2; exit 65; }
root="$(realpath -e "$candidate")"
[ "$root" = "$base/$run_id" ] \
    || { echo "remote run path escaped results root" >&2; exit 65; }

tmp="$(mktemp -d /tmp/contextbench-monitor.XXXXXX)"
trap 'rm -rf "$tmp"' EXIT
list="$tmp/files.list"
index="$tmp/__monitor_snapshot_index__.json"

python3 - "$root" "$run_id" "$list" "$index" "$expected_total" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

root = Path(sys.argv[1]).resolve(strict=True)
run_id, list_path, index_path, expected_total_raw = sys.argv[2:]
expected_total = int(expected_total_raw)
safe_segment = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")

def ensure_no_symlink_parents(path):
    cursor = path.parent
    while cursor != root:
        if cursor.is_symlink():
            raise SystemExit(f"symlink parent rejected: {cursor}")
        cursor = cursor.parent

def stable_identity(path):
    ensure_no_symlink_parents(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise SystemExit(f"non-regular snapshot member rejected: {path}")
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError:
        raise SystemExit(f"snapshot member escaped run root: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.lstat()
    stable = (before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_ino, after.st_size, after.st_mtime_ns
    )
    if not stable:
        raise SystemExit(f"snapshot member changed during capture: {path}")
    return {
        "path": relative,
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }

required_top = ["manifest.json", "run_meta.json", "run_provenance.json"]
for name in required_top:
    if not (root / name).is_file():
        raise SystemExit(f"required run artifact missing: {name}")

manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
if not isinstance(manifest, list) or len(manifest) != expected_total:
    raise SystemExit(f"remote manifest must contain exactly {expected_total} tasks")
if any(not isinstance(item, str) or not item for item in manifest):
    raise SystemExit("remote manifest contains an invalid instance ID")
if len(set(manifest)) != len(manifest):
    raise SystemExit("remote manifest contains duplicate instance IDs")

def slugify(instance_id):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", instance_id)

slugs = [slugify(instance_id) for instance_id in manifest]
if len(set(slugs)) != len(slugs):
    raise SystemExit("manifest instance IDs collide after slugification")
if any(not safe_segment.fullmatch(slug) or slug in {".", ".."} for slug in slugs):
    raise SystemExit("manifest produced an unsafe task path segment")

runs = root / "runs"
if runs.exists() and (runs.is_symlink() or not runs.is_dir()):
    raise SystemExit("runs is not a safe directory")

selected = [root / name for name in required_top]
terminals = []
if runs.is_dir():
    expected = set(slugs)
    for child in runs.iterdir():
        if child.is_symlink():
            raise SystemExit(f"symlink task directory rejected: {child.name}")
        if child.name not in expected and (child / "run_record.json").exists():
            raise SystemExit(f"terminal record under unexpected task segment: {child.name}")
    for manifest_index, (instance_id, slug) in enumerate(zip(manifest, slugs)):
        run_dir = runs / slug
        record_path = run_dir / "run_record.json"
        if not record_path.exists():
            continue
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise SystemExit(f"unsafe task directory: {slug}")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("instance_id") != instance_id:
            raise SystemExit(f"run record identity mismatch: {slug}")
        status_value = record.get("status")
        if status_value not in {"success", "failure"}:
            raise SystemExit(f"non-terminal run record encountered: {slug}")
        audit_dir = run_dir / "prediction-audit"
        if audit_dir.is_symlink() or not audit_dir.is_dir():
            raise SystemExit(f"unsafe prediction-audit directory: {slug}")
        task_paths = [
            record_path,
            run_dir / "prediction.jsonl",
            audit_dir / f"{slug}.json",
        ]
        query_plan = run_dir / "query-plan.json"
        failure = run_dir / "failure.json"
        if status_value == "success":
            task_paths.append(query_plan)
        elif not failure.is_file():
            raise SystemExit(f"terminal failure has no failure.json: {slug}")
        if query_plan.is_file() and query_plan not in task_paths:
            task_paths.append(query_plan)
        if failure.is_file():
            task_paths.append(failure)
        for path in task_paths:
            if not path.exists():
                raise SystemExit(f"terminal artifact missing: {path.relative_to(root)}")
        selected.extend(task_paths)
        terminals.append({
            "instance_id": instance_id,
            "slug": slug,
            "status": status_value,
            "manifest_index": manifest_index,
            "run_record_mtime_ns": record_path.lstat().st_mtime_ns,
        })

identities = []
seen = set()
for path in selected:
    identity = stable_identity(path)
    if identity["path"] in seen:
        continue
    seen.add(identity["path"])
    identities.append(identity)
identities.sort(key=lambda item: item["path"])

mtime_by_path = {item["path"]: item["mtime_ns"] for item in identities}
for terminal in terminals:
    record_relative = f"runs/{terminal['slug']}/run_record.json"
    terminal["run_record_mtime_ns"] = mtime_by_path[record_relative]
terminals.sort(key=lambda item: (item["run_record_mtime_ns"], item["manifest_index"]))

payload = {
    "schema_version": 1,
    "transport": "gnu-tar-pax",
    "run_id": run_id,
    "terminal_tasks": len(terminals),
    "terminals": terminals,
    "files": identities,
}
Path(index_path).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
with open(list_path, "wb") as target:
    for identity in identities:
        target.write(identity["path"].encode("utf-8") + b"\0")
PY

tar --format=pax --create --file=- --no-recursion \
    --verbatim-files-from --null -C "$root" -T "$list" \
    -C "$tmp" "__monitor_snapshot_index__.json"
REMOTE
    then
        monitor_fail "SSH snapshot capture failed for $RUN_ID; remote run was left untouched"
    fi
    [ -s "$archive" ] || monitor_fail "SSH snapshot capture returned an empty archive"
}

validate_and_extract_snapshot() { # archive stage run-id
    local archive="$1" stage="$2" run_id="$3"
    mkdir -p "$stage"
    if ! python3 - "$archive" "$run_id" "$SNAPSHOT_INDEX" "$EXPECTED_TOTAL" <<'PY'
import json
from pathlib import PurePosixPath
import re
import sys
import tarfile

archive, expected_run_id, index_name, expected_total_raw = sys.argv[1:]
expected_total = int(expected_total_raw)
safe_segment = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")

def safe_relative(name):
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return False
    if name in {"manifest.json", "run_meta.json", "run_provenance.json"}:
        return True
    parts = path.parts
    if len(parts) == 3 and parts[0] == "runs":
        return bool(safe_segment.fullmatch(parts[1])) and parts[2] in {
            "run_record.json", "prediction.jsonl", "query-plan.json", "failure.json"
        }
    if len(parts) == 4 and parts[0] == "runs" and parts[2] == "prediction-audit":
        return bool(safe_segment.fullmatch(parts[1])) and parts[3] == f"{parts[1]}.json"
    return False

with tarfile.open(archive, "r:*") as source:
    members = source.getmembers()
    names = [member.name for member in members]
    if len(names) != len(set(names)):
        raise SystemExit("snapshot tar contains duplicate member names")
    index_members = [member for member in members if member.name == index_name]
    if len(index_members) != 1 or not index_members[0].isfile():
        raise SystemExit("snapshot tar has no unique regular index")
    index_source = source.extractfile(index_members[0])
    if index_source is None:
        raise SystemExit("snapshot index is unreadable")
    raw_index = index_source.read()
    payload = json.loads(raw_index)
    if payload.get("schema_version") != 1 or payload.get("transport") != "gnu-tar-pax":
        raise SystemExit("snapshot index has an invalid transport contract")
    if payload.get("run_id") != expected_run_id:
        raise SystemExit("snapshot run ID differs from the explicit monitor run ID")
    files = payload.get("files")
    terminals = payload.get("terminals")
    if not isinstance(files, list) or not isinstance(terminals, list):
        raise SystemExit("snapshot index files/terminals are malformed")
    identities = {}
    for item in files:
        if not isinstance(item, dict) or not safe_relative(item.get("path", "")):
            raise SystemExit(f"unsafe indexed snapshot path: {item!r}")
        path = item["path"]
        if path in identities:
            raise SystemExit(f"duplicate indexed snapshot path: {path}")
        if not isinstance(item.get("size"), int) or item["size"] < 0:
            raise SystemExit(f"invalid indexed size: {path}")
        if not isinstance(item.get("mtime_ns"), int) or item["mtime_ns"] < 0:
            raise SystemExit(f"invalid indexed mtime: {path}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))):
            raise SystemExit(f"invalid indexed hash: {path}")
        identities[path] = item
    for required in ("manifest.json", "run_meta.json", "run_provenance.json"):
        if required not in identities:
            raise SystemExit(f"snapshot index omits {required}")
    expected_names = set(identities) | {index_name}
    if set(names) != expected_names:
        raise SystemExit("snapshot tar members differ from the validated index")
    for member in members:
        if not member.isfile():
            raise SystemExit(f"non-regular tar member rejected: {member.name}")
        if member.name in identities and member.size != identities[member.name]["size"]:
            raise SystemExit(f"tar size differs from index: {member.name}")
    if payload.get("terminal_tasks") != len(terminals) or len(terminals) > expected_total:
        raise SystemExit("snapshot terminal count is invalid")
    terminal_ids = set()
    terminal_slugs = set()
    for terminal in terminals:
        if not isinstance(terminal, dict):
            raise SystemExit("snapshot terminal entry is malformed")
        instance_id, slug = terminal.get("instance_id"), terminal.get("slug")
        if not isinstance(instance_id, str) or not instance_id:
            raise SystemExit("snapshot terminal has no instance ID")
        if not isinstance(slug, str) or not safe_segment.fullmatch(slug):
            raise SystemExit("snapshot terminal has an unsafe slug")
        if instance_id in terminal_ids or slug in terminal_slugs:
            raise SystemExit("snapshot terminal IDs/slugs are not unique")
        terminal_ids.add(instance_id)
        terminal_slugs.add(slug)
        record = f"runs/{slug}/run_record.json"
        required = {
            record,
            f"runs/{slug}/prediction.jsonl",
            f"runs/{slug}/prediction-audit/{slug}.json",
        }
        if terminal.get("status") == "success":
            required.add(f"runs/{slug}/query-plan.json")
        elif terminal.get("status") == "failure":
            required.add(f"runs/{slug}/failure.json")
        else:
            raise SystemExit("snapshot terminal status is invalid")
        if not required <= set(identities):
            raise SystemExit(f"snapshot terminal artifacts are incomplete: {instance_id}")
        if terminal.get("run_record_mtime_ns") != identities[record]["mtime_ns"]:
            raise SystemExit(f"snapshot terminal mtime binding differs: {instance_id}")
    for path in identities:
        parts = PurePosixPath(path).parts
        if parts[0] == "runs" and parts[1] not in terminal_slugs:
            raise SystemExit(f"snapshot includes an unbound task path: {path}")
PY
    then
        return 1
    fi
    tar -xf "$archive" -C "$stage" || return 1
    if ! python3 - "$stage" "$SNAPSHOT_INDEX" "$run_id" "$EXPECTED_TOTAL" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
index_name, expected_run_id, expected_total_raw = sys.argv[2:]
expected_total = int(expected_total_raw)
payload = json.loads((root / index_name).read_text(encoding="utf-8"))
if payload.get("run_id") != expected_run_id:
    raise SystemExit("extracted snapshot run ID differs")
identities = {item["path"]: item for item in payload["files"]}
actual = set()
for directory, dirnames, filenames in os.walk(root, followlinks=False):
    for name in dirnames:
        path = Path(directory) / name
        if path.is_symlink():
            raise SystemExit(f"symlink directory rejected after extraction: {path}")
    for name in filenames:
        path = Path(directory) / name
        relative = path.relative_to(root).as_posix()
        if relative == index_name:
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit(f"non-regular extracted member rejected: {relative}")
        actual.add(relative)
        expected = identities.get(relative)
        if expected is None:
            raise SystemExit(f"unexpected extracted member: {relative}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if info.st_size != expected["size"] or digest != expected["sha256"]:
            raise SystemExit(f"extracted content differs from remote identity: {relative}")
        if info.st_mtime_ns != expected["mtime_ns"]:
            raise SystemExit(
                f"nanosecond mtime was not preserved for {relative}: "
                f"remote={expected['mtime_ns']} local={info.st_mtime_ns}"
            )
if actual != set(identities):
    raise SystemExit("extracted member set differs from remote identity index")
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
if (
    not isinstance(manifest, list)
    or len(manifest) != expected_total
    or len(set(manifest)) != expected_total
):
    raise SystemExit(f"extracted manifest is not the exact {expected_total}-task universe")
print(payload["terminal_tasks"])
PY
    then
        return 1
    fi
}

merge_snapshot() { # stage mirror snapshots
    python3 - "$1" "$2" "$3" "$SNAPSHOT_INDEX" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

stage, mirror, snapshots = map(Path, sys.argv[1:4])
index_name = sys.argv[4]
index_bytes = (stage / index_name).read_bytes()
payload = json.loads(index_bytes)
count = payload["terminal_tasks"]
mirror.mkdir(parents=True, exist_ok=True)
snapshots.mkdir(parents=True, exist_ok=True)

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

for expected in payload["files"]:
    relative = Path(expected["path"])
    source = stage / relative
    destination = mirror / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        info = destination.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size != expected["size"]
            or info.st_mtime_ns != expected["mtime_ns"]
            or sha256(destination) != expected["sha256"]
        ):
            raise SystemExit(f"remote terminal artifact changed after first capture: {relative}")
    else:
        try:
            os.link(source, destination)
        except FileExistsError:
            raise SystemExit(f"snapshot merge raced on destination: {relative}")

receipt = snapshots / f"terminal-{count:04d}.json"
if receipt.exists():
    if receipt.read_bytes() != index_bytes:
        raise SystemExit(f"same terminal count produced a different immutable snapshot: {receipt}")
else:
    descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as target:
        target.write(index_bytes)
        target.flush()
        os.fsync(target.fileno())
print(count)
PY
}

checkpoint_count() {
    python3 - "$CHECKPOINT_ROOT" <<'PY'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
counts = []
if root.is_dir():
    for path in root.iterdir():
        match = re.fullmatch(r"checkpoint-([0-9]{4})", path.name)
        if match and path.is_dir():
            counts.append(int(match.group(1)))
print(max(counts, default=0))
PY
}

validate_output() { # path mode count [candidate]
    python3 - "$@" "$TREATMENT" <<'PY'
import json
from pathlib import Path
import sys

path, mode, count, candidate, treatment = sys.argv[1:]
payload = json.loads(Path(path).read_text(encoding="utf-8"))
if payload.get("schema_version") != 1 or payload.get("mode") != mode or payload.get("valid") is False:
    raise SystemExit(f"invalid immutable monitor output: {path}")
expected = int(count)
if mode == "seal-batch":
    actual = payload.get("completed_count")
    treatment_contract = payload.get("candidate_treatment")
    actual_treatment = (
        treatment_contract.get("name")
        if isinstance(treatment_contract, dict)
        else None
    )
elif mode == "checkpoint":
    actual = payload.get("cumulative", {}).get("count")
    inputs = payload.get("inputs", {})
    actual_treatment = inputs.get("candidate_treatment")
    if candidate and inputs.get("candidate_run") != candidate:
        raise SystemExit(f"N=100 output uses a different candidate artifact: {path}")
else:
    raise SystemExit(f"unsupported output mode: {mode}")
if actual != expected or actual_treatment != treatment:
    raise SystemExit(f"immutable monitor output binding differs: {path}")
PY
}

run_freeze() {
    local target="$1" output
    output="$OUTPUT_DIR/freeze-through-$(printf '%04d' "$1").json"
    [ ! -e "$output" ] || monitor_fail "refusing to overwrite incomplete freeze output: $output"
    python3 "$COMPARE_RUNS" freeze-checkpoints \
        --control "$CONTROL" \
        --candidate "$MIRROR_ROOT" \
        --checkpoint-root "$CHECKPOINT_ROOT" \
        --candidate-treatment "$TREATMENT" \
        --json-out "$output" \
        || monitor_fail "checkpoint freeze failed; remote run was left untouched"
    local actual
    actual="$(checkpoint_count)"
    [ "$actual" -eq "$target" ] \
        || monitor_fail "freeze stopped at N=$actual, expected N=$target"
}

run_seal() {
    local count="$1" checkpoint output
    checkpoint="$CHECKPOINT_ROOT/checkpoint-$(printf '%04d' "$1")"
    output="$OUTPUT_DIR/seal-$(printf '%04d' "$1").json"
    [ -d "$checkpoint" ] || monitor_fail "missing checkpoint directory: $checkpoint"
    if [ -e "$output" ]; then
        [ -d "$checkpoint/evaluation" ] \
            || monitor_fail "seal receipt exists without sealed evaluation: $output"
        validate_output "$output" seal-batch "$count" "" \
            || monitor_fail "stored seal receipt is invalid: $output"
        return 0
    fi
    if [ -d "$checkpoint/evaluation" ]; then
        monitor_warn "N=$count evaluation is already sealed but its outer receipt is absent; comparison will validate the seal"
        return 0
    fi
    python3 "$COMPARE_RUNS" seal-batch \
        --checkpoint-dir "$checkpoint" \
        --evaluator "$EVALUATOR" \
        --runtime-manifest "$RUNTIME_MANIFEST" \
        --cache-dir "$CACHE_DIR" \
        --candidate-treatment "$TREATMENT" \
        --json-out "$output" \
        || monitor_fail "official evaluator/seal failed at N=$count; remote run was left untouched"
    validate_output "$output" seal-batch "$count" "" \
        || monitor_fail "new seal receipt is invalid at N=$count"
}

run_comparison() { # count [complete-candidate]
    local count="$1" candidate="${2:-}"
    local output
    output="$OUTPUT_DIR/compare-$(printf '%04d' "$1").json"
    if [ -e "$output" ]; then
        validate_output "$output" checkpoint "$count" "$candidate" \
            || monitor_fail "stored comparison is invalid: $output"
        return 0
    fi
    local args=(
        "$COMPARE_RUNS" compare-checkpoint
        --control "$CONTROL"
        --checkpoint-root "$CHECKPOINT_ROOT"
        --count "$count"
        --candidate-treatment "$TREATMENT"
        --json-out "$output"
    )
    if [ -n "$candidate" ]; then
        args+=(--candidate-run "$candidate")
    fi
    local rc=0
    python3 "${args[@]}" || rc=$?
    case "$rc" in
        0|1) ;; # 1 is a valid measured regression/gate failure, not a comparator error.
        *) monitor_fail "checkpoint comparator was invalid at N=$count (exit $rc)" ;;
    esac
    validate_output "$output" checkpoint "$count" "$candidate" \
        || monitor_fail "comparison output is invalid at N=$count"
    if [ "$rc" -eq 1 ]; then
        monitor_warn "N=$count is a valid FAIL/regression result; monitoring continues"
    fi
}

write_handoff() {
    python3 - "$MONITOR_ROOT/N100-HANDOFF.json" "$RUN_ID" "$CHECKPOINT_ROOT" \
        "$CONTROL" "$RUNTIME_MANIFEST" "$EVALUATOR" "$CACHE_DIR" \
        "$COMPLETE_CANDIDATE" <<'PY'
import json
import os
from pathlib import Path
import sys

(output, run_id, checkpoint_root, control, runtime_manifest, evaluator,
 cache_dir, candidate) = sys.argv[1:]
payload = {
    "schema_version": 1,
    "mode": "contextbench-n100-candidate-artifact-handoff",
    "status": "awaiting_complete_candidate_artifact",
    "run_id": run_id,
    "checkpoint_root": checkpoint_root,
    "control": control,
    "runtime_manifest": runtime_manifest,
    "evaluator": evaluator,
    "cache_dir": cache_dir,
    "expected_candidate_artifact": candidate or None,
    "requirement": (
        "Collect the completed remote run with its full official results, report, triage, "
        "driver summary, and all 100 terminal task artifacts; rerun this monitor with "
        "--complete-candidate-artifact pointing to that explicit directory."
    ),
    "remote_run_was_stopped_or_killed": False,
}
encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
path = Path(output)
if path.exists():
    if path.read_bytes() != encoded:
        raise SystemExit(f"N=100 handoff binding changed: {path}")
else:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as target:
        target.write(encoded)
        target.flush()
        os.fsync(target.fileno())
PY
}

poll_once() {
    local incoming archive stage
    incoming="$(mktemp -d "$MONITOR_ROOT/.incoming.XXXXXX")"
    archive="$incoming/snapshot.tar"
    stage="$incoming/extracted"
    collect_remote_snapshot "$archive"
    validate_and_extract_snapshot "$archive" "$stage" "$RUN_ID" >/dev/null \
        || monitor_fail "remote snapshot failed path/mtime validation"
    TERMINAL_COUNT="$(merge_snapshot "$stage" "$MIRROR_ROOT" "$SNAPSHOT_DIR")" \
        || monitor_fail "remote snapshot conflicts with the cumulative mirror"
    rm -rf "$incoming"
    monitor_info "run=$RUN_ID terminal=$TERMINAL_COUNT/$EXPECTED_TOTAL"
    if [ "$SNAPSHOT_ONLY" -eq 1 ]; then
        return 0
    fi

    local target existing count candidate_for_n100=""
    target=$(( (TERMINAL_COUNT / 10) * 10 ))
    existing="$(checkpoint_count)"
    [ "$existing" -le "$target" ] \
        || monitor_fail "checkpoint chain N=$existing is ahead of remote terminal N=$TERMINAL_COUNT"
    if [ "$target" -gt "$existing" ]; then
        run_freeze "$target"
    fi

    count=10
    while [ "$count" -le "$target" ]; do
        run_seal "$count"
        if [ "$count" -eq 100 ]; then
            local output="$OUTPUT_DIR/compare-0100.json"
            if [ -e "$output" ]; then
                run_comparison 100 ""
                FINAL_DONE=1
                return 0
            fi
            if [ -n "$COMPLETE_CANDIDATE" ] && [ -d "$COMPLETE_CANDIDATE" ]; then
                candidate_for_n100="$COMPLETE_CANDIDATE"
                run_comparison 100 "$candidate_for_n100"
                FINAL_DONE=1
                return 0
            fi
            write_handoff
            HANDOFF_NEEDED=1
            return 0
        fi
        run_comparison "$count"
        count=$((count + 10))
    done
}

main() {
    local control_raw="" checkpoint_raw="" runtime_raw="" evaluator_raw=""
    local cache_raw="" candidate_raw=""
    RUN_ID=""
    POLL_SECONDS=30
    ONCE=0
    EXPECTED_TOTAL="${EXPECTED_TOTAL:-100}"
    SNAPSHOT_ONLY="${SNAPSHOT_ONLY:-0}"
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --run-id) RUN_ID="${2:?--run-id needs a value}"; shift ;;
            --control) control_raw="${2:?--control needs a value}"; shift ;;
            --checkpoint-root) checkpoint_raw="${2:?--checkpoint-root needs a value}"; shift ;;
            --runtime-manifest) runtime_raw="${2:?--runtime-manifest needs a value}"; shift ;;
            --evaluator) evaluator_raw="${2:?--evaluator needs a value}"; shift ;;
            --cache-dir) cache_raw="${2:?--cache-dir needs a value}"; shift ;;
            --complete-candidate-artifact)
                candidate_raw="${2:?--complete-candidate-artifact needs a value}"
                shift
                ;;
            --poll-seconds) POLL_SECONDS="${2:?--poll-seconds needs a value}"; shift ;;
            --once) ONCE=1 ;;
            -h|--help) usage; return 0 ;;
            *) monitor_fail "unknown argument: $1" ;;
        esac
        shift
    done

    [ -n "$RUN_ID" ] || monitor_fail "--run-id is required"
    [ -n "$control_raw" ] || monitor_fail "--control is required"
    [ -n "$checkpoint_raw" ] || monitor_fail "--checkpoint-root is required"
    [ -n "$runtime_raw" ] || monitor_fail "--runtime-manifest is required"
    [ -n "$evaluator_raw" ] || monitor_fail "--evaluator is required"
    [ -n "$cache_raw" ] || monitor_fail "--cache-dir is required"
    validate_run_id "$RUN_ID"
    [[ "$POLL_SECONDS" =~ ^[1-9][0-9]{0,3}$ ]] \
        || monitor_fail "--poll-seconds must be an integer from 1 to 9999"
    [[ "$EXPECTED_TOTAL" =~ ^[1-9][0-9]{0,2}$ ]] && [ "$EXPECTED_TOTAL" -le 100 ] \
        || monitor_fail "EXPECTED_TOTAL must be an integer from 1 to 100"
    [[ "$SNAPSHOT_ONLY" =~ ^[01]$ ]] \
        || monitor_fail "SNAPSHOT_ONLY must be 0 or 1"

    # shellcheck source=common.sh
    # shellcheck disable=SC1091 # resolved from this script's absolute directory
    . "$SCRIPT_DIR/common.sh"
    command -v python3 >/dev/null 2>&1 || monitor_fail "python3 is required"
    command -v tar >/dev/null 2>&1 || monitor_fail "tar is required"
    SSH_BIN="${MONITOR_EVERY10_SSH_BIN:-ssh}"
    command -v "$SSH_BIN" >/dev/null 2>&1 || monitor_fail "SSH command not found: $SSH_BIN"
    [ -f "$COMPARE_RUNS" ] || monitor_fail "comparator missing: $COMPARE_RUNS"

    CONTROL="$(canonical_path "$control_raw" dir)" \
        || monitor_fail "invalid --control path"
    CHECKPOINT_ROOT="$(canonical_path "$checkpoint_raw" dir-maybe)" \
        || monitor_fail "invalid --checkpoint-root path"
    RUNTIME_MANIFEST="$(canonical_path "$runtime_raw" file)" \
        || monitor_fail "invalid --runtime-manifest path"
    EVALUATOR="$(canonical_path "$evaluator_raw" file)" \
        || monitor_fail "invalid --evaluator path"
    CACHE_DIR="$(canonical_path "$cache_raw" dir-maybe)" \
        || monitor_fail "invalid --cache-dir path"
    COMPLETE_CANDIDATE=""
    if [ -n "$candidate_raw" ]; then
        COMPLETE_CANDIDATE="$(canonical_path "$candidate_raw" dir-maybe)" \
            || monitor_fail "invalid --complete-candidate-artifact path"
    fi
    mkdir -p "$CHECKPOINT_ROOT" "$CACHE_DIR"

    local ip
    ip="$(instance_ip)"
    SSH_TARGET="$REMOTE_USER@$ip"
    MONITOR_ROOT="${CHECKPOINT_ROOT}.monitor-${RUN_ID}"
    MIRROR_ROOT="$MONITOR_ROOT/candidate-mirror"
    SNAPSHOT_DIR="$MONITOR_ROOT/snapshots"
    OUTPUT_DIR="$MONITOR_ROOT/outputs"
    mkdir -p "$OUTPUT_DIR"
    LOCK_HELD=0
    acquire_monitor_lock
    trap release_monitor_lock EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP

    write_or_verify_binding \
        || monitor_fail "monitor inputs or pinned file identities changed"
    TERMINAL_COUNT=0
    FINAL_DONE=0
    HANDOFF_NEEDED=0

    while :; do
        write_or_verify_binding \
            || monitor_fail "monitor inputs or pinned file identities changed during polling"
        poll_once
        if [ "$FINAL_DONE" -eq 1 ]; then
            monitor_info "N=100 comparison is sealed; remote run was not stopped or killed"
            return 0
        fi
        if [ "$HANDOFF_NEEDED" -eq 1 ]; then
            monitor_warn "N=100 batches are sealed; complete candidate collection is required"
            monitor_warn "handoff: $MONITOR_ROOT/N100-HANDOFF.json"
            return 3
        fi
        if [ "$ONCE" -eq 1 ]; then
            return 0
        fi
        sleep "$POLL_SECONDS"
    done
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
