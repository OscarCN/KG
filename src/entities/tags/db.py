"""Userdb-backed repository for the tags subsystem.

Mirrors the in-memory `StanceCatalog` / `ClaimCatalogStore` / `Customer`
surface (`catalogs.py`, `models.py`) so the streaming pipeline, bootstrap
step, and consistency pass can swap implementations without touching the
call sites.

Connection model: every repo takes a psycopg2 connection in its
constructor; the caller owns the transaction lifecycle (`commit()` /
`rollback()`). This lets a RabbitMQ consumer wrap the whole bundle —
stance entries, stance assignments, claim cluster/assignment mutations,
counter bumps — in one TX and ack only after `conn.commit()` succeeds.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Iterable, Optional

import psycopg2
import psycopg2.extras

from src.entities.tags.catalogs import make_cluster_id, make_entry_id
from src.entities.tags.models import (
    ArticleBundle,
    ClaimAssignment,
    ClaimCluster,
    Customer,
    RawClaim,
    SourceItem,
    StanceAssignment,
    StanceEntry,
    StanceType,
    now_iso,
)


logger = logging.getLogger(__name__)


# ── Connection helper ─────────────────────────────────────────────────


def connect_userdb() -> psycopg2.extensions.connection:
    """Open a userdb connection from `USERDB_*` env vars.

    Same convention as `scripts/build_customer_fixture.py`'s `_connect`,
    swapped to the `USERDB_*` prefix. The caller owns the connection's
    transaction lifecycle.
    """
    return psycopg2.connect(
        host=os.environ["USERDB_HOST"],
        port=int(os.environ.get("USERDB_PORT", 5432)),
        user=os.environ["USERDB_USER"],
        password=os.environ["USERDB_PASSWORD"],
        dbname=os.environ["USERDB_NAME"],
    )


def _to_iso(value) -> str:
    """Postgres returns datetimes; the dataclasses store ISO strings."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


# ── Stance catalog ────────────────────────────────────────────────────


