#!/usr/bin/env python3
"""Grade ContextBench Pro, Poly, and Multi patches in their official task images."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
from datasets import load_dataset


SUPPORTED_FAMILIES = {"SWE-Bench-Pro", "SWE-PolyBench", "Multi-SWE-Bench"}


def family(instance_id: str) -> str:
    return instance_id.split("__", 1)[0]


def parse_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    parsed = ast.literal_eval(text)
    return [str(item) for item in parsed]


def task_image(row: dict[str, Any], metadata: dict[str, Any]) -> str:
    task_family = family(str(row["instance_id"]))
    original_id = str(row["original_inst_id"])
    if task_family == "Multi-SWE-Bench":
        match = re.fullmatch(r"(.+)__(.+)-(\d+)", original_id)
        if not match:
            raise ValueError(f"unrecognized Multi-SWE instance id: {original_id}")
        return f"mswebench/{match.group(1)}_m_{match.group(2)}:pr-{match.group(3)}"
    if task_family == "SWE-PolyBench":
        return f"ghcr.io/timesler/swe-polybench.eval.x86_64.{original_id}:latest"
    if task_family == "SWE-Bench-Pro":
        tag = str(metadata.get("dockerhub_tag") or "")
        if not tag:
            raise ValueError(f"missing Pro dockerhub_tag for {original_id}")
        return f"jefzda/sweap-images:{tag}"
    raise ValueError(f"unsupported family: {task_family}")


def run(command: list[str], *, timeout: int, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=check,
    )


def multi_metadata_index() -> dict[str, dict[str, Any]]:
    from datasets import load_dataset_builder
    from huggingface_hub import hf_hub_download

    builder = load_dataset_builder("ByteDance-Seed/Multi-SWE-bench")
    index: dict[str, dict[str, Any]] = {}
    for data_file in builder.config.data_files.get("train", []):
        value = str(data_file)
        if value.startswith("hf://") and "@" in value:
            relative = value.split("@", 1)[1].split("/", 1)[1]
        else:
            relative = value
        local = hf_hub_download(
            repo_id="ByteDance-Seed/Multi-SWE-bench",
            filename=relative,
            repo_type="dataset",
        )
        for line in Path(local).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            instance_id = str(
                row.get("instance_id")
                or f"{row.get('org')}__{row.get('repo')}-{row.get('number')}"
            )
            index[instance_id] = row
    return index


def official_multi_resolution(metadata: dict[str, Any], test_log: str) -> tuple[bool, str]:
    from multi_swe_bench.harness.dataset import Dataset
    from multi_swe_bench.harness.image import Config
    from multi_swe_bench.harness.instance import Instance
    from multi_swe_bench.harness.report import Report

    dataset = Dataset.from_json(json.dumps(metadata))
    instance = Instance.create(
        dataset, Config(need_clone=False, global_env=None, clear_env=True)
    )
    fix_result = instance.parse_log(test_log)
    report_data = json.loads(dataset.to_json())
    report_data["fix_patch_result"] = json.loads(fix_result.to_json())
    report = Report.from_dict(report_data)
    return bool(report.valid), report.error_msg or ""


def container_workdir(container: str, fallback: str) -> str:
    result = run(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-lc",
            "find /home /app /testbed -maxdepth 4 -type d -name .git 2>/dev/null | head -1",
        ],
        timeout=60,
    )
    git_dir = result.stdout.strip().splitlines()
    return str(Path(git_dir[0]).parent) if git_dir else fallback


def grade_task(
    row: dict[str, Any],
    metadata: dict[str, Any],
    patch: str,
    log_path: Path,
    timeout: int,
    remove_image: bool,
) -> dict[str, Any]:
    task_family = family(str(row["instance_id"]))
    image = task_image(row, metadata)
    pull = run(["docker", "pull", image], timeout=1200)
    if pull.returncode != 0:
        raise RuntimeError(f"docker pull failed for {image}: {pull.stdout[-2000:]}")
    name = f"contextbench-eval-{uuid.uuid4().hex[:12]}"
    started = run(
        ["docker", "run", "-d", "--name", name, "--entrypoint", "/bin/sh", image, "-c", "sleep 2h"],
        timeout=120,
    )
    if started.returncode != 0:
        raise RuntimeError(f"container start failed for {image}: {started.stdout[-2000:]}")
    try:
        fallback = "/home" if task_family == "Multi-SWE-Bench" else "/testbed"
        workdir = container_workdir(name, fallback)
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            model_patch = temp / "model.patch"
            test_patch = temp / "test.patch"
            model_patch.write_text(patch, encoding="utf-8")
            test_patch.write_text(str(row.get("test_patch") or ""), encoding="utf-8")
            for source, target in (
                (model_patch, "/home/fix.patch" if task_family == "Multi-SWE-Bench" else "/tmp/model.patch"),
                (test_patch, "/home/test.patch" if task_family == "Multi-SWE-Bench" else "/tmp/test.patch"),
            ):
                copied = run(["docker", "cp", str(source), f"{name}:{target}"], timeout=60)
                if copied.returncode != 0:
                    raise RuntimeError(f"docker cp failed: {copied.stdout[-2000:]}")

        if task_family == "Multi-SWE-Bench":
            command = (
                "export PATH=/usr/local/go/bin:/root/.cargo/bin:/usr/local/cargo/bin:"
                "/opt/maven/bin:/opt/gradle/bin:$PATH && "
                "apt-get update && apt-get install -y patch && "
                "sed -i 's@git apply.*@patch --batch --fuzz=5 -p1 -i /home/test.patch;"
                "patch --batch --fuzz=5 -p1 -i /home/fix.patch@g' /home/fix-run.sh && "
                "chmod +x /home/*.sh && /home/fix-run.sh"
            )
            cwd = "/home"
        elif task_family == "SWE-PolyBench":
            test_command = str(metadata.get("test_command") or "")
            if not test_command:
                raise ValueError("PolyBench task has no test_command")
            command = (
                f"git reset --hard {row['base_commit']} && git clean -fd && "
                "git apply /tmp/model.patch && git apply /tmp/test.patch && "
                f"{test_command}"
            )
            cwd = workdir
        else:
            setup = str(metadata.get("before_repo_set_cmd") or "")
            tests = parse_list(metadata.get("selected_test_files_to_run"))
            if not setup or not tests:
                raise ValueError("Pro task is missing setup command or selected tests")
            quoted_tests = " ".join(subprocess.list2cmdline([test]) for test in tests)
            command = (
                f"{setup}\n"
                "git apply /tmp/model.patch\n"
                f"python -m pytest -q {quoted_tests}"
            )
            cwd = workdir

        result = run(
            ["docker", "exec", "-w", cwd, name, "sh", "-lc", command],
            timeout=timeout,
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(result.stdout, encoding="utf-8")
        resolved = result.returncode == 0
        detail = ""
        if task_family == "Multi-SWE-Bench":
            resolved, detail = official_multi_resolution(metadata, result.stdout)
        return {
            "resolved": resolved,
            "status": "resolved" if resolved else "unresolved",
            "returncode": result.returncode,
            "image": image,
            "workdir": cwd,
            "log": str(log_path),
            "detail": detail,
        }
    finally:
        run(["docker", "rm", "--force", name], timeout=120)
        if remove_image:
            run(["docker", "image", "rm", "--force", image], timeout=300)


def metadata_indexes(families: set[str]) -> dict[str, dict[str, dict[str, Any]]]:
    indexes: dict[str, dict[str, dict[str, Any]]] = {}
    if "SWE-PolyBench" in families:
        indexes["SWE-PolyBench"] = {
            str(row["instance_id"]): row
            for row in load_dataset("AmazonScience/SWE-PolyBench", split="test")
        }
    if "SWE-Bench-Pro" in families:
        indexes["SWE-Bench-Pro"] = {
            str(row["instance_id"]): row
            for row in load_dataset("ScaleAI/SWE-bench_Pro", split="test")
        }
    if "Multi-SWE-Bench" in families:
        indexes["Multi-SWE-Bench"] = multi_metadata_index()
    return indexes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--family", action="append", choices=sorted(SUPPORTED_FAMILIES))
    parser.add_argument("--instance-id")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--remove-images", action="store_true")
    args = parser.parse_args()

    selected_families = set(args.family or SUPPORTED_FAMILIES)
    frame = pd.read_parquet(args.dataset)
    rows = [
        row
        for row in frame.to_dict(orient="records")
        if family(str(row["instance_id"])) in selected_families
        and (args.instance_id is None or str(row["instance_id"]) == args.instance_id)
    ]
    patches = {
        str(value["instance_id"]): str(value.get("model_patch") or "")
        for line in args.predictions.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for value in [json.loads(line)]
    }
    indexes = metadata_indexes(selected_families)
    completed = {
        str(json.loads(line)["instance_id"])
        for line in args.output.read_text(encoding="utf-8").splitlines()
        if line.strip()
    } if args.output.is_file() else set()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    for index, row in enumerate(rows, 1):
        instance_id = str(row["instance_id"])
        if instance_id in completed:
            continue
        original_id = str(row["original_inst_id"])
        patch = patches.get(original_id, "")
        if not patch:
            result = {"resolved": False, "status": "not_submitted"}
        else:
            metadata = indexes.get(family(instance_id), {}).get(original_id, {})
            try:
                result = grade_task(
                    row,
                    metadata,
                    patch,
                    args.log_dir / f"{instance_id}.log",
                    args.timeout,
                    args.remove_images,
                )
            except Exception as error:
                result = {
                    "resolved": False,
                    "status": "error",
                    "error": type(error).__name__,
                    "detail": str(error),
                }
        with args.output.open("a", encoding="utf-8") as output:
            output.write(
                json.dumps(
                    {
                        "instance_id": instance_id,
                        "original_inst_id": original_id,
                        "family": family(instance_id),
                        **result,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        print(f"[{index}/{len(rows)}] {instance_id}: {result['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
