import pytest

from benchmarks.suite.adapters.code_context_engine import ANSI_ESCAPE, RESULT_PATH, CodeContextEngineAdapter


def test_query_before_setup_raises():
    with pytest.raises(RuntimeError, match="call setup"):
        CodeContextEngineAdapter(binary="definitely-not-installed").query_symbol("thing", 5)


def test_result_output_paths_are_normalized_under_corpus(tmp_path):
    adapter = CodeContextEngineAdapter(binary="definitely-not-installed")
    adapter._corpus_path = tmp_path
    text = f"\x1b[36m  1. {tmp_path}/src/app.py:7-12\x1b[0m"
    match = next(RESULT_PATH.finditer(ANSI_ESCAPE.sub("", text)))
    assert match.group(1) == str(tmp_path / "src" / "app.py")
    assert match.group(2) == "7"
    assert adapter._relative_path(str(tmp_path / "src" / "app.py")) == f"{tmp_path.name}/src/app.py"
    assert adapter._relative_path("src/app.py") == f"{tmp_path.name}/src/app.py"
