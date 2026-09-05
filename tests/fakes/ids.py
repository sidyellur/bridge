"""Seeded, deterministic id source shaped like a UUID."""

from __future__ import annotations


class SeededIds:
    def __init__(self, prefix: str = "0000") -> None:
        self._prefix = prefix
        self._n = 0

    def __call__(self) -> str:
        return self.new()

    def new(self) -> str:
        self._n += 1
        tail = f"{self._n:012d}"
        return f"{self._prefix}0000-0000-4000-8000-{tail}"
