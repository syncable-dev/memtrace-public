"""Bench #3 ground-truth generator — Django corpus variant.

Identical flow to pyright_graph.py but points pyright at Django's much
larger codebase (~13k files). Pyright bootstrap + indexing takes longer
(~3-5 min) but call-hierarchy queries are the same.

Writes to `bench_3_graph_django.json` (separate from mempalace's).

Run:
    benchmarks/.venv/bin/python -m benchmarks.suite.datasets.generators.pyright_graph_django
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

from benchmarks.suite.datasets.generators.pyright_graph import generate


DJANGO_PATH = Path("/Users/alexthh/Desktop/ZeroToDemo/django")


def main() -> None:
    if not DJANGO_PATH.exists():
        raise SystemExit(f"django not found at {DJANGO_PATH}")
    print(f"generating pyright ground truth for {DJANGO_PATH}", file=sys.stderr)
    t0 = time.time()
    # Django is big; keep target at 200 triples like mempalace so the
    # per-bench axis is apples-to-apples.
    entries = generate(DJANGO_PATH, corpus_prefix="django", target_triples=200)
    elapsed = time.time() - t0
    out = Path(__file__).resolve().parents[1] / "bench_3_graph_django.json"
    out.write_text(json.dumps(entries, indent=2) + "\n")
    print(f"wrote {len(entries)} triples -> {out}  ({elapsed:.1f}s)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
