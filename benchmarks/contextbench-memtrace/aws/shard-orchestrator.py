#!/usr/bin/env python3
"""Horizontal fleet launcher for the ContextBench AWS harness.

The current production shape uses five c7a.48xlarge hosts at twelve pinned
workers each: 960 of the account's 1,024 Standard-instance vCPUs and sixty
genuinely concurrent benchmark tasks. Each host retains the same proven
12 x 16-core taskset geometry as the single-box gate.

This reuses the existing aws/remote/*.sh building blocks (bootstrap-remote.sh,
build-memtrace-remote.sh, run-remote.sh) but drives them directly via ssh/rsync
per shard instead of through the single-box 01-03 scripts, because those
scripts hardcode one STATE_DIR / one persistent data volume.

Usage:
    python3 shard-orchestrator.py provision --shards 5 --manifest exact-100.json
    python3 shard-orchestrator.py bootstrap
    python3 shard-orchestrator.py run
    python3 shard-orchestrator.py poll
    python3 shard-orchestrator.py collect
    python3 shard-orchestrator.py teardown

State for the whole fleet lives in aws/state/fleet/<RUN_TAG>/fleet.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

AWS_DIR = Path(__file__).resolve().parent
ADAPTER_DIR = AWS_DIR.parent
REPO_ROOT = ADAPTER_DIR.parent.parent  # memtrace-public
ENV_FILE = REPO_ROOT / ".env"
MEMTRACE_SOURCE_DIR = Path(
    os.environ.get(
        "CB_MEMTRACE_SOURCE_DIR", "/Users/alexholmberg/Desktop/Memtrace/memtrace"
    )
).resolve()
DATA_LOCAL = Path("/tmp/contextbench/data")
FLEET_STATE_DIR = AWS_DIR / "state" / "fleet"

AWS_REGION = "us-east-1"
INSTANCE_TYPE = "c7a.48xlarge"  # 192 vCPU
CONCURRENCY = 12  # 12 taskset slots x 16 cores
KEY_NAME = "memtrace-bench"
SSH_KEY_PATH = os.path.expanduser("~/.ssh/id_ed25519")
REMOTE_USER = "ubuntu"
REMOTE_ADAPTER_DIR = f"/home/{REMOTE_USER}/contextbench-adapter"
DATASET = "verified"
LINE_BUDGET = 200
SELECTOR_MODEL = "gpt-5"
SELECTOR_MODE = "guarded"
RUN_TIMEOUT = 7200
WATCHDOG_MINUTES = 90
DATA_VOLUME_GB = 500
ROOT_VOLUME_GB = 100

# Known-slow repos from today's real driver_summary.json samples (median 155s,
# mean 823s, max 3478s=58min across only 20 samples — small n, but the only
# real signal available) plus repos flagged slow in program memory (ansible,
# vscode, angular, material-ui/mui, prettier — prettier is known to HANG
# memtrace mcp reproducibly). Used only to bias bin-packing so no single box
# gets stuck with multiple giant tasks while others idle; not a precise cost
# model.
SLOW_REPO_SUBSTRINGS = [
    "ansible",
    "vscode",
    "angular",
    "mui/material",
    "prettier",
]
DEFAULT_TASK_COST_S = 155.0  # observed median
SLOW_TASK_COST_S = 1800.0  # ~30min, conservative for flagged-slow repos

SSH_OPTS = [
    "-i",
    SSH_KEY_PATH,
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "BatchMode=yes",
]


def sh(cmd, check=True, capture=True, timeout=None):
    r = subprocess.run(
        cmd,
        shell=isinstance(cmd, str),
        capture_output=capture,
        text=True,
        timeout=timeout,
    )
    if check and r.returncode != 0:
        raise RuntimeError(
            f"cmd failed ({r.returncode}): {cmd}\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
        )
    return r


def aws(args, check=True):
    return sh(["aws"] + args + ["--region", AWS_REGION], check=check)


def ssh_run(ip, remote_cmd, timeout=None, check=True):
    return sh(
        ["ssh"] + SSH_OPTS + [f"{REMOTE_USER}@{ip}", remote_cmd],
        check=check,
        timeout=timeout,
    )


def rsync_to(ip, src, dst, extra=None, delete=True):
    args = ["rsync", "-az"]
    if delete:
        args.append("--delete")
    if extra:
        args += extra
    args += ["-e", "ssh " + " ".join(SSH_OPTS), src, f"{REMOTE_USER}@{ip}:{dst}"]
    return sh(args, timeout=1800)


def fleet_path(run_tag):
    d = FLEET_STATE_DIR / run_tag
    d.mkdir(parents=True, exist_ok=True)
    return d / "fleet.json"


def load_fleet(run_tag):
    p = fleet_path(run_tag)
    if p.exists():
        return json.loads(p.read_text())
    return {"run_tag": run_tag, "shards": {}}


def save_fleet(run_tag, data):
    p = fleet_path(run_tag)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)


def estimate_task_cost_s(repo):
    r = (repo or "").lower()
    for sub in SLOW_REPO_SUBSTRINGS:
        if sub in r:
            return SLOW_TASK_COST_S
    return DEFAULT_TASK_COST_S


def read_manifest_ids(path):
    loaded = json.loads(Path(path).read_text())
    if isinstance(loaded, dict):
        ids = [str(task["instance_id"]) for task in loaded["tasks"]]
    else:
        ids = [str(instance_id) for instance_id in loaded]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("manifest must contain unique instance IDs")
    return ids


def dataset_kind(dataset_path):
    name = Path(dataset_path).name
    kinds = {
        "contextbench_verified.parquet": "verified",
        "full.parquet": "full",
        "contextbench_verified_train.parquet": "train",
        "contextbench_verified_test.parquet": "test",
    }
    try:
        return kinds[name]
    except KeyError as error:
        raise ValueError(
            f"unsupported ContextBench dataset filename {name!r}; expected one of {sorted(kinds)}"
        ) from error


def remote_dataset_path(kind):
    names = {
        "verified": "contextbench_verified.parquet",
        "full": "full.parquet",
        "train": "contextbench_verified_train.parquet",
        "test": "contextbench_verified_test.parquet",
    }
    try:
        return f"/srv/contextbench/contextbench/data/{names[kind]}"
    except KeyError as error:
        raise ValueError(f"unsupported remote dataset kind {kind!r}") from error


def reusable_host_info(info):
    allowed = ("instance_id", "name", "public_ip", "az", "volume_id", "ssh_up")
    return {key: info[key] for key in allowed if key in info}


def build_shards(n_shards, dataset_path, manifest_path=None):
    import pandas as pd

    df = pd.read_parquet(dataset_path, columns=["instance_id", "repo"])
    repo_by_id = dict(zip(df["instance_id"].astype(str), df["repo"].astype(str)))
    if manifest_path:
        instance_ids = read_manifest_ids(manifest_path)
        unknown = [
            instance_id for instance_id in instance_ids if instance_id not in repo_by_id
        ]
        if unknown:
            raise ValueError(
                f"manifest contains {len(unknown)} IDs outside the dataset: {unknown[:5]}"
            )
    else:
        instance_ids = sorted(
            repo_by_id, key=lambda instance_id: (repo_by_id[instance_id], instance_id)
        )
    tasks = [
        (instance_id, estimate_task_cost_s(repo_by_id[instance_id]), index)
        for index, instance_id in enumerate(instance_ids)
    ]
    # Longest-Processing-Time-first greedy bin packing: sort tasks by
    # estimated cost descending, always place the next task on the
    # currently-least-loaded shard. This is the single biggest lever against
    # missing the wall-clock target — naive round-robin can stack multiple
    # known-slow-repo tasks (ansible/vscode/angular/mui/prettier) onto the
    # same box while others sit idle. Deterministic tie-break on repo+id
    # (already sorted) keeps runs reproducible.
    tasks.sort(key=lambda task: (-task[1], task[2]))
    loads = [0.0] * n_shards
    shards = [[] for _ in range(n_shards)]
    for iid, cost, _ in tasks:
        i = min(range(n_shards), key=lambda k: loads[k])
        shards[i].append(iid)
        loads[i] += cost
    print(
        f"  LPT bin-pack: estimated per-shard load (s) min={min(loads):.0f} max={max(loads):.0f} "
        f"mean={sum(loads) / len(loads):.0f}"
    )
    return shards, instance_ids


def git_provenance(repo, pathspec=None):
    def g(*args):
        return sh(
            ["git", "--no-optional-locks", "-C", str(repo)] + list(args)
        ).stdout.strip()

    head = g("rev-parse", "HEAD")
    describe = g("describe", "--tags", "--dirty", "--always")
    status_cmd = [
        "git",
        "--no-optional-locks",
        "-C",
        str(repo),
        "status",
        "--porcelain",
    ]
    if pathspec:
        status_cmd += ["--", pathspec]
    dirty_out = sh(status_cmd).stdout
    dirty_count = len([line for line in dirty_out.splitlines() if line.strip()])
    diff_sha = ""
    if dirty_count:
        diff_cmd = ["git", "--no-optional-locks", "-C", str(repo), "diff", "HEAD"]
        if pathspec:
            diff_cmd += ["--", pathspec]
        diff = sh(diff_cmd).stdout
        diff_sha = hashlib.sha256(diff.encode()).hexdigest()[:16]
    return {
        "head_sha": head,
        "git_describe": describe,
        "dirty_file_count": dirty_count,
        "dirty_diff_sha256_16": diff_sha,
    }


def local_git_provenance():
    return git_provenance(MEMTRACE_SOURCE_DIR)


def local_adapter_provenance():
    return git_provenance(REPO_ROOT, "benchmarks/contextbench-memtrace")


def prepare_source_payload(run_tag, provenance):
    payload_dir = FLEET_STATE_DIR / run_tag / "source-payload"
    payload_dir.mkdir(parents=True, exist_ok=True)
    file_list = payload_dir / "source-files.list0"
    checksums = payload_dir / "source-files.sha256"
    archive = payload_dir / "source.tar.gz"
    manifest = payload_dir / "source-manifest.json"

    submodules = sh(
        [
            "git",
            "--no-optional-locks",
            "-C",
            str(MEMTRACE_SOURCE_DIR),
            "submodule",
            "status",
            "--recursive",
        ]
    ).stdout.splitlines()
    uninitialized_submodules = [line for line in submodules if line.startswith("-")]
    if uninitialized_submodules:
        names = ", ".join(line[41:].strip() for line in uninitialized_submodules)
        raise RuntimeError(
            "Memtrace source has uninitialized submodules: "
            f"{names}. Run git submodule update --init --recursive first."
        )

    tracked = sh(
        [
            "git",
            "--no-optional-locks",
            "-C",
            str(MEMTRACE_SOURCE_DIR),
            "ls-files",
            "--recurse-submodules",
            "-z",
        ]
    ).stdout
    untracked = sh(
        [
            "git",
            "--no-optional-locks",
            "-C",
            str(MEMTRACE_SOURCE_DIR),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ]
    ).stdout
    paths = [path for path in (tracked + untracked).split("\0") if path]
    if not paths:
        raise RuntimeError("source payload file list is empty")
    file_list.write_bytes(("\0".join(paths) + "\0").encode())

    checksum_lines = []
    for relative in paths:
        source = MEMTRACE_SOURCE_DIR / relative
        checksum_lines.append(f"{sha256_file(source)}  {relative}\n")
    checksums.write_text("".join(checksum_lines))
    payload_sha = sha256_file(checksums)

    environment = os.environ.copy()
    environment["COPYFILE_DISABLE"] = "1"
    subprocess.run(
        [
            "tar",
            "--no-xattrs",
            "--null",
            "-T",
            str(file_list),
            "-czf",
            str(archive),
        ],
        cwd=MEMTRACE_SOURCE_DIR,
        env=environment,
        check=True,
    )
    source_manifest = {
        "install_mode": "source",
        **provenance,
        "source_dir_local": str(MEMTRACE_SOURCE_DIR),
        "source_payload_sha256": payload_sha,
        "source_file_count": len(paths),
        "submodules": submodules,
        "publishable_source": provenance["dirty_file_count"] == 0,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "captured_on": socket.gethostname(),
    }
    write_json_atomic(manifest, source_manifest)
    return {
        "archive": archive,
        "checksums": checksums,
        "manifest": manifest,
        "payload_sha256": payload_sha,
        "file_count": len(paths),
    }


# ---------------------------------------------------------------------------
def cmd_adopt(args):
    if not args.adopt_state:
        raise ValueError("adopt requires --adopt-state pointing at an existing fleet.json")
    source_path = Path(args.adopt_state)
    source = json.loads(source_path.read_text())
    source_shards = [source["shards"][sid] for sid in sorted(source["shards"])]
    if len(source_shards) < args.shards:
        raise ValueError(
            f"adopt source has {len(source_shards)} hosts, fewer than requested {args.shards}"
        )
    shards, source_manifest = build_shards(
        args.shards, args.dataset, args.manifest or None
    )
    fleet = {
        "run_tag": args.run_tag,
        "adopted_from": str(source_path.resolve()),
        "shards": {},
        "instance_type": source.get("instance_type", args.instance_type),
        "instance_vcpus": source.get("instance_vcpus"),
        "requested_vcpus": source.get("requested_vcpus"),
        "quota_vcpus": source.get("quota_vcpus"),
        "concurrency_per_shard": args.concurrency,
        "root_volume_gb": source.get("root_volume_gb", args.root_volume_gb),
        "data_volume_gb": source.get("data_volume_gb", args.data_volume_gb),
        "dataset_kind": dataset_kind(args.dataset),
        "dataset_path_local": str(Path(args.dataset).resolve()),
        "source_manifest": source_manifest,
        "source_manifest_sha256": (
            hashlib.sha256(Path(args.manifest).read_bytes()).hexdigest()
            if args.manifest
            else hashlib.sha256(json.dumps(source_manifest).encode()).hexdigest()
        ),
    }
    for index, task_ids in enumerate(shards):
        sid = f"shard-{index:02d}"
        fleet["shards"][sid] = {
            **reusable_host_info(source_shards[index]),
            "task_ids": task_ids,
            "adopted": True,
            "bootstrap_ok": False,
        }
    save_fleet(args.run_tag, fleet)
    print(
        f"[adopt] {args.shards} existing hosts assigned {len(source_manifest)} "
        f"{fleet['dataset_kind']} tasks -> {fleet_path(args.run_tag)}"
    )


def cmd_provision(args):
    run_tag = args.run_tag
    fleet = load_fleet(run_tag)

    quota = float(
        aws(
            [
                "service-quotas",
                "get-service-quota",
                "--service-code",
                "ec2",
                "--quota-code",
                "L-1216C47A",
                "--query",
                "Quota.Value",
                "--output",
                "text",
            ]
        ).stdout.strip()
    )
    instance_vcpus = int(
        aws(
            [
                "ec2",
                "describe-instance-types",
                "--instance-types",
                args.instance_type,
                "--query",
                "InstanceTypes[0].VCpuInfo.DefaultVCpus",
                "--output",
                "text",
            ]
        ).stdout.strip()
    )
    requested_vcpus = args.shards * instance_vcpus
    if requested_vcpus > quota:
        raise RuntimeError(
            f"fleet requests {requested_vcpus} vCPUs, above the live {quota:.0f}-vCPU quota"
        )
    print(
        f"[provision] capacity guard: {args.shards} x {args.instance_type} "
        f"({instance_vcpus} vCPU) = {requested_vcpus}/{quota:.0f} vCPUs"
    )

    print("[provision] resolving AMI/SG/keypair (shared across shards)")
    ami = aws(
        [
            "ssm",
            "get-parameters",
            "--names",
            "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
            "--query",
            "Parameters[0].Value",
            "--output",
            "text",
        ]
    ).stdout.strip()
    print(f"  ami={ami}")

    vpc = aws(
        [
            "ec2",
            "describe-vpcs",
            "--filters",
            "Name=isDefault,Values=true",
            "--query",
            "Vpcs[0].VpcId",
            "--output",
            "text",
        ]
    ).stdout.strip()
    sg_name = "contextbench-shard-ssh"
    r = aws(
        [
            "ec2",
            "describe-security-groups",
            "--filters",
            f"Name=group-name,Values={sg_name}",
            f"Name=vpc-id,Values={vpc}",
            "--query",
            "SecurityGroups[0].GroupId",
            "--output",
            "text",
        ],
        check=False,
    )
    sg_id = r.stdout.strip()
    if not sg_id or sg_id == "None":
        sg_id = aws(
            [
                "ec2",
                "create-security-group",
                "--group-name",
                sg_name,
                "--description",
                "ContextBench shard fleet: SSH from operator IP",
                "--vpc-id",
                vpc,
                "--tag-specifications",
                f"ResourceType=security-group,Tags=[{{Key=Name,Value={sg_name}}}]",
                "--query",
                "GroupId",
                "--output",
                "text",
            ]
        ).stdout.strip()
        print(f"  created SG {sg_id}")
    else:
        print(f"  reusing SG {sg_id}")
    myip = sh(["curl", "-fsS", "https://checkip.amazonaws.com"]).stdout.strip()
    aws(
        [
            "ec2",
            "authorize-security-group-ingress",
            "--group-id",
            sg_id,
            "--protocol",
            "tcp",
            "--port",
            "22",
            "--cidr",
            f"{myip}/32",
        ],
        check=False,
    )
    print(f"  SSH authorized from {myip}/32")

    fleet["ami_id"] = ami
    fleet["sg_id"] = sg_id
    fleet["key_name"] = KEY_NAME
    fleet["instance_type"] = args.instance_type
    fleet["instance_vcpus"] = instance_vcpus
    fleet["requested_vcpus"] = requested_vcpus
    fleet["quota_vcpus"] = quota
    fleet["concurrency_per_shard"] = args.concurrency
    fleet["root_volume_gb"] = args.root_volume_gb
    fleet["data_volume_gb"] = args.data_volume_gb
    save_fleet(run_tag, fleet)

    print(f"[provision] building {args.shards} shards from {args.dataset}")
    shards, source_manifest = build_shards(
        args.shards, args.dataset, args.manifest or None
    )
    fleet["dataset_kind"] = dataset_kind(args.dataset)
    fleet["dataset_path_local"] = str(Path(args.dataset).resolve())
    if fleet["dataset_kind"] == "full":
        ensure_full_tracker_dataset(Path(args.dataset))
    fleet["source_manifest"] = source_manifest
    fleet["source_manifest_sha256"] = (
        hashlib.sha256(Path(args.manifest).read_bytes()).hexdigest()
        if args.manifest
        else hashlib.sha256(json.dumps(source_manifest).encode()).hexdigest()
    )
    for i, task_ids in enumerate(shards):
        sid = f"shard-{i:02d}"
        fleet["shards"].setdefault(sid, {})["task_ids"] = task_ids

    if args.adopt_state:
        adopted = json.loads(Path(args.adopt_state).read_text())
        shard = fleet["shards"]["shard-00"]
        shard.update(
            {
                "instance_id": adopted["instance_id"],
                "public_ip": adopted.get("public_ip"),
                "volume_id": adopted["volume_id"],
                "adopted": True,
            }
        )
        print(
            f"  shard-00: adopting {shard['instance_id']} "
            f"with data volume {shard['volume_id']}"
        )
    save_fleet(run_tag, fleet)
    total = sum(len(s) for s in shards)
    print(
        f"  {len(shards)} shards, sizes {min(len(s) for s in shards)}-{max(len(s) for s in shards)}, total tasks={total}"
    )

    def launch_one(sid):
        info = fleet["shards"][sid]
        if info.get("instance_id"):
            print(
                f"  {sid}: already has instance {info['instance_id']}, skipping launch"
            )
            return sid, info
        name = f"contextbench-{run_tag}-{sid}"
        try:
            launch = aws(
                [
                    "ec2",
                    "run-instances",
                    "--instance-type",
                    args.instance_type,
                    "--image-id",
                    ami,
                    "--key-name",
                    KEY_NAME,
                    "--security-group-ids",
                    sg_id,
                    "--count",
                    "1",
                    "--block-device-mappings",
                    f"DeviceName=/dev/sda1,Ebs={{VolumeSize={args.root_volume_gb},VolumeType=gp3,DeleteOnTermination=true}}",
                    "--metadata-options",
                    "HttpTokens=required,HttpPutResponseHopLimit=2",
                    "--tag-specifications",
                    f"ResourceType=instance,Tags=[{{Key=Name,Value={name}}},{{Key=cb-run,Value={run_tag}}},{{Key=cb-shard,Value={sid}}}]",
                    "--query",
                    "Instances[0].InstanceId",
                    "--output",
                    "text",
                ]
            )
            iid = launch.stdout.strip()
            info["instance_id"] = iid
            info["name"] = name
            print(f"  {sid}: launched {iid} (on-demand {INSTANCE_TYPE})")
        except Exception as e:
            info["launch_error"] = str(e)[-2000:]
            print(f"  {sid}: LAUNCH FAILED: {e}")
        return sid, info

    # Per-shard exceptions must never abort the batch (a quota rejection on
    # shard N must not prevent already-launched shards 0..N-1 from reaching
    # the finish phase below — that bug left 11 real instances stranded
    # with no attached volume / no recorded IP on the first run).
    with ThreadPoolExecutor(max_workers=10) as ex:
        for fut in as_completed(
            [ex.submit(launch_one, sid) for sid in fleet["shards"]]
        ):
            # Persist after EVERY future, not after the whole batch: a crash
            # (or an aws-cli error we failed to catch) partway through the
            # batch must not lose already-launched instance ids — that bug
            # stranded 11 real, paying instances untracked on 2026-07-12.
            sid, info = fut.result()
            fleet["shards"][sid] = info
            save_fleet(run_tag, fleet)
    fleet["shards"] = {
        sid: info for sid, info in fleet["shards"].items() if info.get("instance_id")
    }
    save_fleet(run_tag, fleet)

    print(
        "[provision] waiting for all instances running + assigning/attaching data volumes..."
    )

    def finish_one(sid):
        info = fleet["shards"][sid]
        iid = info["instance_id"]
        try:
            aws(["ec2", "wait", "instance-running", "--instance-ids", iid])
            desc = aws(
                [
                    "ec2",
                    "describe-instances",
                    "--instance-ids",
                    iid,
                    "--query",
                    "Reservations[0].Instances[0].[PublicIpAddress,Placement.AvailabilityZone]",
                    "--output",
                    "text",
                ]
            ).stdout.split()
            ip, az = desc[0], desc[1]
            info["public_ip"] = ip
            info["az"] = az
            if not info.get("volume_id"):
                vol_name = f"contextbench-{run_tag}-{sid}-data"
                vol = aws(
                    [
                        "ec2",
                        "create-volume",
                        "--availability-zone",
                        az,
                        "--size",
                        str(args.data_volume_gb),
                        "--volume-type",
                        "gp3",
                        "--tag-specifications",
                        f"ResourceType=volume,Tags=[{{Key=Name,Value={vol_name}}},{{Key=cb-run,Value={run_tag}}},{{Key=cb-shard,Value={sid}}}]",
                        "--query",
                        "VolumeId",
                        "--output",
                        "text",
                    ]
                ).stdout.strip()
                aws(["ec2", "wait", "volume-available", "--volume-ids", vol])
                aws(
                    [
                        "ec2",
                        "attach-volume",
                        "--volume-id",
                        vol,
                        "--instance-id",
                        iid,
                        "--device",
                        "/dev/sdf",
                    ]
                )
                aws(["ec2", "wait", "volume-in-use", "--volume-ids", vol])
                info["volume_id"] = vol
            # wait ssh
            deadline = time.time() + 300
            up = False
            while time.time() < deadline:
                r = ssh_run(ip, "true", timeout=15, check=False)
                if r.returncode == 0:
                    up = True
                    break
                time.sleep(5)
            info["ssh_up"] = up
            print(f"  {sid}: {iid} {ip} {az} vol={info['volume_id']} ssh_up={up}")
        except Exception as e:
            info["finish_error"] = str(e)[-2000:]
            info["ssh_up"] = False
            print(f"  {sid}: FINISH FAILED: {e}")
        return sid, info

    with ThreadPoolExecutor(max_workers=10) as ex:
        for fut in as_completed(
            [ex.submit(finish_one, sid) for sid in fleet["shards"]]
        ):
            sid, info = fut.result()
            fleet["shards"][sid] = info
            save_fleet(run_tag, fleet)

    bad = [sid for sid, info in fleet["shards"].items() if not info.get("ssh_up")]
    if bad:
        print(f"[provision] WARNING: {len(bad)} shards not SSH-reachable: {bad}")
    print(f"[provision] done. fleet state: {fleet_path(run_tag)}")


def cmd_bootstrap(args):
    run_tag = args.run_tag
    fleet = load_fleet(run_tag)
    prov = local_git_provenance()
    adapter_prov = local_adapter_provenance()
    print(
        f"[bootstrap] local memtrace HEAD={prov['head_sha'][:12]} dirty_files={prov['dirty_file_count']} diff_sha={prov['dirty_diff_sha256_16'] or 'clean'}"
    )
    print(
        f"[bootstrap] adapter HEAD={adapter_prov['head_sha'][:12]} dirty_files={adapter_prov['dirty_file_count']} diff_sha={adapter_prov['dirty_diff_sha256_16'] or 'clean'}"
    )
    payload = prepare_source_payload(run_tag, prov)
    print(
        f"[bootstrap] sealed source payload={payload['payload_sha256'][:16]} "
        f"files={payload['file_count']}"
    )
    fleet["memtrace_source_provenance"] = prov
    fleet["adapter_provenance"] = adapter_prov
    fleet["source_payload_sha256"] = payload["payload_sha256"]
    fleet["source_file_count"] = payload["file_count"]
    save_fleet(run_tag, fleet)

    shard_ids = args.only.split(",") if args.only else list(fleet["shards"].keys())

    def boot_one(sid):
        info = fleet["shards"][sid]
        log = []
        try:
            ip = info["public_ip"]
            vol = info["volume_id"]
            ssh_run(ip, f"mkdir -p {REMOTE_ADAPTER_DIR}")
            rsync_to(
                ip,
                str(ADAPTER_DIR) + "/",
                REMOTE_ADAPTER_DIR + "/",
                extra=[
                    "--exclude",
                    ".env",
                    "--exclude",
                    "__pycache__",
                    "--exclude",
                    ".DS_Store",
                    "--exclude",
                    "aws/state",
                    "--exclude",
                    "aws/config.env",
                    "--exclude",
                    "work/",
                ],
            )
            log.append("adapter synced")

            ssh_run(
                ip,
                f"bash {REMOTE_ADAPTER_DIR}/aws/remote/bootstrap-remote.sh "
                f"MEMTRACE_INSTALL_MODE=source DATA_VOL_ID={vol}",
                timeout=1500,
            )
            log.append("bootstrap-remote.sh ok")
            ssh_run(
                ip,
                "/srv/contextbench/venv/bin/python -c "
                "'import docker; assert docker.from_env().ping(); print(docker.__version__)'",
                timeout=30,
            )
            log.append("Docker SDK + daemon verified")

            remote_src = "/srv/contextbench/memtrace-src"
            ssh_run(ip, f"mkdir -p {remote_src}")
            remote_archive = (
                f"/tmp/contextbench-source-{payload['payload_sha256'][:16]}.tar.gz"
            )
            sh(
                ["scp"]
                + SSH_OPTS
                + [
                    "-q",
                    str(payload["archive"]),
                    f"{REMOTE_USER}@{ip}:{remote_archive}",
                ]
            )
            ssh_run(
                ip,
                f"find {remote_src} -mindepth 1 -maxdepth 1 ! -name target -exec rm -rf -- {{}} +; "
                f"tar -xzf {remote_archive} -C {remote_src}; rm -f {remote_archive}",
                timeout=900,
            )
            for local_path, remote_name in (
                (payload["manifest"], "source-manifest.json"),
                (payload["checksums"], "source-files.sha256"),
            ):
                sh(
                    ["scp"]
                    + SSH_OPTS
                    + [
                        "-q",
                        str(local_path),
                        f"{REMOTE_USER}@{ip}:{remote_src}/{remote_name}",
                    ]
                )
            ssh_run(
                ip,
                f"cd {remote_src} && sha256sum -c source-files.sha256 >/dev/null",
                timeout=900,
            )
            log.append(f"source payload verified {payload['payload_sha256'][:16]}")

            ssh_run(
                ip,
                f"bash {REMOTE_ADAPTER_DIR}/aws/remote/build-memtrace-remote.sh SRC_DIR={remote_src}",
                timeout=1500,
            )
            log.append("build-memtrace-remote.sh ok")

            sh(
                ["scp"]
                + SSH_OPTS
                + ["-q", str(ENV_FILE), f"{REMOTE_USER}@{ip}:{REMOTE_ADAPTER_DIR}/.env"]
            )
            ssh_run(ip, f"chmod 600 {REMOTE_ADAPTER_DIR}/.env")
            log.append(".env pushed")

            data_dir = "/srv/contextbench/contextbench/data"
            r = ssh_run(
                ip,
                f"test -f {data_dir}/full.parquet && test -f {data_dir}/contextbench_verified.parquet",
                check=False,
            )
            if r.returncode != 0:
                rsync_to(ip, str(DATA_LOCAL) + "/", data_dir + "/", delete=False)
                log.append("dataset seeded")
            else:
                log.append("dataset already present")

            info["bootstrap_ok"] = True
            info["bootstrap_log"] = log
        except Exception as e:
            info["bootstrap_ok"] = False
            info["bootstrap_error"] = str(e)[-4000:]
            log.append(f"ERROR: {e}")
        print(f"  {sid}: {' | '.join(log)}")
        return sid, info

    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futs = [ex.submit(boot_one, sid) for sid in shard_ids]
        for fut in as_completed(futs):
            sid, info = fut.result()
            fleet["shards"][sid] = info
            save_fleet(run_tag, fleet)

    ok = [sid for sid in shard_ids if fleet["shards"][sid].get("bootstrap_ok")]
    bad = [sid for sid in shard_ids if not fleet["shards"][sid].get("bootstrap_ok")]
    print(f"[bootstrap] {len(ok)} ok, {len(bad)} failed: {bad}")


def cmd_run(args):
    run_tag = args.run_tag
    fleet = load_fleet(run_tag)
    shard_ids = (
        args.only.split(",")
        if args.only
        else [sid for sid, info in fleet["shards"].items() if info.get("bootstrap_ok")]
    )

    src_head = fleet.get("memtrace_source_provenance", {}).get("head_sha", "unknown")[
        :12
    ]
    dirty_suffix = ""
    prov = fleet.get("memtrace_source_provenance", {})
    if prov.get("dirty_diff_sha256_16"):
        dirty_suffix = f"-dirty{prov['dirty_diff_sha256_16'][:8]}"
    lane = args.lane
    cache_policy_suffix = (
        "agent-hierarchy-v2-agent-hierarchy-v2"
        if lane == "codex"
        else "agent-hierarchy-v2"
    )
    cache_namespace = args.cache_namespace or (
        f"contextbench-src{src_head}{dirty_suffix}-jina-code-768-v2-"
        f"{cache_policy_suffix}"
    )
    concurrency = int(fleet.get("concurrency_per_shard", CONCURRENCY))
    agent_policy = (
        "codex-memtrace-skills-v1" if lane == "codex" else "hierarchy-listwise-v2"
    )
    projection_policy = (
        "codex-hierarchical-recall-floor-v1"
        if lane == "codex"
        else "rank-plus-scoped-recall-floor-v1"
    )
    agent_model = "gpt-5" if lane == "codex" else "openai/gpt-5"
    history_days = 0 if lane == "codex" else 30
    run_dataset = str(fleet.get("dataset_kind") or DATASET)
    if run_dataset not in {"verified", "full", "train", "test"}:
        raise RuntimeError(f"fleet has unsupported dataset_kind={run_dataset!r}")
    preflight_gate = None
    if run_dataset == "full":
        preflight_gate = validate_full_preflight_gate(
            fleet,
            FLEET_STATE_DIR / run_tag / "aggregate-preflight",
            cache_namespace,
        )
    fleet["run_treatment"] = {
        "dataset": run_dataset,
        "benchmark_lane": lane,
        "agent_policy": agent_policy,
        "projection_policy": projection_policy,
        "projection_variant": "precision-90" if lane == "codex" else None,
        "line_budget": LINE_BUDGET,
        "selector_model": SELECTOR_MODEL,
        "agent_model": agent_model,
        "history_days": history_days,
        "concurrency_per_shard": concurrency,
        "total_concurrency": concurrency * len(shard_ids),
        "cache_namespace": cache_namespace,
        "preflight_gate": preflight_gate,
    }
    save_fleet(run_tag, fleet)

    def run_one(sid):
        info = fleet["shards"][sid]
        ip = info["public_ip"]
        run_id = f"run-{run_tag}-{sid}-{lane}"
        info["run_id"] = run_id
        results_dir = f"/srv/contextbench/results/{run_id}"
        manifest_json = json.dumps(info["task_ids"])
        # Write manifest.json BEFORE launching so run-remote.sh does not
        # regenerate it from the FULL dataset (rule: disjoint per-shard slice).
        ssh_run(ip, f"mkdir -p {results_dir}")
        write_manifest = f"cat > {results_dir}/manifest.json <<'MANIFEST_EOF'\n{manifest_json}\nMANIFEST_EOF"
        ssh_run(ip, write_manifest)
        ssh_run(ip, f"printf '%s\\n' {run_id} > /srv/contextbench/results/LATEST")

        ntp = ssh_run(
            ip,
            "timedatectl show -p NTPSynchronized --value",
            timeout=20,
        ).stdout.strip()
        if ntp != "yes":
            raise RuntimeError(f"{sid} clock is not NTP-synchronized")
        info["ntp_synchronized"] = True

        inner = (
            f"set -e; "
            f"export OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' {REMOTE_ADAPTER_DIR}/.env | head -1 | cut -d= -f2-); "
            f"[ -n \"$OPENAI_API_KEY\" ] || {{ echo 'FATAL: OPENAI_API_KEY empty'; exit 1; }}; "
            f"BENCHMARK_LANE={lane} AGENT_MODEL={agent_model} AGENT_HISTORY_DAYS={history_days} "
            f"CB_SEARCH_LIMIT=100 CB_PACK_POLICY=v4 CB_QUERY_STRATEGY=v3 "
            f"POST_SELECTOR_POLICY=off DISK_FLOOR_GB=100 "
            f"RUN_ID={run_id} DATASET={run_dataset} CONCURRENCY={concurrency} LINE_BUDGET={LINE_BUDGET} "
            f"SELECTOR_MODEL={SELECTOR_MODEL} SELECTOR_MODE=default "
            f"CACHE_NAMESPACE={cache_namespace} RUN_TIMEOUT={RUN_TIMEOUT} "
            f"WATCHDOG_MINUTES={WATCHDOG_MINUTES} MEMTRACE_INSTALL_MODE=source "
            f"bash {REMOTE_ADAPTER_DIR}/aws/remote/run-remote.sh"
        )
        import shlex

        tmux_cmd = "tmux new-session -d -s contextbench " + shlex.quote(inner)
        r = ssh_run(ip, tmux_cmd, check=False)
        info["run_launch_rc"] = r.returncode
        info["run_launched_at"] = time.time()
        print(f"  {sid}: launched run_id={run_id} rc={r.returncode} {r.stderr[:200]}")
        return sid, info

    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futs = [ex.submit(run_one, sid) for sid in shard_ids]
        for fut in as_completed(futs):
            sid, info = fut.result()
            fleet["shards"][sid] = info
            save_fleet(run_tag, fleet)
    print(f"[run] launched {len(shard_ids)} shards")


def cmd_poll(args):
    run_tag = args.run_tag
    fleet = load_fleet(run_tag)
    shard_ids = [sid for sid, info in fleet["shards"].items() if info.get("run_id")]

    def poll_one(sid):
        info = fleet["shards"][sid]
        ip = info["public_ip"]
        run_id = info["run_id"]
        results_dir = f"/srv/contextbench/results/{run_id}"
        total = len(info["task_ids"])
        r = ssh_run(
            ip,
            f"find {results_dir}/runs -mindepth 2 -maxdepth 2 -name prediction.jsonl -size +0c 2>/dev/null | wc -l",
            check=False,
            timeout=20,
        )
        completed = int(r.stdout.strip() or 0) if r.returncode == 0 else -1
        alive = ssh_run(
            ip,
            "tmux has-session -t contextbench 2>/dev/null && echo yes || echo no",
            check=False,
            timeout=20,
        )
        session_alive = alive.stdout.strip() == "yes"
        return sid, completed, total, session_alive

    done_shards = 0
    total_completed = 0
    total_tasks = 0
    lines = []
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futs = [ex.submit(poll_one, sid) for sid in shard_ids]
        for fut in as_completed(futs):
            sid, completed, total, alive = fut.result()
            total_tasks += total
            total_completed += max(completed, 0)
            status = (
                "RUNNING" if alive else ("DONE" if completed >= total else "STOPPED(!)")
            )
            if status != "RUNNING":
                done_shards += 1
            lines.append((sid, completed, total, status))
    lines.sort()
    for sid, completed, total, status in lines:
        print(f"  {sid}: {completed}/{total} {status}")
    print(
        f"[poll] total {total_completed}/{total_tasks} predictions, {done_shards}/{len(shard_ids)} shards not-running"
    )


def cmd_preflight(args):
    run_tag = args.run_tag
    fleet = load_fleet(run_tag)
    shard_ids = args.only.split(",") if args.only else list(fleet["shards"])
    run_dataset = str(fleet.get("dataset_kind") or dataset_kind(args.dataset))
    dataset = remote_dataset_path(run_dataset)
    concurrency = int(fleet.get("concurrency_per_shard", args.concurrency))
    cache_namespace = args.cache_namespace or (
        fleet.get("run_treatment", {}).get("cache_namespace")
        or "contextbench-src3e9a814f7ac5-jina-code-768-v2-agent-hierarchy-v2-agent-hierarchy-v2"
    )
    fleet["preflight_treatment"] = {
        "dataset": run_dataset,
        "workers_per_shard": concurrency,
        "total_workers": concurrency * len(shard_ids),
        "cache_namespace": cache_namespace,
        "history_days": 0,
        "task_timeout": args.task_timeout,
    }
    save_fleet(run_tag, fleet)

    def launch_one(sid):
        info = fleet["shards"][sid]
        ip = info["public_ip"]
        run_id = f"preflight-{run_tag}-{sid}"
        results_dir = f"/srv/contextbench/preflight/{run_id}"
        manifest_json = json.dumps(info["task_ids"])
        expected_source = str(
            fleet.get("memtrace_source_provenance", {}).get("head_sha") or ""
        )
        remote_source = ssh_run(
            ip,
            "python3 -c 'import json; "
            "print(json.load(open(\"/srv/contextbench/memtrace-bin/source-manifest.json\"))[\"head_sha\"])'",
            timeout=20,
        ).stdout.strip()
        if not expected_source or remote_source != expected_source:
            raise RuntimeError(
                f"{sid} source mismatch: fleet expects {expected_source}, host has {remote_source}"
            )
        ssh_run(
            ip,
            f"test -s {dataset} && test -x /srv/contextbench/memtrace-bin/memtrace "
            "&& test -d /srv/contextbench/rerank-model",
            timeout=20,
        )
        ssh_run(ip, f"mkdir -p {REMOTE_ADAPTER_DIR} {results_dir}")
        rsync_to(
            ip,
            str(ADAPTER_DIR) + "/",
            REMOTE_ADAPTER_DIR + "/",
            extra=[
                "--exclude",
                ".env",
                "--exclude",
                "__pycache__",
                "--exclude",
                ".DS_Store",
                "--exclude",
                "aws/state",
                "--exclude",
                "aws/config.env",
                "--exclude",
                "work/",
            ],
        )
        write_manifest = (
            f"cat > {results_dir}/manifest.json <<'MANIFEST_EOF'\n"
            f"{manifest_json}\nMANIFEST_EOF"
        )
        ssh_run(ip, write_manifest)
        session = "contextbench-preflight"
        benchmark_alive = ssh_run(
            ip,
            "tmux has-session -t contextbench 2>/dev/null",
            check=False,
            timeout=20,
        )
        if benchmark_alive.returncode == 0:
            raise RuntimeError(f"{sid} still has an active scored-run session")
        alive = ssh_run(
            ip,
            f"tmux has-session -t {session} 2>/dev/null",
            check=False,
            timeout=20,
        )
        if alive.returncode == 0:
            raise RuntimeError(f"{sid} already has an active {session} session")
        inner = (
            "set -o pipefail; "
            "MEMTRACE_PIN_ENABLE=1 "
            "MEMTRACE_PIN_WAIT_SECONDS=900 "
            "MEMTRACE_DEV=1 MEMTRACE_TELEMETRY=off MEMTRACE_CORTEX=off "
            f"/srv/contextbench/venv/bin/python {REMOTE_ADAPTER_DIR}/aws/full_preflight_runner.py "
            f"--dataset {shlex.quote(dataset)} "
            f"--manifest {shlex.quote(results_dir + '/manifest.json')} "
            f"--output-dir {shlex.quote(results_dir)} "
            "--graph-cache-dir /srv/contextbench/graph-cache-agent "
            f"--cache-namespace {shlex.quote(cache_namespace)} "
            "--memtrace-binary /srv/contextbench/memtrace-bin/memtrace "
            "--rerank-model-dir /srv/contextbench/rerank-model "
            f"--workers {concurrency} --task-timeout {args.task_timeout} --resume "
            f"--host {shlex.quote(ip)} --run-id {shlex.quote(run_id)} "
            f"2>&1 | tee -a {shlex.quote(results_dir + '/preflight.log')}; "
            "rc=${PIPESTATUS[0]}; "
            f"printf '%s\\n' \"$rc\" > {shlex.quote(results_dir + '/exit_code')}; "
            "exit \"$rc\""
        )
        launched = ssh_run(
            ip,
            f"tmux new-session -d -s {session} {shlex.quote(inner)}",
            check=False,
        )
        info["preflight_run_id"] = run_id
        info["preflight_launch_rc"] = launched.returncode
        info["preflight_launched_at"] = time.time()
        if launched.returncode != 0:
            raise RuntimeError(
                f"{sid} failed to launch preflight tmux: {launched.stderr.strip()}"
            )
        print(f"  {sid}: launched {run_id} rc={launched.returncode}")
        return sid, info

    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = [executor.submit(launch_one, sid) for sid in shard_ids]
        for future in as_completed(futures):
            sid, info = future.result()
            fleet["shards"][sid] = info
            save_fleet(run_tag, fleet)
    print(f"[preflight] launched {len(shard_ids)} shards ({concurrency * len(shard_ids)} workers)")


def preflight_stop_command(run_id, session="contextbench-preflight"):
    return (
        "set -eu; "
        f"session={shlex.quote(session)}; expected={shlex.quote(run_id)}; "
        'if ! tmux has-session -t "$session" 2>/dev/null; then '
        'echo "already-stopped run=$expected"; exit 0; fi; '
        'pane=$(tmux list-panes -t "$session" -F "#{pane_pid}" | head -1); '
        'cmd=$(tr "\\0" " " < "/proc/$pane/cmdline"); '
        'case "$cmd" in *"$expected"*) ;; *) '
        'echo "refusing: session does not match $expected" >&2; exit 42;; esac; '
        'pgid=$(ps -o pgid= -p "$pane" | tr -d " "); '
        'test -n "$pgid"; '
        'kill -TERM -- "-$pgid" 2>/dev/null || true; '
        'sleep 5; '
        'kill -KILL -- "-$pgid" 2>/dev/null || true; '
        'tmux kill-session -t "$session" 2>/dev/null || true; '
        'echo "stopped run=$expected pgid=$pgid"'
    )


def cmd_stop_preflight(args):
    fleet = load_fleet(args.run_tag)
    shard_ids = args.only.split(",") if args.only else list(fleet["shards"])

    def stop_one(sid):
        info = fleet["shards"][sid]
        run_id = info.get("preflight_run_id")
        if not run_id:
            raise RuntimeError(f"{sid} has no preflight run to stop")
        result = ssh_run(
            info["public_ip"],
            preflight_stop_command(run_id),
            timeout=30,
        )
        return sid, result.stdout.strip()

    stopped = []
    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = [executor.submit(stop_one, sid) for sid in shard_ids]
        for future in as_completed(futures):
            sid, detail = future.result()
            stopped.append((sid, detail))
    for sid, detail in sorted(stopped):
        fleet["shards"][sid]["preflight_stopped_at"] = time.time()
        fleet["shards"][sid]["preflight_stop_detail"] = detail
        print(f"  {sid}: {detail}")
    save_fleet(args.run_tag, fleet)
    print(f"[stop-preflight] stopped {len(stopped)} shards")


def cmd_poll_preflight(args):
    fleet = load_fleet(args.run_tag)
    shard_ids = [sid for sid, info in fleet["shards"].items() if info.get("preflight_run_id")]

    def poll_one(sid):
        info = fleet["shards"][sid]
        ip = info["public_ip"]
        run_id = info["preflight_run_id"]
        results_dir = f"/srv/contextbench/preflight/{run_id}"
        output = ssh_run(
            ip,
            f"success=$(grep -l '\"status\": \"success\"' {results_dir}/records/*.json 2>/dev/null | wc -l); "
            f"failure=$(grep -l '\"status\": \"failure\"' {results_dir}/records/*.json 2>/dev/null | wc -l); "
            f"running=$(grep -l '\"status\": \"running\"' {results_dir}/records/*.json 2>/dev/null | wc -l); "
            "printf '%s %s %s\\n' \"$success\" \"$running\" \"$failure\"",
            check=False,
            timeout=30,
        )
        counts = output.stdout.strip().split()
        success, running, failure = (
            (int(counts[0]), int(counts[1]), int(counts[2]))
            if len(counts) == 3
            else (-1, -1, -1)
        )
        alive = ssh_run(
            ip,
            "tmux has-session -t contextbench-preflight 2>/dev/null && echo yes || echo no",
            check=False,
            timeout=20,
        ).stdout.strip() == "yes"
        return sid, success, running, failure, len(info["task_ids"]), alive

    totals = [0, 0, 0, 0]
    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = [executor.submit(poll_one, sid) for sid in shard_ids]
        for future in as_completed(futures):
            sid, success, running, failure, total, alive = future.result()
            totals[0] += max(success, 0)
            totals[1] += max(running, 0)
            totals[2] += max(failure, 0)
            totals[3] += total
            state = "RUNNING" if alive else ("DONE" if success + failure == total else "STOPPED(!)")
            print(
                f"  {sid}: pass={success} running={running} fail={failure} "
                f"total={total} {state}"
            )
    print(
        f"[poll-preflight] pass={totals[0]} running={totals[1]} fail={totals[2]} "
        f"terminal={totals[0] + totals[2]}/{totals[3]}"
    )


def preflight_records_ready(probe, sid):
    probe = probe.strip()
    if probe == "ready":
        return True
    if probe == "pending":
        return False
    if probe.startswith("terminal:"):
        raise RuntimeError(
            f"preflight {sid} terminated without a records directory ({probe})"
        )
    raise RuntimeError(f"unexpected preflight records probe for {sid}: {probe!r}")


def preflight_record_counts(records):
    return {
        status: sum(record.get("status") == status for record in records)
        for status in ("success", "failure", "running")
    }


def preflight_repair_exclusions(run_tags):
    excluded = set()
    for run_tag in run_tags:
        state_path = fleet_path(run_tag)
        if not state_path.exists():
            raise RuntimeError(f"repair fleet state is missing: {state_path}")
        repair_fleet = json.loads(state_path.read_text())
        source_manifest = [
            str(instance_id)
            for instance_id in repair_fleet.get("source_manifest") or []
        ]
        if not source_manifest:
            raise RuntimeError(f"repair fleet has an empty source manifest: {run_tag}")
        excluded.update(source_manifest)
    return excluded


def unresolved_preflight_failures(records, excluded_task_ids=()):
    excluded = {str(instance_id) for instance_id in excluded_task_ids}
    return sorted(
        {
            str(record.get("instance_id") or "")
            for record in records
            if record.get("status") == "failure"
            and record.get("instance_id")
            and str(record["instance_id"]) not in excluded
        }
    )


def validate_full_preflight_gate(fleet, aggregate, cache_namespace):
    manifest = [str(value) for value in fleet.get("source_manifest") or []]
    if not manifest or len(manifest) != len(set(manifest)):
        raise RuntimeError("full-run preflight gate requires a non-empty unique manifest")
    treatment = fleet.get("preflight_treatment") or {}
    expected_treatment = {
        "dataset": "full",
        "cache_namespace": cache_namespace,
        "history_days": 0,
    }
    treatment_mismatches = {
        key: (treatment.get(key), value)
        for key, value in expected_treatment.items()
        if treatment.get(key) != value
    }
    if treatment_mismatches:
        raise RuntimeError(
            f"full-run preflight treatment mismatch: {treatment_mismatches}"
        )

    summary_path = aggregate / "summary.json"
    records_dir = aggregate / "records"
    if not summary_path.is_file() or not records_dir.is_dir():
        raise RuntimeError(f"full-run preflight evidence is missing under {aggregate}")
    summary = json.loads(summary_path.read_text())
    expected_total = len(manifest)
    expected_summary = {
        "total": expected_total,
        "records": expected_total,
        "terminal_records": expected_total,
        "succeeded": expected_total,
        "failed": 0,
        "running": 0,
    }
    summary_mismatches = {
        key: (summary.get(key), value)
        for key, value in expected_summary.items()
        if summary.get(key) != value
    }
    if summary_mismatches:
        raise RuntimeError(
            f"full-run preflight summary has not passed: {summary_mismatches}"
        )

    records = {}
    for path in records_dir.glob("*.json"):
        record = json.loads(path.read_text())
        instance_id = str(record.get("instance_id") or "")
        if not instance_id or instance_id in records:
            raise RuntimeError(f"invalid or duplicate preflight record: {path}")
        records[instance_id] = record
    expected_ids = set(manifest)
    missing = sorted(expected_ids - set(records))
    unexpected = sorted(set(records) - expected_ids)
    if missing or unexpected:
        raise RuntimeError(
            f"full-run preflight record set mismatch: missing={missing[:5]} "
            f"unexpected={unexpected[:5]}"
        )
    required_stages = (
        "dataset",
        "checkout",
        "index_embeddings",
        "cache_sealed",
        "mcp",
    )
    failures = []
    for instance_id in manifest:
        record = records[instance_id]
        stages = record.get("stages") or {}
        failed_stages = [
            stage
            for stage in required_stages
            if (stages.get(stage) or {}).get("status") != "PASS"
        ]
        if record.get("status") != "success" or failed_stages:
            failures.append(
                {
                    "instance_id": instance_id,
                    "status": record.get("status"),
                    "failed_stages": failed_stages,
                }
            )
    if failures:
        raise RuntimeError(
            f"full-run preflight has {len(failures)} invalid task records: "
            f"{failures[:5]}"
        )
    return {
        "total": expected_total,
        "summary": str(summary_path),
        "cache_namespace": cache_namespace,
    }


def dataset_task_count(dataset_path):
    import pandas as pd

    return len(pd.read_parquet(dataset_path, columns=["instance_id"]))


def ensure_full_tracker_dataset(dataset_path, durable_path=None):
    """Persist the full tracker dataset outside purge-prone temporary storage."""
    source = Path(dataset_path)
    if source.name != "full.parquet":
        raise ValueError(f"tracker dataset must be full.parquet, got {source}")
    durable = Path(
        durable_path
        or AWS_DIR / "state" / "preflight" / "full-1136" / "full.parquet"
    )
    if source.exists():
        if durable.exists():
            if sha256_file(source) != sha256_file(durable):
                raise RuntimeError(
                    f"full tracker dataset mismatch: {source} differs from {durable}"
                )
        else:
            durable.parent.mkdir(parents=True, exist_ok=True)
            temporary = durable.with_name(
                f"{durable.name}.{os.getpid()}.{time.time_ns()}.tmp"
            )
            try:
                shutil.copy2(source, temporary)
                temporary.replace(durable)
            finally:
                temporary.unlink(missing_ok=True)
    if not durable.exists():
        raise RuntimeError(
            "full ContextBench tracker dataset is missing from both "
            f"{source} and durable snapshot {durable}"
        )
    return durable


def cmd_collect_preflight(args):
    fleet = load_fleet(args.run_tag)
    aggregate = FLEET_STATE_DIR / args.run_tag / "aggregate-preflight"
    records_dir = aggregate / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    seen = {}
    for sid, info in fleet["shards"].items():
        run_id = info.get("preflight_run_id")
        if not run_id:
            continue
        local = FLEET_STATE_DIR / args.run_tag / "pulled-preflight" / sid
        local.mkdir(parents=True, exist_ok=True)
        results_dir = f"/srv/contextbench/preflight/{run_id}"
        remote = f"{results_dir}/records/"
        probe = ssh_run(
            info["public_ip"],
            f"if [ -d {remote} ]; then echo ready; "
            f"elif [ -f {results_dir}/exit_code ]; then "
            f"printf 'terminal:%s\\n' \"$(cat {results_dir}/exit_code)\"; "
            "else echo pending; fi",
            check=False,
            timeout=30,
        )
        if probe.returncode != 0:
            raise RuntimeError(
                f"preflight records probe failed for {sid}: ssh rc={probe.returncode}"
            )
        if not preflight_records_ready(probe.stdout, sid):
            print(f"  {sid}: collected 0 records (workers still starting)")
            continue
        result = sh(
            [
                "rsync",
                "-az",
                "--delete",
                "-e",
                "ssh " + " ".join(SSH_OPTS),
                f"{REMOTE_USER}@{info['public_ip']}:{remote}",
                str(local) + "/",
            ],
            check=False,
            timeout=1800,
        )
        if result.returncode != 0:
            raise RuntimeError(f"preflight collection failed for {sid}: rsync rc={result.returncode}")
        for record_path in sorted(local.glob("*.json")):
            record = json.loads(record_path.read_text())
            instance_id = str(record.get("instance_id") or "")
            if instance_id in seen:
                raise RuntimeError(f"duplicate preflight record for {instance_id}")
            if instance_id not in set(fleet.get("source_manifest") or []):
                raise RuntimeError(f"preflight record outside source manifest: {instance_id}")
            destination = records_dir / record_path.name
            shutil.copy2(record_path, destination)
            seen[instance_id] = record
        print(f"  {sid}: collected {len(list(local.glob('*.json')))} records")
    counts = preflight_record_counts(seen.values())
    success = counts["success"]
    failure = counts["failure"]
    running = counts["running"]
    write_json_atomic(
        aggregate / "summary.json",
        {
            "schema_version": 1,
            "total": len(fleet.get("source_manifest") or []),
            "records": len(seen),
            "terminal_records": success + failure,
            "succeeded": success,
            "failed": failure,
            "running": running,
            "captured_at_unix_ns": time.time_ns(),
        },
    )
    repair_manifest_out = (
        getattr(args, "repair_manifest_out", "")
        or fleet.get("preflight_repair_manifest_out")
        or ""
    )
    repair_run_tags = [
        run_tag
        for run_tag in (
            getattr(args, "repair_exclude_run_tags", "")
            or ",".join(fleet.get("preflight_repair_exclude_run_tags") or [])
        ).split(",")
        if run_tag
    ]
    if repair_manifest_out:
        excluded_task_ids = preflight_repair_exclusions(repair_run_tags)
        repair_task_ids = unresolved_preflight_failures(
            seen.values(), excluded_task_ids
        )
        write_json_atomic(Path(repair_manifest_out), repair_task_ids)
        print(
            f"  repair manifest: {len(repair_task_ids)} tasks -> "
            f"{repair_manifest_out}"
        )
    tracker_dataset = Path(fleet.get("dataset_path_local") or args.dataset)
    tracker_state = AWS_DIR / "state" / "preflight" / "full-1136" / "task-status.jsonl"
    tracker = ADAPTER_DIR / "TRACKER.md"
    if tracker_dataset.name == "full.parquet":
        tracker_dataset = ensure_full_tracker_dataset(tracker_dataset)
        sh(
            [
                sys.executable,
                str(AWS_DIR / "full_preflight_tracker.py"),
                "--dataset",
                str(tracker_dataset),
                "--state",
                str(tracker_state),
                "--tracker",
                str(tracker),
                "--expected-total",
                str(dataset_task_count(tracker_dataset)),
                "import-preflight",
                "--records",
                str(records_dir),
            ]
        )
    print(
        f"[collect-preflight] pass={success} running={running} fail={failure} "
        f"terminal={success + failure}/{len(fleet.get('source_manifest') or [])}"
    )


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


def cmd_collect(args):
    run_tag = args.run_tag
    fleet = load_fleet(run_tag)
    # A warm fleet can host more than one sealed treatment over its lifetime
    # (for example the rejected agent lane followed by the Codex lane). Keep
    # local mirrors and immutable checkpoint receipts lane-scoped so an older
    # terminal-0010.json can never be mistaken for the current treatment.
    aggregate = AWS_DIR / "state" / "fleet" / run_tag / f"aggregate-{args.lane}"
    aggregate_runs = aggregate / "runs"
    aggregate_runs.mkdir(parents=True, exist_ok=True)
    shard_ids = [sid for sid, info in fleet["shards"].items() if info.get("run_id")]

    def pull_one(sid):
        info = fleet["shards"][sid]
        ip = info["public_ip"]
        run_id = info["run_id"]
        results_dir = f"/srv/contextbench/results/{run_id}"
        local_dir = (
            AWS_DIR / "state" / "fleet" / run_tag / f"pulled-{args.lane}" / sid
        )
        local_dir.mkdir(parents=True, exist_ok=True)
        # Pull only immutable terminal/evaluation artifacts. Worktrees and
        # generated agent configs are both huge and may contain ephemeral
        # bridge credentials, so they never cross back to the operator host.
        r = sh(
            [
                "rsync",
                "-az",
                "--delete",
                "--include=/manifest.json",
                "--include=/runs/",
                "--include=/runs/*/",
                "--include=/runs/*/prediction.jsonl",
                "--include=/runs/*/prediction-audit/",
                "--include=/runs/*/prediction-audit/*.json",
                "--include=/runs/*/query-plan.json",
                "--include=/runs/*/run_record.json",
                "--include=/runs/*/failure.json",
                "--include=/runs/*/runner.log",
                "--exclude=*",
                "-e",
                "ssh " + " ".join(SSH_OPTS),
                f"{REMOTE_USER}@{ip}:{results_dir}/",
                str(local_dir) + "/",
            ],
            check=False,
            timeout=1800,
        )
        return sid, local_dir, r.returncode

    pulled = []
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futs = [ex.submit(pull_one, sid) for sid in shard_ids]
        for fut in as_completed(futs):
            sid, local_dir, rc = fut.result()
            if rc != 0:
                raise RuntimeError(
                    f"artifact collection failed for {sid}: rsync rc={rc}"
                )
            pulled.append((sid, local_dir))
            print(f"  {sid}: terminal artifacts refreshed")

    source_manifest = list(fleet.get("source_manifest") or [])
    if not source_manifest:
        raise RuntimeError("fleet state has no source_manifest")
    manifest_index = {
        instance_id: index for index, instance_id in enumerate(source_manifest)
    }
    terminals_by_id = {}
    for sid, local_dir in pulled:
        runs_src = local_dir / "runs"
        if not runs_src.is_dir():
            continue
        for slug_dir in runs_src.iterdir():
            record_path = slug_dir / "run_record.json"
            if not record_path.is_file():
                continue
            record = json.loads(record_path.read_text())
            instance_id = str(record.get("instance_id") or "")
            completed_at = record.get("completed_at_unix_ns")
            if (
                instance_id not in manifest_index
                or record.get("status") not in {"success", "failure"}
                or not isinstance(completed_at, int)
            ):
                raise RuntimeError(f"malformed fleet terminal record: {record_path}")
            if instance_id in terminals_by_id:
                raise RuntimeError(
                    f"duplicate terminal task across shards: {instance_id}"
                )
            destination = aggregate_runs / slug_dir.name
            shutil.copytree(slug_dir, destination, dirs_exist_ok=True)
            terminals_by_id[instance_id] = {
                "instance_id": instance_id,
                "slug": slug_dir.name,
                "manifest_index": manifest_index[instance_id],
                "completed_at_unix_ns": completed_at,
                "status": record["status"],
                "shard_id": sid,
            }

    terminals = sorted(
        terminals_by_id.values(),
        key=lambda terminal: (
            terminal["completed_at_unix_ns"],
            terminal["manifest_index"],
        ),
    )
    write_json_atomic(aggregate / "manifest.json", source_manifest)
    write_json_atomic(aggregate / "completion-order.json", terminals)

    prediction_rows = []
    for instance_id in source_manifest:
        terminal = terminals_by_id.get(instance_id)
        if terminal:
            prediction = aggregate_runs / terminal["slug"] / "prediction.jsonl"
            prediction_rows.append(prediction.read_text().strip())
    (aggregate / "predictions.jsonl").write_text(
        "\n".join(prediction_rows) + ("\n" if prediction_rows else "")
    )

    snapshots = aggregate / "snapshots"
    snapshots.mkdir(exist_ok=True)
    for count in range(10, len(terminals) + 1, 10):
        snapshot_path = snapshots / f"terminal-{count:04d}.json"
        if snapshot_path.exists():
            continue
        selected = terminals[:count]
        files = []
        for terminal in selected:
            run_dir = aggregate_runs / terminal["slug"]
            for artifact in (
                run_dir / "prediction.jsonl",
                run_dir / "prediction-audit" / f"{terminal['slug']}.json",
                run_dir / "query-plan.json",
                run_dir / "run_record.json",
            ):
                if artifact.is_file():
                    files.append(
                        {
                            "path": artifact.relative_to(aggregate).as_posix(),
                            "size": artifact.stat().st_size,
                            "sha256": sha256_file(artifact),
                        }
                    )
        write_json_atomic(
            snapshot_path,
            {
                "schema_version": 1,
                "run_id": f"fleet-{run_tag}-{args.lane}",
                "manifest_sha256": sha256_file(aggregate / "manifest.json"),
                "captured_at_unix_ns": time.time_ns(),
                "terminals": selected,
                "files": files,
            },
        )
        print(f"[collect] sealed immutable N{count} snapshot: {snapshot_path}")

    failures = [terminal for terminal in terminals if terminal["status"] == "failure"]
    print(
        f"[collect] {len(terminals)}/{len(source_manifest)} terminals, "
        f"{len(failures)} failures -> {aggregate}"
    )


def cmd_terminate(args):
    run_tag = args.run_tag
    fleet = load_fleet(run_tag)
    ids = [
        info["instance_id"]
        for info in fleet["shards"].values()
        if info.get("instance_id")
    ]
    if not ids:
        print("[terminate] no instances recorded")
        return
    print(f"[terminate] terminating {len(ids)} instances")
    for i in range(0, len(ids), 50):
        aws(["ec2", "terminate-instances", "--instance-ids"] + ids[i : i + 50])
    aws(["ec2", "wait", "instance-terminated", "--instance-ids"] + ids)
    print("[terminate] all instances confirmed terminated")

    vol_ids = [
        info["volume_id"] for info in fleet["shards"].values() if info.get("volume_id")
    ]
    print(f"[terminate] deleting {len(vol_ids)} shard data volumes")
    for vid in vol_ids:
        r = aws(["ec2", "delete-volume", "--volume-id", vid], check=False)
        if r.returncode != 0:
            print(f"  warn: could not delete {vid}: {r.stderr.strip()[:200]}")
    print("[terminate] done")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "cmd",
        choices=[
            "adopt",
            "provision",
            "bootstrap",
            "preflight",
            "stop-preflight",
            "poll-preflight",
            "collect-preflight",
            "run",
            "poll",
            "collect",
            "terminate",
        ],
    )
    ap.add_argument("--run-tag", default="agent-n100-fullpower")
    ap.add_argument("--shards", type=int, default=5)
    ap.add_argument(
        "--dataset", default=str(DATA_LOCAL / "contextbench_verified.parquet")
    )
    ap.add_argument("--manifest", default="")
    ap.add_argument("--adopt-state", default="")
    ap.add_argument("--instance-type", default=INSTANCE_TYPE)
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    ap.add_argument("--root-volume-gb", type=int, default=ROOT_VOLUME_GB)
    ap.add_argument("--data-volume-gb", type=int, default=DATA_VOLUME_GB)
    ap.add_argument("--only", default="")
    ap.add_argument("--parallel", type=int, default=5)
    ap.add_argument("--task-timeout", type=int, default=7200)
    ap.add_argument("--repair-manifest-out", default="")
    ap.add_argument("--repair-exclude-run-tags", default="")
    ap.add_argument(
        "--lane",
        choices=("agent", "codex"),
        default="codex",
        help="coding-agent runtime used by the run command",
    )
    ap.add_argument(
        "--cache-namespace",
        default="",
        help=(
            "explicit compatible graph-cache namespace; use this to reuse indexed "
            "embeddings across runner-only Memtrace changes"
        ),
    )
    args = ap.parse_args()
    if args.shards < 1 or args.concurrency < 1:
        ap.error("--shards and --concurrency must be positive")
    {
        "adopt": cmd_adopt,
        "provision": cmd_provision,
        "bootstrap": cmd_bootstrap,
        "preflight": cmd_preflight,
        "stop-preflight": cmd_stop_preflight,
        "poll-preflight": cmd_poll_preflight,
        "collect-preflight": cmd_collect_preflight,
        "run": cmd_run,
        "poll": cmd_poll,
        "collect": cmd_collect,
        "terminate": cmd_terminate,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
