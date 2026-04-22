"""Pytest config — makes benchmarks.suite importable without install."""
import sys
from pathlib import Path

# benchmarks/ is 2 levels up from this file; add its parent to sys.path
# so `from benchmarks.suite import ...` works.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: test requires live services/binaries (Memtrace, Chroma, etc.)",
    )
