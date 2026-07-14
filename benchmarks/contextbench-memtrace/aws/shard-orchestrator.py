#!/usr/bin/env python3
"""shard-orchestrator.py — horizontal-sharding launcher for the ContextBench
AWS harness. Built 2026-07-12 after a program of single-box runs proved the
per-process thread-pool bug in `memtrace mcp` makes >1 concurrent task on one
box unsafe (load 400-560 observed on 96 cores). The only safe pattern is many
separate boxes, each running tasks SEQUENTIALLY (concurrency=1).

This reuses the existing aws/remote/*.sh building blocks (bootstrap-remote.sh,
build-memtrace-remote.sh, run-remote.sh) but drives them directly via ssh/rsync
per shard instead of through the single-box 01-03 scripts, because those
scripts hardcode one STATE_DIR / one persistent data volume.

Usage:
    python3 shard-orchestrator.py provision   --shards 39 --shard-size 13
    python3 shard-orchestrator.py bootstrap
    python3 shard-orchestrator.py run
    python3 shard-orchestrator.py poll
    python3 shard-orchestrator.py collect
    python3 shard-orchestrator.py teardown

State for the whole fleet lives in aws/state/fleet/<RUN_TAG>/fleet.json.
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time, hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

AWS_DIR = Path(__file__).resolve().parent
ADAPTER_DIR = AWS_DIR.parent
REPO_ROOT = ADAPTER_DIR.parent.parent  # memtrace-public
ENV_FILE = REPO_ROOT / ".env"
MEMTRACE_SOURCE_DIR = Path("/Users/alexholmberg/Desktop/Memtrace/memtrace")
DATA_LOCAL = Path("/tmp/contextbench/data")
FLEET_STATE_DIR = AWS_DIR / "state" / "fleet"

AWS_REGION = "us-east-1"
# 256 vCPU account quota (Running On-Demand Standard A/C/D/H/I/M/R/T/Z) is a
# HARD wall discovered live on 2026-07-12 — a quota-increase request is an
# out-of-scope account-level change and was correctly blocked. Given that
# fixed ceiling, box SIZE trades against box COUNT: c7a.2xlarge (8 vCPU) x 32
# boxes uses the full 256 vCPU as 32 genuinely-parallel single-task workers,
# vs c7a.8xlarge (32 vCPU) x 8. Smaller boxes run any one CPU-bound task
# somewhat slower (rayon/cargo-build parallelism scales with cores) but
# parallelism count is what buys wall-clock under the <2hr target — concurrency
# stays 1 per box either way, so the thread-pool bug (rule 1) is not
# reintroduced by shrinking the box, only single-task speed is traded.
INSTANCE_TYPE = "c7a.2xlarge"   # 8 vCPU
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
DATA_VOLUME_GB = 80   # fewer tasks/box at higher shard count -> less disk needed
ROOT_VOLUME_GB = 40

# Known-slow repos from today's real driver_summary.json samples (median 155s,
# mean 823s, max 3478s=58min across only 20 samples — small n, but the only
# real signal available) plus repos flagged slow in program memory (ansible,
# vscode, angular, material-ui/mui, prettier — prettier is known to HANG
# memtrace mcp reproducibly). Used only to bias bin-packing so no single box
# gets stuck with multiple giant tasks while others idle; not a precise cost
# model.
SLOW_REPO_SUBSTRINGS = [
    "ansible", "vscode", "angular", "mui/material", "prettier",
]
DEFAULT_TASK_COST_S = 155.0   # observed median
SLOW_TASK_COST_S = 1800.0     # ~30min, conservative for flagged-slow repos

SSH_OPTS = [
    "-i", SSH_KEY_PATH,
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=30",
    "-o", "BatchMode=yes",
]


def sh(cmd, check=True, capture=True, timeout=None):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=capture,
                        text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd failed ({r.returncode}): {cmd}\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}")
    return r


def aws(args, check=True):
    return sh(["aws"] + args + ["--region", AWS_REGION], check=check)


def ssh_run(ip, remote_cmd, timeout=None, check=True):
    return sh(["ssh"] + SSH_OPTS + [f"{REMOTE_USER}@{ip}", remote_cmd],
              check=check, timeout=timeout)


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


def build_shards(n_shards, dataset_path):
    import pandas as pd
    df = pd.read_parquet(dataset_path, columns=["instance_id", "repo"])
    df = df.sort_values(["repo", "instance_id"]).reset_index(drop=True)
    tasks = [(row.instance_id, estimate_task_cost_s(row.repo)) for row in df.itertuples()]
    # Longest-Processing-Time-first greedy bin packing: sort tasks by
    # estimated cost descending, always place the next task on the
    # currently-least-loaded shard. This is the single biggest lever against
    # missing the wall-clock target — naive round-robin can stack multiple
    # known-slow-repo tasks (ansible/vscode/angular/mui/prettier) onto the
    # same box while others sit idle. Deterministic tie-break on repo+id
    # (already sorted) keeps runs reproducible.
    tasks.sort(key=lambda t: -t[1])
    loads = [0.0] * n_shards
    shards = [[] for _ in range(n_shards)]
    for iid, cost in tasks:
        i = min(range(n_shards), key=lambda k: loads[k])
        shards[i].append(iid)
        loads[i] += cost
    print(f"  LPT bin-pack: estimated per-shard load (s) min={min(loads):.0f} max={max(loads):.0f} "
          f"mean={sum(loads)/len(loads):.0f}")
    return shards


def local_git_provenance():
    def g(*args):
        return sh(["git", "--no-optional-locks", "-C", str(MEMTRACE_SOURCE_DIR)] + list(args)).stdout.strip()
    head = g("rev-parse", "HEAD")
    describe = g("describe", "--tags", "--dirty", "--always")
    dirty_out = sh(["git", "--no-optional-locks", "-C", str(MEMTRACE_SOURCE_DIR), "status", "--porcelain"]).stdout
    dirty_count = len([l for l in dirty_out.splitlines() if l.strip()])
    diff_sha = ""
    if dirty_count:
        diff = sh(["git", "--no-optional-locks", "-C", str(MEMTRACE_SOURCE_DIR), "diff", "HEAD"]).stdout
        diff_sha = hashlib.sha256(diff.encode()).hexdigest()[:16]
    return {"head_sha": head, "git_describe": describe, "dirty_file_count": dirty_count,
            "dirty_diff_sha256_16": diff_sha}


# ---------------------------------------------------------------------------
def cmd_provision(args):
    run_tag = args.run_tag
    fleet = load_fleet(run_tag)

    print(f"[provision] resolving AMI/SG/keypair (shared across shards)")
    ami = aws(["ssm", "get-parameters", "--names",
               "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
               "--query", "Parameters[0].Value", "--output", "text"]).stdout.strip()
    print(f"  ami={ami}")

    vpc = aws(["ec2", "describe-vpcs", "--filters", "Name=isDefault,Values=true",
               "--query", "Vpcs[0].VpcId", "--output", "text"]).stdout.strip()
    sg_name = f"contextbench-shard-ssh"
    r = aws(["ec2", "describe-security-groups", "--filters",
             f"Name=group-name,Values={sg_name}", f"Name=vpc-id,Values={vpc}",
             "--query", "SecurityGroups[0].GroupId", "--output", "text"], check=False)
    sg_id = r.stdout.strip()
    if not sg_id or sg_id == "None":
        sg_id = aws(["ec2", "create-security-group", "--group-name", sg_name,
                     "--description", "ContextBench shard fleet: SSH from operator IP",
                     "--vpc-id", vpc,
                     "--tag-specifications", f"ResourceType=security-group,Tags=[{{Key=Name,Value={sg_name}}}]",
                     "--query", "GroupId", "--output", "text"]).stdout.strip()
        print(f"  created SG {sg_id}")
    else:
        print(f"  reusing SG {sg_id}")
    myip = sh(["curl", "-fsS", "https://checkip.amazonaws.com"]).stdout.strip()
    aws(["ec2", "authorize-security-group-ingress", "--group-id", sg_id,
         "--protocol", "tcp", "--port", "22", "--cidr", f"{myip}/32"], check=False)
    print(f"  SSH authorized from {myip}/32")

    fleet["ami_id"] = ami
    fleet["sg_id"] = sg_id
    fleet["key_name"] = KEY_NAME
    fleet["instance_type"] = INSTANCE_TYPE
    save_fleet(run_tag, fleet)

    print(f"[provision] building {args.shards} shards from {args.dataset}")
    shards = build_shards(args.shards, args.dataset)
    for i, task_ids in enumerate(shards):
        sid = f"shard-{i:02d}"
        fleet["shards"].setdefault(sid, {})["task_ids"] = task_ids
    save_fleet(run_tag, fleet)
    total = sum(len(s) for s in shards)
    print(f"  {len(shards)} shards, sizes {min(len(s) for s in shards)}-{max(len(s) for s in shards)}, total tasks={total}")

    def launch_one(sid):
        info = fleet["shards"][sid]
        if info.get("instance_id"):
            print(f"  {sid}: already has instance {info['instance_id']}, skipping launch")
            return sid, info
        name = f"contextbench-{run_tag}-{sid}"
        try:
            launch = aws(["ec2", "run-instances",
                          "--instance-type", INSTANCE_TYPE,
                          "--image-id", ami,
                          "--key-name", KEY_NAME,
                          "--security-group-ids", sg_id,
                          "--count", "1",
                          "--block-device-mappings",
                          f"DeviceName=/dev/sda1,Ebs={{VolumeSize={ROOT_VOLUME_GB},VolumeType=gp3,DeleteOnTermination=true}}",
                          "--metadata-options", "HttpTokens=required,HttpPutResponseHopLimit=2",
                          "--tag-specifications",
                          f"ResourceType=instance,Tags=[{{Key=Name,Value={name}}},{{Key=cb-run,Value={run_tag}}},{{Key=cb-shard,Value={sid}}}]",
                          "--query", "Instances[0].InstanceId", "--output", "text"])
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
        for fut in as_completed([ex.submit(launch_one, sid) for sid in fleet["shards"]]):
            # Persist after EVERY future, not after the whole batch: a crash
            # (or an aws-cli error we failed to catch) partway through the
            # batch must not lose already-launched instance ids — that bug
            # stranded 11 real, paying instances untracked on 2026-07-12.
            sid, info = fut.result()
            fleet["shards"][sid] = info
            save_fleet(run_tag, fleet)
    fleet["shards"] = {sid: info for sid, info in fleet["shards"].items() if info.get("instance_id")}
    save_fleet(run_tag, fleet)

    print("[provision] waiting for all instances running + assigning/attaching data volumes...")

    def finish_one(sid):
        info = fleet["shards"][sid]
        iid = info["instance_id"]
        try:
            aws(["ec2", "wait", "instance-running", "--instance-ids", iid])
            desc = aws(["ec2", "describe-instances", "--instance-ids", iid,
                        "--query", "Reservations[0].Instances[0].[PublicIpAddress,Placement.AvailabilityZone]",
                        "--output", "text"]).stdout.split()
            ip, az = desc[0], desc[1]
            info["public_ip"] = ip
            info["az"] = az
            if not info.get("volume_id"):
                vol_name = f"contextbench-{run_tag}-{sid}-data"
                vol = aws(["ec2", "create-volume", "--availability-zone", az,
                           "--size", str(DATA_VOLUME_GB), "--volume-type", "gp3",
                           "--tag-specifications",
                           f"ResourceType=volume,Tags=[{{Key=Name,Value={vol_name}}},{{Key=cb-run,Value={run_tag}}},{{Key=cb-shard,Value={sid}}}]",
                           "--query", "VolumeId", "--output", "text"]).stdout.strip()
                aws(["ec2", "wait", "volume-available", "--volume-ids", vol])
                aws(["ec2", "attach-volume", "--volume-id", vol, "--instance-id", iid, "--device", "/dev/sdf"])
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
        for fut in as_completed([ex.submit(finish_one, sid) for sid in fleet["shards"]]):
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
    print(f"[bootstrap] local memtrace HEAD={prov['head_sha'][:12]} dirty_files={prov['dirty_file_count']} diff_sha={prov['dirty_diff_sha256_16'] or 'clean'}")
    fleet["memtrace_source_provenance"] = prov
    save_fleet(run_tag, fleet)

    shard_ids = args.only.split(",") if args.only else list(fleet["shards"].keys())

    def boot_one(sid):
        info = fleet["shards"][sid]
        log = []
        try:
            ip = info["public_ip"]
            vol = info["volume_id"]
            ssh_run(ip, f"mkdir -p {REMOTE_ADAPTER_DIR}")
            rsync_to(ip, str(ADAPTER_DIR) + "/", REMOTE_ADAPTER_DIR + "/",
                     extra=["--exclude", ".env", "--exclude", "__pycache__",
                            "--exclude", ".DS_Store", "--exclude", "aws/state",
                            "--exclude", "aws/config.env", "--exclude", "work/"])
            log.append("adapter synced")

            ssh_run(ip, f"bash {REMOTE_ADAPTER_DIR}/aws/remote/bootstrap-remote.sh "
                        f"MEMTRACE_VERSION=0.8.21 MEMTRACE_INSTALL_MODE=source DATA_VOL_ID={vol}",
                     timeout=1500)
            log.append("bootstrap-remote.sh ok")

            remote_src = "/srv/contextbench/memtrace-src"
            ssh_run(ip, f"mkdir -p {remote_src}")
            rsync_to(ip, str(MEMTRACE_SOURCE_DIR) + "/", remote_src + "/",
                     extra=["--filter", ":- .gitignore", "--exclude", ".git/",
                            "--exclude", "target/", "--exclude", "node_modules/",
                            "--exclude", ".DS_Store", "--exclude", "__pycache__"])
            manifest_local = FLEET_STATE_DIR / run_tag / f"{sid}-source-manifest.json"
            manifest_local.write_text(json.dumps({
                "install_mode": "source", **prov,
                "source_dir_local": str(MEMTRACE_SOURCE_DIR),
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "captured_on": "Alexs-MacBook-Pro.local",
            }, indent=2) + "\n")
            sh(["scp"] + SSH_OPTS + ["-q", str(manifest_local), f"{REMOTE_USER}@{ip}:{remote_src}/source-manifest.json"])
            log.append("source rsynced")

            ssh_run(ip, f"bash {REMOTE_ADAPTER_DIR}/aws/remote/build-memtrace-remote.sh SRC_DIR={remote_src}",
                     timeout=1500)
            log.append("build-memtrace-remote.sh ok")

            sh(["scp"] + SSH_OPTS + ["-q", str(ENV_FILE), f"{REMOTE_USER}@{ip}:{REMOTE_ADAPTER_DIR}/.env"])
            ssh_run(ip, f"chmod 600 {REMOTE_ADAPTER_DIR}/.env")
            log.append(".env pushed")

            data_dir = "/srv/contextbench/contextbench/data"
            r = ssh_run(ip, f"test -f {data_dir}/full.parquet && test -f {data_dir}/contextbench_verified.parquet", check=False)
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
    shard_ids = args.only.split(",") if args.only else \
        [sid for sid, info in fleet["shards"].items() if info.get("bootstrap_ok")]

    src_head = fleet.get("memtrace_source_provenance", {}).get("head_sha", "unknown")[:12]
    dirty_suffix = ""
    prov = fleet.get("memtrace_source_provenance", {})
    if prov.get("dirty_diff_sha256_16"):
        dirty_suffix = f"-dirty{prov['dirty_diff_sha256_16'][:8]}"
    cache_namespace = f"contextbench-src{src_head}{dirty_suffix}-jina-code-768-v1"

    def run_one(sid):
        info = fleet["shards"][sid]
        ip = info["public_ip"]
        run_id = f"run-{run_tag}-{sid}-{DATASET}"
        info["run_id"] = run_id
        results_dir = f"/srv/contextbench/results/{run_id}"
        manifest_json = json.dumps(info["task_ids"])
        # Write manifest.json BEFORE launching so run-remote.sh does not
        # regenerate it from the FULL dataset (rule: disjoint per-shard slice).
        ssh_run(ip, f"mkdir -p {results_dir}")
        write_manifest = f"cat > {results_dir}/manifest.json <<'MANIFEST_EOF'\n{manifest_json}\nMANIFEST_EOF"
        ssh_run(ip, write_manifest)
        ssh_run(ip, f"printf '%s\\n' {run_id} > /srv/contextbench/results/LATEST")

        inner = (
            f"set -e; "
            f"export OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' {REMOTE_ADAPTER_DIR}/.env | head -1 | cut -d= -f2-); "
            f"echo \"[{sid}] OPENAI_API_KEY prefix: ${{OPENAI_API_KEY:0:8}}\"; "
            f"[ -n \"$OPENAI_API_KEY\" ] || {{ echo 'FATAL: OPENAI_API_KEY empty'; exit 1; }}; "
            f"CB_SEARCH_LIMIT=100 CB_PACK_POLICY=v4 DISK_FLOOR_GB=20 "
            f"RUN_ID={run_id} DATASET={DATASET} CONCURRENCY=1 LINE_BUDGET={LINE_BUDGET} "
            f"SELECTOR_MODEL={SELECTOR_MODEL} SELECTOR_MODE={SELECTOR_MODE} "
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
        r = ssh_run(ip, f"find {results_dir}/runs -mindepth 2 -maxdepth 2 -name prediction.jsonl -size +0c 2>/dev/null | wc -l", check=False, timeout=20)
        completed = int(r.stdout.strip() or 0) if r.returncode == 0 else -1
        alive = ssh_run(ip, "tmux has-session -t contextbench 2>/dev/null && echo yes || echo no", check=False, timeout=20)
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
            status = "RUNNING" if alive else ("DONE" if completed >= total else "STOPPED(!)")
            if status != "RUNNING":
                done_shards += 1
            lines.append((sid, completed, total, status))
    lines.sort()
    for sid, completed, total, status in lines:
        print(f"  {sid}: {completed}/{total} {status}")
    print(f"[poll] total {total_completed}/{total_tasks} predictions, {done_shards}/{len(shard_ids)} shards not-running")


def cmd_collect(args):
    run_tag = args.run_tag
    fleet = load_fleet(run_tag)
    agg_dir = AWS_DIR / "state" / "fleet" / run_tag / "aggregate" / "runs"
    agg_dir.mkdir(parents=True, exist_ok=True)
    shard_ids = [sid for sid, info in fleet["shards"].items() if info.get("run_id")]

    def pull_one(sid):
        info = fleet["shards"][sid]
        ip = info["public_ip"]
        run_id = info["run_id"]
        results_dir = f"/srv/contextbench/results/{run_id}"
        local_dir = AWS_DIR / "state" / "fleet" / run_tag / "pulled" / sid
        local_dir.mkdir(parents=True, exist_ok=True)
        rsync_to(ip, "", "", delete=False) if False else None
        r = sh(["rsync", "-az", "-e", "ssh " + " ".join(SSH_OPTS),
                f"{REMOTE_USER}@{ip}:{results_dir}/", str(local_dir) + "/"], check=False, timeout=1800)
        return sid, local_dir, r.returncode

    pulled = []
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futs = [ex.submit(pull_one, sid) for sid in shard_ids]
        for fut in as_completed(futs):
            sid, local_dir, rc = fut.result()
            pulled.append((sid, local_dir, rc))
            print(f"  {sid}: pulled to {local_dir} rc={rc}")

    # merge runs/ dirs into aggregate, dedup by instance_id (dir name = slug)
    seen = {}
    dup = 0
    for sid, local_dir, rc in pulled:
        runs_src = local_dir / "runs"
        if not runs_src.is_dir():
            continue
        for slug_dir in runs_src.iterdir():
            if not slug_dir.is_dir():
                continue
            slug = slug_dir.name
            if slug in seen:
                dup += 1
                continue
            seen[slug] = sid
            dst = agg_dir / slug
            if not dst.exists():
                sh(["cp", "-R", str(slug_dir), str(dst)])
    print(f"[collect] aggregated {len(seen)} unique task dirs (dedup skipped {dup}) -> {agg_dir}")

    # build merged predictions.jsonl
    pred_lines = []
    for slug, sid in seen.items():
        p = agg_dir / slug / "prediction.jsonl"
        if p.exists() and p.stat().st_size > 0:
            pred_lines.append(p.read_text().strip())
    merged = AWS_DIR / "state" / "fleet" / run_tag / "aggregate" / "predictions.jsonl"
    merged.write_text("\n".join(pred_lines) + "\n")
    print(f"[collect] {len(pred_lines)} predictions -> {merged}")


def cmd_terminate(args):
    run_tag = args.run_tag
    fleet = load_fleet(run_tag)
    ids = [info["instance_id"] for info in fleet["shards"].values() if info.get("instance_id")]
    if not ids:
        print("[terminate] no instances recorded")
        return
    print(f"[terminate] terminating {len(ids)} instances")
    for i in range(0, len(ids), 50):
        aws(["ec2", "terminate-instances", "--instance-ids"] + ids[i:i+50])
    aws(["ec2", "wait", "instance-terminated", "--instance-ids"] + ids)
    print("[terminate] all instances confirmed terminated")

    vol_ids = [info["volume_id"] for info in fleet["shards"].values() if info.get("volume_id")]
    print(f"[terminate] deleting {len(vol_ids)} shard data volumes")
    for vid in vol_ids:
        r = aws(["ec2", "delete-volume", "--volume-id", vid], check=False)
        if r.returncode != 0:
            print(f"  warn: could not delete {vid}: {r.stderr.strip()[:200]}")
    print("[terminate] done")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["provision", "bootstrap", "run", "poll", "collect", "terminate"])
    ap.add_argument("--run-tag", default="e5")
    ap.add_argument("--shards", type=int, default=39)
    ap.add_argument("--dataset", default=str(DATA_LOCAL / "contextbench_verified.parquet"))
    ap.add_argument("--only", default="")
    ap.add_argument("--parallel", type=int, default=15)
    args = ap.parse_args()
    {
        "provision": cmd_provision,
        "bootstrap": cmd_bootstrap,
        "run": cmd_run,
        "poll": cmd_poll,
        "collect": cmd_collect,
        "terminate": cmd_terminate,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
