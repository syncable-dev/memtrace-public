"""ScratchFixtureCorpus — handle for the hand-authored task-queue fixture
used by Bench #4 (incremental indexing). Parallels MempalaceCorpus."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_SCRATCH = (
    Path(__file__).resolve().parent / "scratch_fixture"
)


@dataclass
class ScratchFixtureCorpus:
    path: Path = field(default_factory=lambda: DEFAULT_SCRATCH)
    name: str = "scratch_fixture"

    @property
    def parent(self) -> Path:
        return self.path.parent