class StanceCatalogRepo:
    """Per-`(entity_id, org_id)` stance catalog backed by userdb.

    Method surface matches `catalogs.StanceCatalog`. Methods do not
    commit — the caller manages the transaction.
    """

    def __init__(
        self,
        conn: psycopg2.extensions.connection,
        *,
        entity_id: int,
        org_id: int,
    ):
        self.conn = conn
        self.entity_id = entity_id
        self.org_id = org_id
        # `customer_id` alias for compatibility with the in-memory
        # interface (which uses `customer_id` as the catalog's identity).
        self.customer_id = entity_id
        # Optional per-bundle enrichment context. Streaming consumers
        # call `set_bundle_context(bundle, query_id)` once per message
        # so that subsequent `assign()` calls auto-fill
        # `parent_source_id` / `news_type` / `query_id` on each
        # `StanceAssignment` from the bundle's items. Non-streaming
        # callers can ignore it — when unset, `assign()` writes
        # whatever the dataclass already carries.
        self._bundle_items: dict[str, SourceItem] = {}
        self._bundle_query_id: Optional[int] = None

    def set_bundle_context(
        self, bundle: ArticleBundle, query_id: Optional[int] = None,
    ) -> None:
        """Stash the current message's bundle + query_id for assignment
        enrichment. Streaming pipelines call this once before each
        bundle; the matching `clear_bundle_context()` is optional —
        the next `set_*` overwrites."""
        self._bundle_items = {item.id: item for item in bundle.all_items}
        self._bundle_query_id = query_id

    def clear_bundle_context(self) -> None:
        self._bundle_items = {}
        self._bundle_query_id = None

    def _enrich_assignment_from_context(self, a: StanceAssignment) -> None:
        """Fill missing dimension fields on `a` from the current bundle
        context. No-op when context isn't set."""
        if self._bundle_items:
            item = self._bundle_items.get(a.source_item_id)
            if item is not None:
                if a.parent_source_id is None:
                    a.parent_source_id = item.parent_source_id
                if a.news_type is None and item.metadata:
                    nt = item.metadata.get("news_type")
                    if isinstance(nt, str):
                        a.news_type = nt
        if a.query_id is None:
            a.query_id = self._bundle_query_id

    # ── Mutations ─────────────────────────────────────────────────────

    def add(
        self,
        label: str,
        description: str = "",
        *,
        primary_type: StanceType = "entity_stance",
        entry_id: Optional[str] = None,
        origin_event_id: Optional[str] = None,  # accepted but not persisted
    ) -> StanceEntry:
        """Create a new stance entry. Returns the created (or existing) row.

        `origin_event_id` is accepted for in-memory parity but is not
        persisted — the column was dropped from the userdb schema (see
        `serialization_plan.md`).
        """
        del origin_event_id  # silence linters; intentionally unused
        stance_id = entry_id or make_entry_id(label, primary_type)
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO stance_entries
                    (stance_id, entity_id, org_id, label, description, primary_type)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (entity_id, org_id, primary_type, label) DO NOTHING
                RETURNING stance_id, label, description, primary_type, aliases, created_at
                """,
                (stance_id, self.entity_id, self.org_id, label, description, primary_type),
            )
            row = cur.fetchone()
            if row is None:
                # An existing row collides on the unique key. Re-fetch it.
                cur.execute(
                    """
                    SELECT stance_id, label, description, primary_type, aliases, created_at
                      FROM stance_entries
                     WHERE entity_id = %s AND org_id = %s
                       AND primary_type = %s AND label = %s
                    """,
                    (self.entity_id, self.org_id, primary_type, label),
                )
                row = cur.fetchone()
        return _row_to_stance_entry(row)

    def add_entry(self, entry: StanceEntry) -> StanceEntry:
        """Insert a pre-constructed entry (used by tests / migrations)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO stance_entries
                    (stance_id, entity_id, org_id, label, description,
                     primary_type, aliases, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (stance_id) DO NOTHING
                """,
                (
                    entry.id, self.entity_id, self.org_id, entry.label,
                    entry.description, entry.primary_type,
                    psycopg2.extras.Json(list(entry.aliases)),
                    entry.created_at or now_iso(),
                ),
            )
        return entry

    def assign(self, assignment: StanceAssignment) -> bool:
        """Insert a stance assignment. Returns True on insert, False on
        conflict / type mismatch (same semantics as the in-memory version)."""
        self._enrich_assignment_from_context(assignment)
        if assignment.stance_id is not None:
            primary_type = self._primary_type_of(assignment.stance_id)
            if primary_type is None:
                logger.debug("drop assignment: unknown stance_id=%s", assignment.stance_id)
                return False
            if primary_type != assignment.stance_type:
                logger.debug(
                    "drop assignment: type mismatch stance_type=%s entry.primary_type=%s",
                    assignment.stance_type, primary_type,
                )
                return False
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO stance_assignments
                    (source_item_id, source_kind, parent_source_id, news_type,
                     entity_id, org_id, query_id, stance_id, stance_type,
                     event_id, reason, assigned_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    assignment.source_item_id, assignment.source_kind,
                    assignment.parent_source_id, assignment.news_type,
                    self.entity_id, self.org_id, assignment.query_id,
                    assignment.stance_id, assignment.stance_type,
                    assignment.event_id, assignment.reason,
                    assignment.assigned_at or now_iso(),
                ),
            )
            return cur.rowcount > 0

    def rename(self, stance_id: str, new_label: str, new_description: str = "") -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE stance_entries
                   SET label = %s,
                       description = CASE WHEN %s <> '' THEN %s ELSE description END,
                       aliases = CASE
                           WHEN label IS DISTINCT FROM %s
                           THEN aliases || to_jsonb(label)
                           ELSE aliases
                       END
                 WHERE stance_id = %s AND entity_id = %s AND org_id = %s
                """,
                (new_label, new_description, new_description, new_label,
                 stance_id, self.entity_id, self.org_id),
            )
            return cur.rowcount > 0

    def merge(self, src_id: str, dst_id: str) -> int:
        """Merge `src` into `dst`. Returns the number of moved assignments.

        Order matters: UPDATE assignments first, then move the src label
        into the dst's aliases, then DELETE the src entry. The
        `ON DELETE RESTRICT` FK protects against re-ordering bugs.
        """
        if src_id == dst_id:
            return 0
        with self.conn.cursor() as cur:
            # Cross-scope guard — never merge across (entity, org) lines.
            cur.execute(
                """
                SELECT stance_id, label, entity_id, org_id, primary_type
                  FROM stance_entries
                 WHERE stance_id IN (%s, %s)
                """,
                (src_id, dst_id),
            )
            rows = {r[0]: r for r in cur.fetchall()}
            src = rows.get(src_id)
            dst = rows.get(dst_id)
            if src is None or dst is None:
                return 0
            if (src[2], src[3]) != (self.entity_id, self.org_id):
                return 0
            if (dst[2], dst[3]) != (self.entity_id, self.org_id):
                return 0
            if src[4] != dst[4]:
                # cross-type merge — refuse, same as in-memory `merge`.
                return 0

            cur.execute(
                "UPDATE stance_assignments SET stance_id = %s WHERE stance_id = %s",
                (dst_id, src_id),
            )
            moved = cur.rowcount
            cur.execute(
                """
                UPDATE stance_entries
                   SET aliases = aliases || to_jsonb(%s::text)
                 WHERE stance_id = %s
                """,
                (src[1], dst_id),
            )
            cur.execute("DELETE FROM stance_entries WHERE stance_id = %s", (src_id,))
        return moved

    def retire(self, stance_id: str) -> bool:
        """Per the user spec ('if it has none, delete it'), retire is a
        guarded hard delete — succeeds only when zero assignments remain.
        The FK `ON DELETE RESTRICT` enforces the same invariant at the
        DB layer for any non-app caller."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM stance_entries
                 WHERE stance_id = %s AND entity_id = %s AND org_id = %s
                   AND NOT EXISTS (
                       SELECT 1 FROM stance_assignments
                        WHERE stance_id = stance_entries.stance_id
                   )
                """,
                (stance_id, self.entity_id, self.org_id),
            )
            return cur.rowcount > 0

    # Alias — semantically the same operation under the new model.
    delete = retire

    def reroute(self, from_id: str, to_id: str) -> int:
        if from_id == to_id:
            return 0
        # Sanity: the target must exist and be in the same scope.
        if self._primary_type_of(to_id) is None:
            return 0
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE stance_assignments
                   SET stance_id = %s
                 WHERE stance_id = %s
                   AND entity_id = %s AND org_id = %s
                """,
                (to_id, from_id, self.entity_id, self.org_id),
            )
            return cur.rowcount

    # ── Queries ───────────────────────────────────────────────────────

    def iter_entries(
        self, types: Optional[Iterable[StanceType]] = None
    ) -> Iterable[StanceEntry]:
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if types is None:
                cur.execute(
                    """
                    SELECT stance_id, label, description, primary_type, aliases, created_at
                      FROM stance_entries
                     WHERE entity_id = %s AND org_id = %s
                    """,
                    (self.entity_id, self.org_id),
                )
            else:
                cur.execute(
                    """
                    SELECT stance_id, label, description, primary_type, aliases, created_at
                      FROM stance_entries
                     WHERE entity_id = %s AND org_id = %s
                       AND primary_type = ANY(%s)
                    """,
                    (self.entity_id, self.org_id, list(types)),
                )
            for row in cur.fetchall():
                yield _row_to_stance_entry(row)

    @property
    def entries(self) -> dict[str, StanceEntry]:
        """Compatibility shim for code paths that iterate
        `catalog.entries.items()` (e.g. `consistency.py:140`). Each call
        round-trips to DB; consider switching call sites to
        `iter_entries()` and SQL counts when wiring is finalised."""
        return {e.id: e for e in self.iter_entries()}

    @property
    def retired_entries(self) -> dict[str, StanceEntry]:
        """No soft-retire under the new schema — always empty. Kept so
        legacy code paths that look here don't crash; they will fall
        through to whatever NULL-handling they already have."""
        return {}

    @property
    def assignments(self) -> list[StanceAssignment]:
        """Full per-`(entity, org)` assignment scan. After retention this
        is bounded by `assignment_ttl_days`. Used by
        `consistency.py:135` to count by stance_id; that can be replaced
        with a SQL aggregate in the follow-up."""
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT source_item_id, source_kind, parent_source_id, news_type,
                       entity_id, org_id, query_id, stance_id, stance_type,
                       event_id, reason, assigned_at
                  FROM stance_assignments
                 WHERE entity_id = %s AND org_id = %s
                 ORDER BY assigned_at DESC
                """,
                (self.entity_id, self.org_id),
            )
            return [_row_to_stance_assignment(r) for r in cur.fetchall()]

    def assignments_for(
        self,
        *,
        types: Optional[Iterable[StanceType]] = None,
        stance_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> Iterable[StanceAssignment]:
        clauses = ["entity_id = %s", "org_id = %s"]
        params: list = [self.entity_id, self.org_id]
        if types is not None:
            clauses.append("stance_type = ANY(%s)")
            params.append(list(types))
        if stance_id is not None:
            clauses.append("stance_id = %s")
            params.append(stance_id)
        if event_id is not None:
            clauses.append("event_id = %s")
            params.append(event_id)
        sql = f"""
            SELECT source_item_id, source_kind, parent_source_id, news_type,
                   entity_id, org_id, query_id, stance_id, stance_type,
                   event_id, reason, assigned_at
              FROM stance_assignments
             WHERE {' AND '.join(clauses)}
             ORDER BY assigned_at DESC
        """
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, tuple(params))
            for row in cur.fetchall():
                yield _row_to_stance_assignment(row)

    def summary(
        self,
        *,
        types: Optional[Iterable[StanceType]] = None,
        event_id: Optional[str] = None,
        top_n: Optional[int] = None,
    ) -> list[tuple[str, int]]:
        """`(label, count)` rows by count desc — JOIN over assignments."""
        clauses = ["a.entity_id = %s", "a.org_id = %s", "a.stance_id IS NOT NULL"]
        params: list = [self.entity_id, self.org_id]
        if types is not None:
            clauses.append("e.primary_type = ANY(%s)")
            params.append(list(types))
        if event_id is not None:
            clauses.append("a.event_id = %s")
            params.append(event_id)
        sql = f"""
            SELECT e.label, COUNT(*) AS n
              FROM stance_assignments a
              JOIN stance_entries    e ON e.stance_id = a.stance_id
             WHERE {' AND '.join(clauses)}
             GROUP BY e.label
             ORDER BY n DESC, e.label ASC
        """
        if top_n is not None:
            sql += " LIMIT %s"
            params.append(int(top_n))
        with self.conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [(label, int(n)) for (label, n) in cur.fetchall()]

    def snapshot(self, *, types: Optional[Iterable[StanceType]] = None) -> list[dict]:
        """Compact prompt-ready entry list."""
        return [
            {
                "id": e.id,
                "label": e.label,
                "description": e.description,
                "primary_type": e.primary_type,
            }
            for e in self.iter_entries(types=types)
        ]

    def recent_bundle_assignments(
        self,
        *,
        n_bundles: int,
        kinds: Iterable[str] = ("article", "user_post"),
    ) -> list[StanceAssignment]:
        """Window assignments to the K most-recent bundles (= unique
        post/article `source_item_id`s) for this `(entity, org)`.

        Mirrors the CTE in `readme_tags.md` § DB mapping.
        """
        if n_bundles <= 0:
            return []
        kind_list = list(kinds)
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                WITH recent AS (
                    SELECT source_item_id, MAX(assigned_at) AS last_at
                      FROM stance_assignments
                     WHERE entity_id = %s AND org_id = %s
                       AND source_kind = ANY(%s)
                     GROUP BY source_item_id
                     ORDER BY last_at DESC
                     LIMIT %s
                )
                SELECT a.source_item_id, a.source_kind, a.parent_source_id, a.news_type,
                       a.entity_id, a.org_id, a.query_id, a.stance_id, a.stance_type,
                       a.event_id, a.reason, a.assigned_at
                  FROM stance_assignments a
                  JOIN recent r USING (source_item_id)
                 WHERE a.entity_id = %s AND a.org_id = %s
                """,
                (self.entity_id, self.org_id, kind_list, int(n_bundles),
                 self.entity_id, self.org_id),
            )
            return [_row_to_stance_assignment(r) for r in cur.fetchall()]

    # ── Retention ─────────────────────────────────────────────────────

    def expire_old_assignments(self, ttl_days: int) -> int:
        """Step 1 of retention: delete assignments older than TTL."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM stance_assignments
                 WHERE entity_id = %s AND org_id = %s
                   AND assigned_at < now() - (%s::text || ' days')::interval
                """,
                (self.entity_id, self.org_id, int(ttl_days)),
            )
            return cur.rowcount

    def gc_orphan_entries(self) -> int:
        """Step 2 of retention: delete entries with zero assignments."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM stance_entries
                 WHERE entity_id = %s AND org_id = %s
                   AND NOT EXISTS (
                       SELECT 1 FROM stance_assignments
                        WHERE stance_id = stance_entries.stance_id
                   )
                """,
                (self.entity_id, self.org_id),
            )
            return cur.rowcount

    # ── Internal ──────────────────────────────────────────────────────

    def _primary_type_of(self, stance_id: str) -> Optional[str]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT primary_type
                  FROM stance_entries
                 WHERE stance_id = %s AND entity_id = %s AND org_id = %s
                """,
                (stance_id, self.entity_id, self.org_id),
            )
            row = cur.fetchone()
            return row[0] if row else None


