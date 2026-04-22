from pathlib import Path
from benchmarks.suite.corpora.mempalace import DEFAULT_MEMPALACE, MempalaceCorpus


# Reads from `MEMPALACE_PATH` at import time of `corpora.mempalace`.
MEMPALACE_DEFAULT = DEFAULT_MEMPALACE


def test_corpus_path_is_absolute():
    c = MempalaceCorpus()
    assert c.path.is_absolute()


def test_corpus_default_matches_fair_convention():
    c = MempalaceCorpus()
    assert c.path == MEMPALACE_DEFAULT


def test_corpus_parent_is_one_level_up():
    c = MempalaceCorpus()
    assert c.parent == MEMPALACE_DEFAULT.parent


def test_corpus_name():
    c = MempalaceCorpus()
    assert c.name == "mempalace"


def test_corpus_path_override(tmp_path):
    c = MempalaceCorpus(path=tmp_path)
    assert c.path == tmp_path
