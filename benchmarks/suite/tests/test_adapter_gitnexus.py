import pytest
from benchmarks.suite.adapters.gitnexus import GitNexusAdapter
from benchmarks.suite.corpora.mempalace import MempalaceCorpus


@pytest.mark.integration
def test_gitnexus_smoke():
    a = GitNexusAdapter()
    rep = a.setup(MempalaceCorpus())
    try:
        if not a._server_up:
            pytest.skip("gitnexus eval-server not running on :4848")
        res = a.query_symbol("MempalaceCorpus", limit=10)
        assert res.latency_ms >= 0
    finally:
        a.teardown()


def test_gitnexus_teardown_idempotent():
    a = GitNexusAdapter()
    a.teardown()
    a.teardown()


def test_gitnexus_query_returns_server_down_when_not_pinged():
    a = GitNexusAdapter()
    # Never call setup() → _server_up remains False
    res = a.query_symbol("foo", limit=10)
    assert res.paths == []
    assert "<server down>" in res.raw_response_text
