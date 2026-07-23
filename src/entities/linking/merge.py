"""Canonical↔canonical merge primitive for kgdb (reconciliation direction B).

Merges a set of already-canonical `entities` (decided same real-world event by the
reconciliation pass) into one survivor, in a single transaction. The shared building
block for the periodic consistency sweep (B) and, later, link-time multi-match (A).

**Tombstone, not delete** (forced by the FK topology — an absorbed `entities` row is
referenced by its own `entities_alias.original_entity_id`): the absorbed row stays,
its `event_properties`/`entity_locations` are removed/moved so it's **invisible to
the linker's retrieval** (which JOINs `event_properties`), and
`entities_alias.current_entity_id → survivor` redirects every reference.

Survivor = the entity with the most `_sources` (tie → lowest `entity_id`).
Children are **consolidated onto the survivor** (alias-routed rows rewritten to the
survivor's `original_entity_id`; direct-FK rows moved), deduped on conflict.

Date/location use the shared layer-aware, outlier-robust aggregator
(`linking/aggregate.py`): `umbrella` → envelope + venue set; `instance` → narrowest
window + finest venue.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import psycopg2.extras
from dateutil import parser as dtparser

from .aggregate import aggregate_date

logger = logging.getLogger(__name__)


def _meta(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, str):
        return json.loads(raw)
    return raw or {}


def _dt(v):
    if not v:
        return None
    try:
        return dtparser.parse(v) if isinstance(v, str) else v
    except (ValueError, TypeError):
        return None


def _dedup(seq, key):
    seen, out = set(), []
    for x in seq:
        k = key(x)
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def _entity_window(meta: Dict[str, Any]) -> Tuple[Any, Any, Any]:
    dr = (meta.get("date_range") or {}).get("date_range") or {}
    pd = (meta.get("date_range") or {}).get("precision_days")
    return _dt(dr.get("start")), _dt(dr.get("end")), pd


class CanonicalMerger:
    """Executes (or dry-runs) canonical↔canonical merges against a kgdb connection."""

    def __init__(self, conn) -> None:
        self._conn = conn

    # -- planning -----------------------------------------------------------------

    def _load(self, entity_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT entity_id, name, description, metadata FROM entities "
                "WHERE entity_id = ANY(%s)",
                (entity_ids,),
            )
            return {r["entity_id"]: r for r in cur.fetchall()}

    @staticmethod
    def _pick_survivor(rows: Dict[int, Dict[str, Any]]) -> int:
        def n_sources(eid: int) -> int:
            return len(_meta(rows[eid]["metadata"]).get("source_ids") or [])
        # most sources, tie -> lowest entity_id
        return max(rows, key=lambda eid: (n_sources(eid), -eid))

    def plan(self, entity_ids: List[int], layer: str) -> Dict[str, Any]:
        """Compute the survivor, merged metadata and aggregated date/location.

        Pure (no writes) — the dry-run body and the execute body both call this."""
        rows = self._load(sorted(set(entity_ids)))
        if len(rows) < 2:
            raise ValueError(f"need >=2 existing entities to merge, got {list(rows)}")
        survivor = self._pick_survivor(rows)
        absorbed = [e for e in rows if e != survivor]
        metas = {e: _meta(r["metadata"]) for e, r in rows.items()}

        # Union the accumulators (survivor first so its ordering wins on ties).
        order = [survivor] + absorbed
        source_ids = _dedup((s for e in order for s in (metas[e].get("source_ids") or [])),
                            key=lambda x: x)
        windows = _dedup((w for e in order for w in (metas[e].get("_source_windows") or [])),
                         key=lambda w: json.dumps(w, sort_keys=True, default=str))
        sources = _dedup((s for e in order for s in (metas[e].get("_sources") or [])),
                         key=lambda s: s.get("source_id"))

        # Layer-aware date over one window per merged canonical (mirrors the dry-run).
        start, end, pd, dropped = aggregate_date(
            [_entity_window(metas[e]) for e in order], layer)

        # Location: finest _geo (highest precision_level) across all entities.
        def prec(e):
            return (metas[e].get("_geo") or {}).get("precision_level") or 0
        finest = max(order, key=prec)

        return {
            "survivor": survivor,
            "absorbed": absorbed,
            "layer": layer,
            "n_sources_total": len(source_ids),
            "date": {"start": start, "end": end, "precision_days": pd},
            "outliers_dropped": dropped,
            "finest_geo_entity": finest,
            "_merged_source_ids": source_ids,
            "_merged_windows": windows,
            "_merged_sources": sources,
            "_metas": metas,
        }

    # -- execution ----------------------------------------------------------------

    def merge(self, entity_ids: List[int], layer: str, dry_run: bool = True) -> Dict[str, Any]:
        p = self.plan(entity_ids, layer)
        survivor, absorbed = p["survivor"], p["absorbed"]
        summary = {k: p[k] for k in ("survivor", "absorbed", "layer",
                                     "n_sources_total", "outliers_dropped")}
        summary["date"] = {"start": _iso(p["date"]["start"]), "end": _iso(p["date"]["end"]),
                           "precision_days": p["date"]["precision_days"]}
        if dry_run:
            summary["dry_run"] = True
            return summary

        with self._conn.cursor() as cur:
            # Lock survivor + absorbed (id-ordered — deadlock-safe).
            cur.execute("SELECT entity_id FROM entities WHERE entity_id = ANY(%s) "
                        "ORDER BY entity_id FOR UPDATE", (sorted(entity_ids),))

            self._write_survivor_metadata(cur, p)
            self._write_event_properties(cur, p)
            self._merge_locations(cur, p)
            self._consolidate_children(cur, survivor, absorbed)
            self._repoint_alias(cur, survivor, absorbed)
            self._tombstone(cur, survivor, absorbed)
        self._conn.commit()
        summary["executed"] = True
        return summary

    def _write_survivor_metadata(self, cur, p) -> None:
        survivor, metas = p["survivor"], p["_metas"]
        meta = dict(metas[survivor])
        # fillna the descriptive fields from absorbed (survivor first keeps its values).
        for e in p["absorbed"]:
            for f in ("name", "description", "context", "status"):
                if not meta.get(f) and metas[e].get(f):
                    meta[f] = metas[e][f]
        meta["source_ids"] = p["_merged_source_ids"]
        meta["_source_windows"] = p["_merged_windows"]
        meta["_sources"] = p["_merged_sources"]
        meta["_layer"] = p["layer"]
        meta["_merged_from"] = sorted(set(meta.get("_merged_from", []))
                                      | {int(e) for e in p["absorbed"]})
        # canonical date_range
        dr = meta.setdefault("date_range", {})
        dr.setdefault("date_range", {})
        dr["date_range"]["start"] = _iso(p["date"]["start"])
        dr["date_range"]["end"] = _iso(p["date"]["end"])
        dr["precision_days"] = p["date"]["precision_days"]
        # finest geo/location anchor
        finest_meta = metas[p["finest_geo_entity"]]
        if finest_meta.get("_geo"):
            meta["_geo"] = finest_meta["_geo"]
            meta["_geo_source"] = finest_meta.get("_geo_source", meta.get("_geo_source"))
        if finest_meta.get("location"):
            meta["location"] = finest_meta["location"]
        cur.execute("UPDATE entities SET metadata = %s WHERE entity_id = %s",
                    (psycopg2.extras.Json(meta), survivor))
        # fill blank survivor name/description columns
        cur.execute(
            "UPDATE entities SET name = COALESCE(NULLIF(name,''), %s), "
            "description = COALESCE(NULLIF(description,''), %s) WHERE entity_id = %s",
            (meta.get("name"), meta.get("description"), survivor))

    def _write_event_properties(self, cur, p) -> None:
        survivor = p["survivor"]
        start, end = p["date"]["start"], p["date"]["end"]
        status = (p["_metas"][survivor].get("status")
                  or next((p["_metas"][e].get("status") for e in p["absorbed"]
                           if p["_metas"][e].get("status")), None))
        cur.execute(
            "INSERT INTO event_properties (event_id, date_start, date_end, status) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (event_id) DO UPDATE "
            "SET date_start=EXCLUDED.date_start, date_end=EXCLUDED.date_end, "
            "status=COALESCE(EXCLUDED.status, event_properties.status)",
            (survivor, start, end, status))
        cur.execute("DELETE FROM event_properties WHERE event_id = ANY(%s)", (p["absorbed"],))

    def _merge_locations(self, cur, p) -> None:
        survivor, absorbed, layer = p["survivor"], p["absorbed"], p["layer"]
        if layer == "umbrella":
            # keep the venue set: drop absorbed rows already on survivor (by geoid),
            # then re-point the rest to the survivor.
            cur.execute(
                "DELETE FROM entity_locations a WHERE a.entity_id = ANY(%s) AND EXISTS ("
                "  SELECT 1 FROM entity_locations s WHERE s.entity_id = %s "
                "  AND s.geoid IS NOT DISTINCT FROM a.geoid)", (absorbed, survivor))
            cur.execute("UPDATE entity_locations SET entity_id = %s WHERE entity_id = ANY(%s)",
                        (survivor, absorbed))
        else:
            # instance: keep the finest single venue. If it belongs to an absorbed
            # entity, hand its rows to the survivor first, then drop the survivor's
            # coarser rows and all remaining absorbed rows.
            finest = p["finest_geo_entity"]
            if finest != survivor:
                cur.execute(
                    "DELETE FROM entity_locations WHERE entity_id = %s", (survivor,))
                cur.execute(
                    "UPDATE entity_locations SET entity_id = %s WHERE entity_id = %s",
                    (survivor, finest))
            cur.execute("DELETE FROM entity_locations WHERE entity_id = ANY(%s)", (absorbed,))

    def _consolidate_children(self, cur, survivor: int, absorbed: List[int]) -> None:
        allids = [survivor] + absorbed
        # entities_documents — UNIQUE(entity_id, doc_id). Across the WHOLE merge set
        # (survivor + all absorbed) keep ONE row per doc_id — preferring one that
        # carries images, then the survivor's — delete the rest, then re-point.
        # (Two *absorbed* entities can share a doc_id, so a survivor-only dedup isn't
        # enough — that would collide on the re-point.)
        cur.execute(
            "DELETE FROM entities_documents ed WHERE ed.entity_id = ANY(%s) "
            "AND ed.ent_doc_id NOT IN ("
            "  SELECT DISTINCT ON (doc_id) ent_doc_id FROM entities_documents "
            "  WHERE entity_id = ANY(%s) "
            "  ORDER BY doc_id, (doc_images IS NOT NULL) DESC, "
            "           (entity_id = %s) DESC, ent_doc_id)",
            (allids, allids, survivor))
        cur.execute("UPDATE entities_documents SET entity_id=%s WHERE entity_id=ANY(%s)",
                    (survivor, absorbed))
        # entity_types — keep one row per entity_type_id across the merge set, re-point.
        cur.execute(
            "DELETE FROM entity_types et WHERE et.entity_id = ANY(%s) "
            "AND et.record_id NOT IN ("
            "  SELECT DISTINCT ON (entity_type_id) record_id FROM entity_types "
            "  WHERE entity_id = ANY(%s) "
            "  ORDER BY entity_type_id, (entity_id = %s) DESC, record_id)",
            (allids, allids, survivor))
        cur.execute("UPDATE entity_types SET entity_id=%s WHERE entity_id=ANY(%s)",
                    (survivor, absorbed))
        # relations — dedupe (source, dest, type) then re-point both endpoints.
        for col in ("ent_id_source", "ent_id_dest"):
            cur.execute(
                f"DELETE FROM relations a WHERE a.{col}=ANY(%s) AND EXISTS ("
                f"  SELECT 1 FROM relations s WHERE s.relation_type=a.relation_type "
                f"  AND s.ent_id_source = CASE WHEN a.ent_id_source=ANY(%s) THEN %s ELSE a.ent_id_source END "
                f"  AND s.ent_id_dest   = CASE WHEN a.ent_id_dest=ANY(%s)   THEN %s ELSE a.ent_id_dest END "
                f"  AND s.relation_id <> a.relation_id)",
                (absorbed, absorbed, survivor, absorbed, survivor))
            cur.execute(f"UPDATE relations SET {col}=%s WHERE {col}=ANY(%s)", (survivor, absorbed))
        # document_extractions.linked_entity_id
        cur.execute("UPDATE document_extractions SET linked_entity_id=%s "
                    "WHERE linked_entity_id=ANY(%s)", (survivor, absorbed))

    def _repoint_alias(self, cur, survivor: int, absorbed: List[int]) -> None:
        cur.execute("UPDATE entities_alias SET current_entity_id=%s "
                    "WHERE original_entity_id=ANY(%s)", (survivor, absorbed))

    def _tombstone(self, cur, survivor: int, absorbed: List[int]) -> None:
        # Keep the row (FK integrity + provenance); mark it merged so it's clearly dead.
        cur.execute(
            "UPDATE entities SET metadata = jsonb_set(metadata::jsonb, '{_merged_into}', "
            "to_jsonb(%s::int)) WHERE entity_id = ANY(%s)", (survivor, absorbed))


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v
