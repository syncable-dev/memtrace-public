import pytest
from pathlib import Path
from benchmarks.suite.adapters.chromadb import ChromaDBAdapter
from benchmarks.suite.corpora.mempalace import MempalaceCorpus


@pytest.mark.integration
@pytest.mark.skipif(
    not MempalaceCorpus().path.exists(),
    reason="mempalace checkout missing",
)
def test_chromadb_smoke_indexes_and_queries():
    a = ChromaDBAdapter(collection_name="suite_bench_smoke")
    rep = a.setup(MempalaceCorpus())
    try:
        assert rep.indexed_files > 0
        res = a.query_symbol("MempalaceCorpus", limit=10)
        assert res.latency_ms > 0
    finally:
        a.teardown()


def test_chromadb_teardown_idempotent():
    """Pure unit test — no chromadb service needed when no setup ran."""
    a = ChromaDBAdapter()
    a.teardown()  # never set up
    a.teardown()  # double teardown


def test_chromadb_query_before_setup_raises():
    a = ChromaDBAdapter()
    with pytest.raises(RuntimeError, match="call setup"):
        a.query_symbol("foo", limit=10)
