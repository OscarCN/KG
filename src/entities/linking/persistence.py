"""KgdbWriter — persist linked KG records into the unified kgdb Postgres DB.

Step Zero of the kg event-persistence work (see
``docs/todos/kgdb_event_persistence.md``): a decoupled, idempotent writer.
``write_linked(record)`` writes one linked event/entity in a single transaction
— the unit the future RabbitMQ streaming consumer will call per message.

Connection via ``KGDB_HOST/PORT/USER/PASSWORD/NAME`` (mirrors
``scripts/build_customer_fixture.py``). Category-aware: ``event`` records get an
``event_properties`` row, ``entity`` records don't. Themes are not linked
upstream, so they never reach here.

Write path per record (the corrected sketch from the TODO):
  1. resolve catalog ids: ``_supertype`` -> supertype ``entity_type_id`` (+
     ``entity_kind``); leaf ``event_type``/``entity_type`` -> child id.
  2. ``entities`` (metadata = full record + ``_link_id`` + ``_link_run``).
  3. ``entities_alias`` (original = current = entity_id).
  4. ``entity_types`` supertype row (+ child row when the leaf resolves).
  5. ``entity_locations`` from ``record["_geo"]`` (skipped when absent).
  6. ``event_properties`` (events only) — slack-widened confidence window.
  7. ``entities_documents`` one row per ``source_ids``.

``entity_id`` written everywhere is the alias ``original_entity_id`` (== entity_id
at create). Direct-FK caveat: ``entity_locations.entity_id`` /
``event_properties.event_id`` FK straight to ``entities.entity_id`` — fine on
create; a future in-DB merge step must rewrite them.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
from dateutil import parser as dateparser

logger = logging.getLogger(__name__)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return dateparser.isoparse(value)
    except (ValueError, TypeError):
        return None


class KgdbWriter:
    """Idempotent writer for linked KG records. One instance per run/stem."""

    def __init__(self, run_tag: str, conn=None):
        self.run_tag = run_tag
        self._conn = conn or self._connect()
        # (entity_type, parent_id|None) -> catalog row dict | None
        self._catalog: dict[tuple[str, Optional[int]], Optional[dict]] = {}
        self.written = 0
        self.skipped = 0
        self.dropped: dict[str, int] = {}

    @staticmethod
    def _connect():
        return psycopg2.connect(
            host=os.environ["KGDB_HOST"],
            port=int(os.environ.get("KGDB_PORT", 5432)),
            user=os.environ["KGDB_USER"],
            password=os.environ["KGDB_PASSWORD"],
            dbname=os.environ["KGDB_NAME"],
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _bump(self, key: str) -> None:
        self.dropped[key] = self.dropped.get(key, 0) + 1

    # -- catalog resolution (cached) ------------------------------------------

    def _resolve_supertype(self, cur, supertype: str) -> Optional[dict]:
        key = (supertype, None)
        if key not in self._catalog:
            cur.execute(
                "SELECT entity_type_id, entity_kind FROM entity_types_kinds_available "
                "WHERE entity_type = %s AND parent_entity_type IS NULL",
                (supertype,),
            )
            row = cur.fetchone()
            self._catalog[key] = dict(row) if row else None
        return self._catalog[key]

    def _resolve_child(self, cur, leaf: str, parent_id: int) -> Optional[int]:
        key = (leaf, parent_id)
        if key not in self._catalog:
            cur.execute(
                "SELECT entity_type_id FROM entity_types_kinds_available "
                "WHERE entity_type = %s AND parent_entity_type = %s",
                (leaf, parent_id),
            )
            row = cur.fetchone()
            self._catalog[key] = dict(row) if row else None
        cached = self._catalog[key]
        return cached["entity_type_id"] if cached else None

    # -- field helpers --------------------------------------------------------

    @staticmethod
    def _name_desc(record: dict) -> tuple[str, str]:
        """entities.name / .description are NOT NULL — synthesize when absent."""
        description = (record.get("description") or record.get("context") or "").strip()
        name = (record.get("name") or "").strip()
        if not name:
            name = (
                description[:120].strip()
                or record.get("event_type")
                or record.get("entity_type")
                or str(record.get("id"))
            )
        if not description:
            description = name
        return name, description

    @staticmethod
    def _confidence_window(record: dict) -> tuple[Optional[datetime], Optional[datetime]]:
        """Slack-widened window so a tstzrange && index reproduces the candidate
        date filter. Prefer per-source windows (start-slack .. end+slack), then
        the canonical date_range, then the publication date."""
        starts: list[datetime] = []
        ends: list[datetime] = []
        for w in record.get("_source_windows") or []:
            start = _parse_dt(w.get("start"))
            end = _parse_dt(w.get("end")) or start
            slack = int(w.get("slack_days") or 0)
            if start:
                starts.append(start - timedelta(days=slack))
            if end:
                ends.append(end + timedelta(days=slack))
        if starts and ends:
            return min(starts), max(ends)

        date_range = (record.get("date_range") or {}).get("date_range") or {}
        start = _parse_dt(date_range.get("start"))
        end = _parse_dt(date_range.get("end")) or start
        if start:
            return start, end

        pub = _parse_dt(record.get("publication_date"))
        return pub, pub

    # -- per-table writes -----------------------------------------------------

    @staticmethod
    def _write_location(cur, entity_id: int, geo: Optional[dict]) -> None:
        if not geo:
            return
        columns = ["entity_id", "formatted_name", "precision_level", "geoid"]
        placeholders = ["%s", "%s", "%s", "%s"]
        precision = geo.get("precision_level")
        values: list[Any] = [
            entity_id,
            geo.get("formatted_name") or "",
            None if precision is None else str(precision),
            geo.get("geoid"),
        ]
        for n in range(1, 8):
            for suffix in ("", "_id"):
                col = f"level_{n}{suffix}"
                columns.append(col)
                placeholders.append("%s")
                values.append(geo.get(col))
        lat, lon = geo.get("matched_lat"), geo.get("matched_lon")
        if lat is not None and lon is not None:
            columns.append("coords")
            placeholders.append("point(%s, %s)")
            values.extend([lon, lat])
        cur.execute(
            f"INSERT INTO entity_locations ({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)})",
            values,
        )

    def _write_event_properties(self, cur, entity_id: int, record: dict) -> None:
        start, end = self._confidence_window(record)
        cur.execute(
            "INSERT INTO event_properties (event_id, date_start, date_end, status) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (event_id) DO UPDATE SET "
            "date_start = EXCLUDED.date_start, date_end = EXCLUDED.date_end, "
            "status = EXCLUDED.status, record_updated = now()",
            (entity_id, start, end, record.get("status")),
        )

    @staticmethod
    def _write_documents(cur, entity_id: int, record: dict) -> None:
        pub = record.get("publication_date")
        news_type = record.get("news_type")
        for source_id in record.get("source_ids") or []:
            host = urlparse(source_id).netloc or None
            cur.execute(
                "INSERT INTO entities_documents "
                "(entity_id, doc_id, doc_index, doc_date_created, doc_source, news_type) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (entity_id, doc_id) DO NOTHING",
                (entity_id, source_id, "news", pub, host, news_type),
            )

    # -- public API -----------------------------------------------------------

    def write_linked(self, record: dict) -> Optional[int]:
        """Write one linked record in a single transaction. Returns the
        original_entity_id, or None if the record was dropped/errored."""
        link_id = record.get("id")
        supertype = record.get("_supertype")
        if not supertype:
            self._bump("no_supertype")
            return None
        try:
            with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Idempotency: skip if this link_id was already written for this run.
                cur.execute(
                    "SELECT entity_id FROM entities "
                    "WHERE metadata->>'_link_id' = %s AND metadata->>'_link_run' = %s",
                    (str(link_id), self.run_tag),
                )
                existing = cur.fetchone()
                if existing:
                    self.skipped += 1
                    return existing["entity_id"]

                super_row = self._resolve_supertype(cur, supertype)
                if not super_row:
                    self._conn.rollback()
                    self._bump(f"unseeded_supertype:{supertype}")
                    return None
                kind = super_row["entity_kind"]
                supertype_id = super_row["entity_type_id"]
                leaf = record.get("event_type") or record.get("entity_type")
                child_id = self._resolve_child(cur, leaf, supertype_id) if leaf else None

                # entities
                name, description = self._name_desc(record)
                metadata = dict(record)
                metadata["_link_id"] = link_id
                metadata["_link_run"] = self.run_tag
                cur.execute(
                    "INSERT INTO entities (name, description, added, metadata) "
                    "VALUES (%s, %s, %s, %s) RETURNING entity_id",
                    (name, description, datetime.now(timezone.utc),
                     psycopg2.extras.Json(metadata)),
                )
                entity_id = cur.fetchone()["entity_id"]

                # entities_alias (original == current == entity_id)
                cur.execute(
                    "INSERT INTO entities_alias "
                    "(original_entity_id, entity_alias, current_entity_id) "
                    "VALUES (%s, %s, %s) ON CONFLICT (original_entity_id) DO NOTHING",
                    (entity_id, name, entity_id),
                )

                # entity_types: supertype (+ child when resolved)
                cur.execute(
                    "INSERT INTO entity_types (entity_id, entity_type_id) VALUES (%s, %s)",
                    (entity_id, supertype_id),
                )
                if child_id:
                    cur.execute(
                        "INSERT INTO entity_types (entity_id, entity_type_id) VALUES (%s, %s)",
                        (entity_id, child_id),
                    )

                self._write_location(cur, entity_id, record.get("_geo"))
                if kind == "event":
                    self._write_event_properties(cur, entity_id, record)
                self._write_documents(cur, entity_id, record)

            self._conn.commit()
            self.written += 1
            return entity_id
        except Exception:
            self._conn.rollback()
            logger.exception("write_linked failed for link_id=%s", link_id)
            self._bump("error")
            return None

    def reset_run(self) -> int:
        """Delete all rows written under this run_tag (child -> parent order).
        Returns the number of entities removed."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT entity_id FROM entities WHERE metadata->>'_link_run' = %s",
                (self.run_tag,),
            )
            ids = [r[0] for r in cur.fetchall()]
            if ids:
                cur.execute("DELETE FROM entities_documents WHERE entity_id = ANY(%s)", (ids,))
                cur.execute("DELETE FROM event_properties WHERE event_id = ANY(%s)", (ids,))
                cur.execute("DELETE FROM entity_locations WHERE entity_id = ANY(%s)", (ids,))
                cur.execute("DELETE FROM entity_types WHERE entity_id = ANY(%s)", (ids,))
                cur.execute("DELETE FROM entities_alias WHERE original_entity_id = ANY(%s)", (ids,))
                cur.execute("DELETE FROM entities WHERE entity_id = ANY(%s)", (ids,))
        self._conn.commit()
        return len(ids)
