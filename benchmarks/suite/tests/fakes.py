"""Test double used by runner tests. Deterministic, no side effects."""
from benchmarks.suite.contract import Adapter, QueryResult, SetupReport


class FakeAdapter(Adapter):
    name = "fake"
    description = "canned-response adapter for unit tests"
    version = "fake@test"

    def __init__(self, canned: dict[str, QueryResult]):
        self.canned = canned
        self.setup_called = False
        self.teardown_called = False

    def setup(self, corpus) -> SetupReport:
        self.setup_called = True
        return SetupReport(indexed_files=0, wall_ms=0.0)

    def teardown(self) -> None:
        self.teardown_called = True

    def query_symbol(self, name: str, limit: int) -> QueryResult:
        return self.canned.get(name, QueryResult())


class CrashingAdapter(FakeAdapter):
    def query_symbol(self, name: str, limit: int) -> QueryResult:
        raise RuntimeError(f"boom on {name}")
