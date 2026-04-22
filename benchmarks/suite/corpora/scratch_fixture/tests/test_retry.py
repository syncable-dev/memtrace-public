"""Tests for retry.retry wrapper."""
import pytest
from ..retry import retry


def test_retry_eventually_succeeds():
    calls = {"n": 0}

    def flake():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient")
        return "ok"

    wrapped = retry(flake, attempts=3, base_delay=0.0)
    assert wrapped() == "ok"
    assert calls["n"] == 2


def test_retry_reraises_after_exhaustion():
    def always_fail():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        retry(always_fail, attempts=2, base_delay=0.0)()