def _row_to_stance_entry(row) -> StanceEntry:
    aliases = row["aliases"]
    if isinstance(aliases, str):
        aliases = json.loads(aliases)
    return StanceEntry(
        id=row["stance_id"],
        label=row["label"],
        description=row["description"] or "",
        primary_type=row["primary_type"],
        aliases=list(aliases or []),
        created_at=_to_iso(row["created_at"]),
        origin_event_id=None,
    )


def _row_to_stance_assignment(row) -> StanceAssignment:
    return StanceAssignment(
        source_item_id=row["source_item_id"],
        source_kind=row["source_kind"],
        customer_id=row["entity_id"],
        stance_id=row["stance_id"],
        stance_type=row["stance_type"],
        event_id=row["event_id"],
        reason=row["reason"] or "",
        assigned_at=_to_iso(row["assigned_at"]),
        org_id=row["org_id"],
        query_id=row["query_id"],
        parent_source_id=row["parent_source_id"],
        news_type=row["news_type"],
    )


# ── Claim catalog ─────────────────────────────────────────────────────


class ClaimCatalogRepo:
    """Per-`(entity_id, org_id, event_id)` claim cluster catalog."""

    def __init__(
        self,
        conn: psycopg2.extensions.connection,
        *,
        entity_id: int,
        org_id: int,
        event_id: str,
    ):
        self.conn = conn
        self.entity_id = entity_id
        self.org_id = org_id
        self.event_id = event_id
        # `customer_id` alias matches the in-memory `ClaimCatalog`.
        self.customer_id = entity_id
        # Per-bundle enrichment context (mirrors `StanceCatalogRepo`).
        # Populated indirectly via `ClaimCatalogStoreRepo.set_bundle_context`,
        # which forwards the items + query_id to every repo it hands out.
        self._bundle_items: dict[str, SourceItem] = {}
        self._bundle_query_id: Optional[int] = None

    def set_bundle_context(
        self,
        items_by_id: dict[str, SourceItem],
        query_id: Optional[int],
    ) -> None:
        self._bundle_items = items_by_id
        self._bundle_query_id = query_id

    def _ctx_for(self, claim: RawClaim) -> tuple[Optional[int], Optional[str], Optional[str]]:
        """Resolve `(query_id, parent_source_id, news_type)` from the
        current bundle context. Each tuple element is None if the
        context is unset or doesn't carry that field."""
        query_id = self._bundle_query_id
        parent_source_id: Optional[str] = None
        news_type: Optional[str] = None
        if self._bundle_items:
            item = self._bundle_items.get(claim.source_item_id)
            if item is not None:
                parent_source_id = item.parent_source_id
                if item.metadata:
                    nt = item.metadata.get("news_type")
                    if isinstance(nt, str):
                        news_type = nt
        return query_id, parent_source_id, news_type

    # ── Mutations ─────────────────────────────────────────────────────

    def assign(
        self,
        claim: RawClaim,
        cluster_id: str,
        *,
        query_id: Optional[int] = None,
        parent_source_id: Optional[str] = None,
        news_type: Optional[str] = None,
    ) -> Optional[ClaimAssignment]:
        """Insert one `claim_assignments` row referencing an existing cluster."""
        # Validate the cluster belongs to this scope.
        if not self._cluster_in_scope(cluster_id):
            logger.debug("drop claim assign: unknown cluster_id=%s", cluster_id)
            return None
        ctx_qid, ctx_parent, ctx_nt = self._ctx_for(claim)
        if query_id is None:
            query_id = ctx_qid
        if parent_source_id is None:
            parent_source_id = ctx_parent
        if news_type is None:
            news_type = ctx_nt
        a = ClaimAssignment(
            source_item_id=claim.source_item_id,
            source_kind=claim.source_kind,
            cluster_id=cluster_id,
            event_id=self.event_id,
            customer_id=self.entity_id,
            verbatim=claim.verbatim,
            assigned_at=now_iso(),
            org_id=self.org_id,
            query_id=query_id,
            parent_source_id=parent_source_id,
            news_type=news_type,
            importance=int(claim.importance or 1),
            importance_reason=str(claim.importance_reason or ""),
        )
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO claim_assignments
                    (source_item_id, source_kind, parent_source_id, news_type,
                     entity_id, org_id, query_id, event_id, cluster_id,
                     verbatim, importance, importance_reason, extracted_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    a.source_item_id, a.source_kind, a.parent_source_id, a.news_type,
                    self.entity_id, self.org_id, a.query_id, self.event_id, cluster_id,
                    a.verbatim, a.importance, a.importance_reason, a.assigned_at,
                ),
            )
        return a

    def create(
        self,
        claim: RawClaim,
        canonical: str,
        *,
        query_id: Optional[int] = None,
        parent_source_id: Optional[str] = None,
        news_type: Optional[str] = None,
    ) -> ClaimCluster:
        ctx_qid, ctx_parent, ctx_nt = self._ctx_for(claim)
        if query_id is None:
            query_id = ctx_qid
        if parent_source_id is None:
            parent_source_id = ctx_parent
        if news_type is None:
            news_type = ctx_nt
        cluster_id = make_cluster_id(canonical, self.event_id)
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO claim_clusters
                    (cluster_id, entity_id, org_id, event_id, canonical)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (entity_id, org_id, event_id, canonical) DO NOTHING
                RETURNING cluster_id, canonical, aliases, created_at, is_new, freshness_window_hours
                """,
                (cluster_id, self.entity_id, self.org_id, self.event_id, canonical),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    SELECT cluster_id, canonical, aliases, created_at, is_new, freshness_window_hours
                      FROM claim_clusters
                     WHERE entity_id = %s AND org_id = %s AND event_id = %s AND canonical = %s
                    """,
                    (self.entity_id, self.org_id, self.event_id, canonical),
                )
                row = cur.fetchone()
                cluster_id = row["cluster_id"]
        cluster = _row_to_claim_cluster(row, entity_id=self.entity_id, event_id=self.event_id)
        self.assign(
            claim, cluster.id,
            query_id=query_id,
            parent_source_id=parent_source_id,
            news_type=news_type,
        )
        return cluster

    def rename(self, cluster_id: str, new_canonical: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE claim_clusters
                   SET canonical = %s,
                       aliases = CASE
                           WHEN canonical IS DISTINCT FROM %s
                           THEN aliases || to_jsonb(canonical)
                           ELSE aliases
                       END
                 WHERE cluster_id = %s AND entity_id = %s AND org_id = %s
                   AND event_id = %s
                """,
                (new_canonical, new_canonical, cluster_id,
                 self.entity_id, self.org_id, self.event_id),
            )
            return cur.rowcount > 0

    def merge(self, src_id: str, dst_id: str) -> int:
        if src_id == dst_id:
            return 0
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT cluster_id, canonical, entity_id, org_id, event_id
                  FROM claim_clusters
                 WHERE cluster_id IN (%s, %s)
                """,
                (src_id, dst_id),
            )
            rows = {r[0]: r for r in cur.fetchall()}
            src = rows.get(src_id)
            dst = rows.get(dst_id)
            if src is None or dst is None:
                return 0
            scope = (self.entity_id, self.org_id, self.event_id)
            if (src[2], src[3], src[4]) != scope or (dst[2], dst[3], dst[4]) != scope:
                return 0
            cur.execute(
                "UPDATE claim_assignments SET cluster_id = %s WHERE cluster_id = %s",
                (dst_id, src_id),
            )
            moved = cur.rowcount
            cur.execute(
                """
                UPDATE claim_clusters
                   SET aliases = aliases || to_jsonb(%s::text)
                 WHERE cluster_id = %s
                """,
                (src[1], dst_id),
            )
            cur.execute("DELETE FROM claim_clusters WHERE cluster_id = %s", (src_id,))
        return moved

    # ── Queries ───────────────────────────────────────────────────────

    def iter_clusters(self) -> Iterable[ClaimCluster]:
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT cluster_id, canonical, aliases, created_at,
                       is_new, freshness_window_hours
                  FROM claim_clusters
                 WHERE entity_id = %s AND org_id = %s AND event_id = %s
                """,
                (self.entity_id, self.org_id, self.event_id),
            )
            for row in cur.fetchall():
                yield _row_to_claim_cluster(row, entity_id=self.entity_id, event_id=self.event_id)

    @property
    def clusters(self) -> dict[str, ClaimCluster]:
        return {c.id: c for c in self.iter_clusters()}

    def summary(self) -> list[tuple[str, int, int, bool]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.canonical,
                       COUNT(a.record_id) AS n_members,
                       COALESCE(MAX(a.importance), 0) AS importance_max,
                       c.is_new
                  FROM claim_clusters c
             LEFT JOIN claim_assignments a ON a.cluster_id = c.cluster_id
                 WHERE c.entity_id = %s AND c.org_id = %s AND c.event_id = %s
                 GROUP BY c.cluster_id, c.canonical, c.is_new
                 ORDER BY n_members DESC, importance_max DESC
                """,
                (self.entity_id, self.org_id, self.event_id),
            )
            return [
                (canonical, int(n), int(imp_max), bool(is_new))
                for (canonical, n, imp_max, is_new) in cur.fetchall()
            ]

    # ── Internal ──────────────────────────────────────────────────────

    def _cluster_in_scope(self, cluster_id: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM claim_clusters
                 WHERE cluster_id = %s AND entity_id = %s AND org_id = %s
                   AND event_id = %s
                """,
                (cluster_id, self.entity_id, self.org_id, self.event_id),
            )
            return cur.fetchone() is not None


class ClaimCatalogStoreRepo:
    """Registry of `ClaimCatalogRepo` instances keyed by `event_id`.

    Mirrors `ClaimCatalogStore`. Each `get_or_create` returns a thin
    handle bound to `(entity_id, org_id, event_id)`; the cluster rows
    themselves live in `claim_clusters`.
    """

    def __init__(
        self,
        conn: psycopg2.extensions.connection,
        *,
        entity_id: int,
        org_id: int,
    ):
        self.conn = conn
        self.entity_id = entity_id
        self.org_id = org_id
        # Forwarded to every `ClaimCatalogRepo` this store hands out so
        # the bundle-context enrichment flows through to per-event
        # repos without an extra wiring step.
        self._bundle_items: dict[str, SourceItem] = {}
        self._bundle_query_id: Optional[int] = None

    def set_bundle_context(
        self, bundle: ArticleBundle, query_id: Optional[int] = None,
    ) -> None:
        self._bundle_items = {item.id: item for item in bundle.all_items}
        self._bundle_query_id = query_id

    def clear_bundle_context(self) -> None:
        self._bundle_items = {}
        self._bundle_query_id = None

    def _build_repo(self, event_id: str) -> ClaimCatalogRepo:
        repo = ClaimCatalogRepo(
            self.conn, entity_id=self.entity_id, org_id=self.org_id, event_id=event_id,
        )
        if self._bundle_items or self._bundle_query_id is not None:
            repo.set_bundle_context(self._bundle_items, self._bundle_query_id)
        return repo

    def get_or_create(self, customer_id: int, event_id: str) -> ClaimCatalogRepo:
        # `customer_id` parameter kept for API parity with
        # `ClaimCatalogStore.get_or_create(customer_id, event_id)`. Callers
        # should pass `self.entity_id`; mismatch is a bug.
        if customer_id != self.entity_id:
            raise ValueError(
                f"customer_id={customer_id} mismatches repo scope entity_id={self.entity_id}"
            )
        return self._build_repo(event_id)

    def get(self, customer_id: int, event_id: str) -> Optional[ClaimCatalogRepo]:
        if customer_id != self.entity_id:
            return None
        # Only return a handle if at least one cluster exists for this event.
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM claim_clusters
                 WHERE entity_id = %s AND org_id = %s AND event_id = %s
                 LIMIT 1
                """,
                (self.entity_id, self.org_id, event_id),
            )
            if cur.fetchone() is None:
                return None
        return self._build_repo(event_id)


