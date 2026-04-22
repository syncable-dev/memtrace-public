"""Mempalace corpus handle. Thin wrapper over the existing on-disk checkout
used by fair/. No copy; no git clone; just a pinned path.

External reproducers: set `MEMPALACE_PATH` in the environment to point at
your mempalace checkout, or pass `path=` explicitly. Default is the
original benchmark host layout.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_MEMPALACE = Path(
    os.environ.get(
        "MEMPALACE_PATH",
        "/Users/alexthh/Desktop/ZeroToDemo/mempalace",
    )
)


@dataclass
class MempalaceCorpus:
    path: Path = field(default=DEFAULT_MEMPALACE)
    name: str = "mempalace"

    @property
    def parent(self) -> Path:
        return self.path.parent
