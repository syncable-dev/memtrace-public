import pytest

from benchmarks.suite.adapters.codebase_memory import CodebaseMemoryAdapter


def test_query_before_setup_raises():
    with pytest.raises(RuntimeError, match="call setup"):
        CodebaseMemoryAdapter(binary="definitely-not-installed").query_symbol("thing", 5)


def test_missing_binary_is_an_honest_empty_result(tmp_path):
    adapter = CodebaseMemoryAdapter(binary="definitely-not-installed")
    adapter._corpus_path = tmp_path
    result = adapter.query_symbol("thing", 5)
    assert result.paths == []
    assert "not installed" in result.raw_response_text


def test_absolute_path_is_normalized_under_corpus(tmp_path):
    adapter = CodebaseMemoryAdapter(binary="definitely-not-installed")
    adapter._corpus_path = tmp_path
    assert adapter._relative_path(str(tmp_path / "src" / "app.py")) == f"{tmp_path.name}/src/app.py"


def test_relative_path_is_normalized_under_corpus(tmp_path):
    adapter = CodebaseMemoryAdapter(binary="definitely-not-installed")
    adapter._corpus_path = tmp_path
    assert adapter._relative_path("src/app.py") == f"{tmp_path.name}/src/app.py"


def test_cli_uses_documented_tool_position(monkeypatch):
    seen = {}

    class Result:
        returncode = 0
        stdout = '{"results": []}'

    def fake_run(command, **kwargs):
        seen["command"] = command
        return Result()

    monkeypatch.setattr("benchmarks.suite.adapters.codebase_memory.subprocess.run", fake_run)
    adapter = CodebaseMemoryAdapter(binary="codebase-memory-mcp")
    assert adapter._run("search_graph", {"name_pattern": "^thing$"}, timeout=30) == {"results": []}
    assert seen["command"][:3] == ["codebase-memory-mcp", "cli", "search_graph"]
