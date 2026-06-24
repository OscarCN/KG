"""Candidate retrieval backends — the swappable pair behind the linker.

Two collaborators make candidate retrieval pluggable (in-memory today,
kgdb-backed for streaming — see `docs/todos/kgdb_candidate_index.md`):

- `CandidateIndex` — maps a record's identity to a set of candidate ids.
  `register` is fed opaque key tuples *constructed by the strategy*
  (`strategy.lookup_keys` / `_register`), so the index itself knows nothing
  about dates or geography. `lookup_candidates(strategy, prep)` is the one call
  the linker makes; the in-memory impl projects it to opaque-key lookups, a
  kgdb impl to SQL over the persisted retrieval columns.
- `RecordStore` — resolves a candidate id to its linked record. The linker
  needs the records (not just ids) to run the geo gate, deterministic gate, and
  LLM. In-memory this is just a dict; a kgdb impl reads `entities.metadata`.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, Protocol, Set, Tuple

IndexKey = Tuple[str, ...]


class CandidateIndex(Protocol):
    """Protocol for candidate retrieval backends."""

    def register(self, key: IndexKey, item_id: str) -> None:
        """Register `item_id` under `key`. Idempotent. (No-op for column-backed
        indexes, where the writer's rows are the registration.)"""
        ...

    def lookup(self, keys: Iterable[IndexKey]) -> Set[str]:
        """Return the union of ids registered under any of `keys`."""
        ...

    def lookup_candidates(self, strategy: Any, prep: Any) -> Set[str]:
        """Candidate ids for `prep`. The single retrieval call the linker makes:
        in-memory projects to `lookup(strategy.lookup_keys(prep))`; a kgdb index
        projects `strategy.retrieval_criteria(prep)` to SQL."""
        ...


class RecordStore(Protocol):
    """Protocol for id -> linked-record resolution (dict-like)."""

    def __getitem__(self, item_id: str) -> Dict[str, Any]: ...
    def __setitem__(self, item_id: str, record: Dict[str, Any]) -> None: ...
    def __contains__(self, item_id: str) -> bool: ...
    def get(self, item_id: str, default: Any = None) -> Any: ...


class InMemoryCandidateIndex:
    """Dict-backed `CandidateIndex` for single-run / single-worker use."""

    def __init__(self) -> None:
        self._buckets: Dict[IndexKey, Set[str]] = defaultdict(set)

    def register(self, key: IndexKey, item_id: str) -> None:
        self._buckets[key].add(item_id)

    def lookup(self, keys: Iterable[IndexKey]) -> Set[str]:
        found: Set[str] = set()
        for key in keys:
            found |= self._buckets.get(key, set())
        return found

    def lookup_candidates(self, strategy: Any, prep: Any) -> Set[str]:
        return self.lookup(strategy.lookup_keys(prep))


class InMemoryRecordStore(dict):
    """`RecordStore` = a plain dict (id -> linked record). Behaviour-identical to
    the previous `EntityLinker.events` dict."""
