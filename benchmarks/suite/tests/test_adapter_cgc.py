import pytest
from benchmarks.suite.adapters.cgc import CGCAdapter, DEFAULT_BIN
from benchmarks.suite.corpora.mempalace import MempalaceCorpus


@pytest.mark.integration
@pytest.mark.skipif(not DEFAULT_BIN.exists(), reason="cgc CLI not installed")
def test_cgc_smoke():
    a = CGCAdapter()
    a.setup(MempalaceCorpus())
    try:
        res = a.query_symbol("MempalaceCorpus", limit=10)
        assert res.latency_ms >= 0
    finally:
        a.teardown()


def test_cgc_teardown_idempotent():
    a = CGCAdapter()
    a.teardown()
    a.teardown()


def test_cgc_query_before_setup_raises():
    a = CGCAdapter()
    with pytest.raises(RuntimeError, match="call setup"):
        a.query_symbol("foo", limit=10)
