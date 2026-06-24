"""kgdb-backed candidate retrieval (column reconstruction).

The durable counterparts of `InMemoryCandidateIndex` / `InMemoryRecordStore`,
so a streaming worker dedups against everything already in kgdb (not just events
seen in its own lifetime). See `docs/todos/kgdb_candidate_index.md`.

- `KgdbCandidateIndex` — `lookup_candidates` projects `strategy.retrieval_criteria`
  to one SQL query over the rows the writer already persists: `entities`
  (supertype via `metadata`), `event_properties` (date `&&`), `entity_locations`
  (fine `level_N_id` / 3×3 grid block / coarse-state bridge). `register` is a
  no-op — those persisted rows *are* the registration.
- `KgdbRecordStore` — resolves a candidate id to its linked record by reading
  `entities.metadata` keyed on `_link_id`. Writes go through `KgdbWriter`.

Both share the caller's psycopg2 connection (the listener's `KgdbWriter` conn),
so candidate lookup sees everything committed by prior `upsert_linked` calls.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional, Set

from .index import IndexKey


class KgdbCandidateIndex:
    """Column-reconstruction `CandidateIndex`. `register` is a no-op."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def register(self, key: IndexKey, item_id: str) -> None:
        # No-op: the writer's entity/event_properties/entity_locations rows are
        # the registration; there is no separate key table to maintain.
        return None

    def lookup(self, keys: Iterable[IndexKey]) -> Set[str]:  # pragma: no cover
        raise NotImplementedError("KgdbCandidateIndex retrieves via lookup_candidates")

    def lookup_candidates(self, strategy: Any, prep: Any) -> Set[str]:
        c = strategy.retrieval_criteria(prep)
        if c.win_start is None or c.win_end is None:
            return set()

        params: Dict[str, Any] = {
            "partition": c.partition_value,
            "win_start": c.win_start,
            "win_end": c.win_end,
        }
        geo_clauses = []
        for n, lid in c.level_ids.items():
            geo_clauses.append(f"l.level_{n}_id = %(l{n})s")
            params[f"l{n}"] = lid
        if c.grid_bbox is not None:
            lat_lo, lat_hi, lon_lo, lon_hi = c.grid_bbox
            geo_clauses.append(
                "(l.coords IS NOT NULL "
                "AND l.coords[1] >= %(lat_lo)s AND l.coords[1] < %(lat_hi)s "
                "AND l.coords[0] >= %(lon_lo)s AND l.coords[0] < %(lon_hi)s)"
            )
            params.update(lat_lo=lat_lo, lat_hi=lat_hi, lon_lo=lon_lo, lon_hi=lon_hi)
        if c.probe_noloc:
            # The so:<state> / noloc bridge: a coarse candidate (no fine ids and
            # no coords) or one with no location row at all. The hard geo gate
            # downstream still enforces hierarchical containment.
            coarse = (
                "l.record_id IS NULL OR (l.level_3_id IS NULL AND l.level_5_id IS NULL "
                "AND l.level_6_id IS NULL AND l.level_7_id IS NULL AND l.coords IS NULL)"
            )
            if c.level_2_id is not None:
                geo_clauses.append(
                    f"(({coarse}) AND (l.level_2_id = %(l2)s OR l.level_2_id IS NULL))"
                )
                params["l2"] = c.level_2_id
            else:
                geo_clauses.append(f"({coarse})")
        if not geo_clauses:
            return set()

        sql = f"""
            SELECT DISTINCT e.metadata->>'_link_id' AS link_id
            FROM entities e
            JOIN event_properties ep ON ep.event_id = e.entity_id
            LEFT JOIN entity_locations l ON l.entity_id = e.entity_id
            WHERE e.metadata->>'{c.partition_field}' = %(partition)s
              AND ep.date_start IS NOT NULL AND ep.date_end IS NOT NULL
              AND tstzrange(LEAST(ep.date_start, ep.date_end),
                            GREATEST(ep.date_start, ep.date_end), '[]')
                  && tstzrange(%(win_start)s, %(win_end)s, '[]')
              AND ({' OR '.join(geo_clauses)})
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return {row[0] for row in cur.fetchall() if row[0] is not None}


class KgdbRecordStore:
    """`RecordStore` backed by `entities.metadata` (keyed on `_link_id`)."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def _load(self, item_id: str) -> Optional[Dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT metadata FROM entities WHERE metadata->>'_link_id' = %s LIMIT 1",
                (str(item_id),),
            )
            row = cur.fetchone()
        if not row or row[0] is None:
            return None
        record = row[0]
        return json.loads(record) if isinstance(record, str) else record

    def __getitem__(self, item_id: str) -> Dict[str, Any]:
        record = self._load(item_id)
        if record is None:
            raise KeyError(item_id)
        return record

    def get(self, item_id: str, default: Any = None) -> Any:
        record = self._load(item_id)
        return record if record is not None else default

    def __contains__(self, item_id: str) -> bool:
        return self._load(item_id) is not None

    def __setitem__(self, item_id: str, record: Dict[str, Any]) -> None:
        # Writes go through KgdbWriter.upsert_linked; nothing to cache here.
        return None
