"""Django corpus handle (copy of the one on `bench-2-django-intent`).
Same shape as MempalaceCorpus.

External reproducers: set `DJANGO_PATH` in the environment to point at
your Django checkout, or pass `path=` explicitly.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_DJANGO = Path(
    os.environ.get(
        "DJANGO_PATH",
        "/Users/alexthh/Desktop/ZeroToDemo/django",
    )
)


@dataclass
class DjangoCorpus:
    path: Path = field(default=DEFAULT_DJANGO)
    name: str = "django"

    @property
    def parent(self) -> Path:
        return self.path.parent