def _row_to_claim_cluster(row, *, entity_id: int, event_id: str) -> ClaimCluster:
    aliases = row["aliases"]
    if isinstance(aliases, str):
        aliases = json.loads(aliases)
    return ClaimCluster(
        id=row["cluster_id"],
        customer_id=entity_id,
        event_id=event_id,
        canonical=row["canonical"],
        members=[],  # filled lazily from claim_assignments if needed
        aliases=list(aliases or []),
        created_at=_to_iso(row["created_at"]),
        is_new=bool(row["is_new"]),
        freshness_window_hours=int(row["freshness_window_hours"] or 24),
    )


# ── Entity state (counters + thresholds) ──────────────────────────────


class EntityStateRepo:
    """Reads/writes `tags_entity_state` rows.

    A consumer loads counters once per message (`load`), bumps them in
    the same TX as the bundle's stance/claim writes (`bump_streaming`),
    and resets the `_since_last_pass` counters after a consistency pass
    completes (`mark_consistency_pass`).
    """

    def __init__(self, conn: psycopg2.extensions.connection):
        self.conn = conn

    def load(self, entity_id: int, org_id: int) -> Optional[dict]:
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT entity_id, org_id,
                       items_processed_total, items_processed_since_last_pass,
                       bundles_processed_total, bundles_processed_since_last_pass,
                       last_consistency_pass_at, last_consistency_pass_count,
                       bootstrap_completed_at,
                       assignment_ttl_days,
                       consistency_pass_threshold_items,
                       consistency_pass_threshold_days
                  FROM tags_entity_state
                 WHERE entity_id = %s AND org_id = %s
                """,
                (entity_id, org_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            row = dict(row)
            row["last_consistency_pass_at"] = (
                _to_iso(row["last_consistency_pass_at"])
                if row["last_consistency_pass_at"] else None
            )
            row["bootstrap_completed_at"] = (
                _to_iso(row["bootstrap_completed_at"])
                if row["bootstrap_completed_at"] else None
            )
            return row

    def ensure(self, entity_id: int, org_id: int) -> None:
        """Insert a default state row if missing. No-op when one exists."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tags_entity_state (entity_id, org_id)
                VALUES (%s, %s)
                ON CONFLICT (entity_id, org_id) DO NOTHING
                """,
                (entity_id, org_id),
            )

    def bump_streaming(
        self,
        entity_id: int,
        org_id: int,
        *,
        n_items: int = 1,
        n_bundles: int = 1,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tags_entity_state
                    (entity_id, org_id,
                     items_processed_total, items_processed_since_last_pass,
                     bundles_processed_total, bundles_processed_since_last_pass)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (entity_id, org_id) DO UPDATE SET
                    items_processed_total =
                        tags_entity_state.items_processed_total + EXCLUDED.items_processed_total,
                    items_processed_since_last_pass =
                        tags_entity_state.items_processed_since_last_pass + EXCLUDED.items_processed_since_last_pass,
                    bundles_processed_total =
                        tags_entity_state.bundles_processed_total + EXCLUDED.bundles_processed_total,
                    bundles_processed_since_last_pass =
                        tags_entity_state.bundles_processed_since_last_pass + EXCLUDED.bundles_processed_since_last_pass
                """,
                (entity_id, org_id, int(n_items), int(n_items),
                 int(n_bundles), int(n_bundles)),
            )

    def mark_bootstrap_complete(self, entity_id: int, org_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tags_entity_state (entity_id, org_id, bootstrap_completed_at)
                VALUES (%s, %s, now())
                ON CONFLICT (entity_id, org_id) DO UPDATE
                   SET bootstrap_completed_at = now()
                """,
                (entity_id, org_id),
            )

    def mark_consistency_pass(self, entity_id: int, org_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tags_entity_state (entity_id, org_id, last_consistency_pass_at)
                VALUES (%s, %s, now())
                ON CONFLICT (entity_id, org_id) DO UPDATE SET
                    last_consistency_pass_at = now(),
                    last_consistency_pass_count = tags_entity_state.last_consistency_pass_count + 1,
                    items_processed_since_last_pass = 0,
                    bundles_processed_since_last_pass = 0
                """,
                (entity_id, org_id),
            )

    def get_ttl_days(self, entity_id: int, org_id: int) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT assignment_ttl_days
                  FROM tags_entity_state
                 WHERE entity_id = %s AND org_id = %s
                """,
                (entity_id, org_id),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 4

    def apply_counters_to(self, customer: Customer, org_id: int) -> Customer:
        """Hydrate a `Customer` dataclass with counters from the DB.

        Returns the same `customer` object after mutating its counter
        fields. Useful when bridging an in-memory `Customer` (loaded
        from the kgdb fixture) with userdb-side state.
        """
        state = self.load(customer.entity_id, org_id)
        if state is None:
            return customer
        customer.items_processed_total = int(state["items_processed_total"])
        customer.items_processed_since_last_pass = int(state["items_processed_since_last_pass"])
        customer.bundles_processed_total = int(state["bundles_processed_total"])
        customer.bundles_processed_since_last_pass = int(state["bundles_processed_since_last_pass"])
        customer.last_consistency_pass_at = state["last_consistency_pass_at"]
        customer.last_consistency_pass_count = int(state["last_consistency_pass_count"])
        customer.consistency_pass_threshold_items = int(state["consistency_pass_threshold_items"])
        customer.consistency_pass_threshold_days = int(state["consistency_pass_threshold_days"])
        return customer
