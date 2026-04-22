import pytest
from pathlib import Path
from benchmarks.suite.adapters.memtrace import MemtraceAdapter, DEFAULT_BINARY
from benchmarks.suite.corpora.mempalace import MempalaceCorpus


@pytest.mark.integration
@pytest.mark.skipif(not DEFAULT_BINARY.exists(),
                    reason="memtrace release binary not built")
def test_memtrace_smoke_find_symbol():
    a = MemtraceAdapter()
    a.setup(MempalaceCorpus())
    try:
        res = a.query_symbol("MempalaceCorpus", limit=10)
        assert res.latency_ms > 0
        assert res.tokens_used >= 0
    finally:
        a.teardown()


def test_memtrace_teardown_idempotent():
    """Pure unit test — no binary needed. Verifies teardown is safe to call
    before setup and multiple times in a row."""
    a = MemtraceAdapter()
    a.teardown()  # never set up
    a.teardown()  # double teardown
