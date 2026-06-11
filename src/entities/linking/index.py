"""Candidate index — key→ids store behind a protocol.

The index is deliberately dumb: it maps opaque key tuples to sets of
linked-record ids. Key *construction* (day-key enumeration, geo
partitioning, etc.) belongs to the linking strategy (`strategy.py`),
so a future kgdb-backed index can implement the same protocol without
knowing anything about dates or geography.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, Protocol, Set, Tuple

IndexKey = Tuple[str, ...]


class CandidateIndex(Protocol):
    """Protocol for candidate retrieval backends."""

    def register(self, key: IndexKey, item_id: str) -> None:
        """Register `item_id` under `key`. Idempotent."""
        ...

    def lookup(self, keys: Iterable[IndexKey]) -> Set[str]:
        """Return the union of ids registered under any of `keys`."""
        ...


class InMemoryCandidateIndex:
    """Dict-backed `CandidateIndex` for single-run / streaming use."""

    def __init__(self) -> None:
        self._buckets: Dict[IndexKey, Set[str]] = defaultdict(set)

    def register(self, key: IndexKey, item_id: str) -> None:
        self._buckets[key].add(item_id)

    def lookup(self, keys: Iterable[IndexKey]) -> Set[str]:
        found: Set[str] = set()
        for key in keys:
            found |= self._buckets.get(key, set())
        return found
