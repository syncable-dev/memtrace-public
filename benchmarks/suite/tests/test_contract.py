from dataclasses import is_dataclass, fields
import pytest
from benchmarks.suite.contract import (
    QueryResult, GraphResult, ReindexReport, SetupReport,
    NotSupported, Adapter, SymbolRef,
)


def test_query_result_is_dataclass_with_expected_fields():
    assert is_dataclass(QueryResult)
    names = {f.name for f in fields(QueryResult)}
    assert names == {"paths", "ranked_symbols", "raw_response_text",
                     "latency_ms", "tokens_used"}


def test_graph_result_fields():
    assert is_dataclass(GraphResult)
    names = {f.name for f in fields(GraphResult)}
    assert names == {"nodes", "latency_ms"}


def test_reindex_report_fields():
    assert is_dataclass(ReindexReport)
    names = {f.name for f in fields(ReindexReport)}
    assert names == {"files_reindexed", "wall_ms", "incremental"}


def test_setup_report_fields():
    assert is_dataclass(SetupReport)
    names = {f.name for f in fields(SetupReport)}
    assert names == {"indexed_files", "wall_ms"}


def test_symbol_ref_fields():
    assert is_dataclass(SymbolRef)
    names = {f.name for f in fields(SymbolRef)}
    assert names == {"name", "file_path", "line"}


def test_not_supported_carries_reason():
    ns = NotSupported(reason="no graph support")
    assert ns.reason == "no graph support"


def test_adapter_defaults_return_not_supported_for_unimplemented():
    class BareAdapter(Adapter):
        name = "bare"
        description = "test"
        version = "0.0.0"
        def setup(self, corpus): return SetupReport(indexed_files=0, wall_ms=0.0)
        def teardown(self): pass
        def query_symbol(self, name, limit): return QueryResult([], [], "", 0.0, 0)

    a = BareAdapter()
    assert isinstance(a.query_natural("foo", 10), NotSupported)
    assert isinstance(a.callers_of("foo"), NotSupported)
    assert isinstance(a.callees_of("foo"), NotSupported)
    assert isinstance(a.impact_of("foo"), NotSupported)
    assert isinstance(a.find_dead_code(), NotSupported)
    assert isinstance(a.reindex_paths([]), NotSupported)
