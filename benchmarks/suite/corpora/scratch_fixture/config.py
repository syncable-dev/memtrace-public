"""Configuration loader — plain dict-backed for the fixture."""
from __future__ import annotations


DEFAULT_CONFIG = {
    "num_workers": 2,
    "max_retries": 3,
    "base_delay": 0.01,
    "debug": False,
}


def load_config(overrides: dict | None = None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if overrides:
        cfg.update(overrides)
    return cfg


def get_num_workers(cfg: dict) -> int:
    return int(cfg.get("num_workers", 2))
