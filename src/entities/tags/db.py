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

import hashlib
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


def _parse_iso_datetime(value: object) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string into an aware `datetime`.

    Returns None on empty/invalid input. Naive timestamps are coerced to
    UTC so callers can compare them against the rest of the stream
    clock (also UTC-aware) without raising.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _latest_created_at(items: Iterable[SourceItem]) -> Optional[datetime]:
    """Max `created_at` across `items`, parsed to an aware datetime.

    Used to advance the stream clock per bundle: the latest item
    timestamp represents "how far the stream has progressed" so far,
    which is what retention and consistency checks should compare
    against. Returns None when no item has a parseable `created_at`
    (the repo's `effective_now()` then falls back to wall-clock)."""
    latest: Optional[datetime] = None
    for item in items:
        dt = _parse_iso_datetime(item.created_at)
        if dt is None:
            continue
        if latest is None or dt > latest:
            latest = dt
    return latest


def _verbatim_hash(verbatim: str) -> str:
    """Stable content hash for claim idempotency.

    Backs the `claim_assignments` unique key
    `(source_item_id, entity_id, org_id, event_id, cluster_id,
    verbatim_hash)`. SHA-256 hex (64 chars); deterministic and free of
    extension dependencies (PG 11+ has `sha256()` built in for the
    matching backfill).
    """
    return hashlib.sha256((verbatim or "").encode("utf-8")).hexdigest()


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
        simulate_assigned_at_from_document: bool = False,
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
        # Backfill-testing knob: when True, `assign()` overwrites the
        # dataclass's `assigned_at` with the bundle item's `created_at`
        # (article `date_created`), so the row's timestamp tracks the
        # simulated stream time rather than wall-clock. Comments inherit
        # their parent post's `created_at`. Off by default — live
        # streaming wants wall-clock.
        self._simulate_assigned_at = simulate_assigned_at_from_document
        # Stream clock — the "current document time" used by every
        # retention check (`retire_stale_entries`), windowing query
        # (`recent_bundle_assignments`), and audit stamp (`retired_at`
        # on `retire`/`merge`). Derived per-bundle from the max
        # `created_at` across the bundle's items (set by
        # `set_bundle_context`/`set_items_context`) so replaying an
        # old corpus uses the corpus's own timeline; falls back to
        # wall-clock when unset. NEVER use `datetime.now()` directly
        # inside this class — go through `effective_now()` so the
        # backfill/replay path stays consistent.
        self._stream_now: Optional[datetime] = None

    def set_bundle_context(
        self, bundle: ArticleBundle, query_id: Optional[int] = None,
    ) -> None:
        """Stash the current message's bundle + query_id for assignment
        enrichment, and advance the stream clock to the latest
        `created_at` in the bundle. Streaming pipelines call this once
        before each bundle; the matching `clear_bundle_context()` is
        optional — the next `set_*` overwrites."""
        self._bundle_items = {item.id: item for item in bundle.all_items}
        self._bundle_query_id = query_id
        self._stream_now = _latest_created_at(bundle.all_items)

    def set_items_context(
        self,
        items_by_id: dict[str, SourceItem],
        query_id: Optional[int] = None,
    ) -> None:
        """Multi-bundle variant of `set_bundle_context` — used by
        `BootstrapStep.run` to load every item from the seed corpus at
        once so the bootstrap-time `assign()` calls can enrich
        `parent_source_id` / `news_type` / `query_id` the same way
        streaming does. Advances the stream clock to the latest
        `created_at` across the loaded items."""
        self._bundle_items = dict(items_by_id)
        self._bundle_query_id = query_id
        self._stream_now = _latest_created_at(items_by_id.values())

    def clear_bundle_context(self) -> None:
        self._bundle_items = {}
        self._bundle_query_id = None
        # Stream clock is intentionally *not* cleared here. Once the
        # repo has observed a document timeline (per-bundle or via
        # `set_stream_now` at startup) we want subsequent calls — e.g.
        # a consistency pass that runs right after `clear_bundle_context`
        # — to keep using that last-known stream time rather than
        # silently snapping back to wall-clock. Call `set_stream_now(None)`
        # explicitly to opt back into wall-clock.

    def set_stream_now(self, dt: Optional[datetime]) -> None:
        """Explicitly seed the stream clock without a bundle context.
        Used by `build_repos` at startup to anchor retention against
        the latest `assigned_at` already in the DB (so the sweep runs
        at the stream's last-known position, not wall-clock today)."""
        self._stream_now = dt

    def effective_now(self) -> datetime:
        """The clock used by every time-based check or stamp on this
        repo. Returns the per-bundle stream time when set, else
        wall-clock UTC. All retention SQL routes through this — no
        SQL statement in this class should reference `now()` directly."""
        return self._stream_now or datetime.now(timezone.utc)

    def _enrich_assignment_from_context(self, a: StanceAssignment) -> None:
        """Fill missing dimension fields on `a` from the current bundle
        context. No-op when context isn't set.

        `parent_source_id` always resolves to the **post-level URL**:
        the parent post for comments, or the row's own
        `source_item_id` for root posts / articles. That lets queries
        group/join on a single column without `COALESCE` — at the cost
        of mildly stretching the "parent" name (a root row points at
        itself).

        Comments inherit `news_type` from their parent post: their own
        metadata only carries comment-level fields, but the post-level
        network type is stored on the parent's metadata.
        """
        if self._bundle_items:
            item = self._bundle_items.get(a.source_item_id)
            if item is not None:
                if a.parent_source_id is None:
                    a.parent_source_id = item.parent_source_id or item.id
                if a.news_type is None:
                    nt = (item.metadata or {}).get("news_type")
                    if not isinstance(nt, str) and item.parent_source_id:
                        parent = self._bundle_items.get(item.parent_source_id)
                        if parent and parent.metadata:
                            nt = parent.metadata.get("news_type")
                    if isinstance(nt, str):
                        a.news_type = nt
                if self._simulate_assigned_at:
                    sim = item.created_at
                    if not sim and item.parent_source_id:
                        parent = self._bundle_items.get(item.parent_source_id)
                        if parent:
                            sim = parent.created_at
                    if sim:
                        a.assigned_at = sim
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

        The unique index `stance_entries_scope_label_active_uniq` only
        fires on active rows (`retired_at IS NULL`), so an `add` that
        collides with a retired-and-preserved row of the same label
        inserts a fresh active row alongside the historical one.

        `origin_event_id` is accepted for in-memory parity but is not
        persisted — the column was dropped from the userdb schema (see
        `serialization_plan.md`).
        """
        del origin_event_id  # silence linters; intentionally unused
        stance_id = entry_id or make_entry_id(label, primary_type)
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Bare `ON CONFLICT DO NOTHING` so we swallow both the PK
            # collision (same `stance_id`) and the active-only partial
            # unique (`stance_entries_scope_label_active_uniq`).
            cur.execute(
                """
                INSERT INTO stance_entries
                    (stance_id, entity_id, org_id, label, description, primary_type)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING stance_id, label, description, primary_type, aliases, created_at
                """,
                (stance_id, self.entity_id, self.org_id, label, description, primary_type),
            )
            row = cur.fetchone()
            if row is None:
                # An existing active row collides on label. Re-fetch it.
                # Retired rows of the same label are skipped — the caller
                # gets a brand-new active row in that case.
                cur.execute(
                    """
                    SELECT stance_id, label, description, primary_type, aliases, created_at
                      FROM stance_entries
                     WHERE entity_id = %s AND org_id = %s
                       AND primary_type = %s AND label = %s
                       AND retired_at IS NULL
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
                ON CONFLICT ON CONSTRAINT stance_entries_pkey DO NOTHING
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
        """Upsert a stance assignment.

        One row per `(source_item_id, entity_id, org_id, stance_type)`
        — re-tagging the same item (e.g. a re-crawled article whose
        triage now lands on a different `stance_id`, or queue
        redelivery) updates the existing row's `stance_id` / `reason`
        / `assigned_at` / `event_id` in place. Item-context fields
        (`source_kind`, `parent_source_id`, `news_type`, `query_id`)
        stay because they describe the item, not the tagging decision.

        Returns True on either insert or update; False only when the
        target stance_id is unknown or mismatches the entry's
        `primary_type` (same validation contract as the in-memory
        version).
        """
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
                ON CONFLICT (source_item_id, entity_id, org_id, stance_type)
                DO UPDATE SET
                    stance_id   = EXCLUDED.stance_id,
                    reason      = EXCLUDED.reason,
                    assigned_at = EXCLUDED.assigned_at,
                    event_id    = EXCLUDED.event_id
                """,
                (
                    assignment.source_item_id, assignment.source_kind,
                    assignment.parent_source_id, assignment.news_type,
                    self.entity_id, self.org_id, assignment.query_id,
                    assignment.stance_id, assignment.stance_type,
                    assignment.event_id, assignment.reason,
                    assignment.assigned_at or self.effective_now().isoformat(),
                ),
            )
            return cur.rowcount > 0

    def rename(self, stance_id: str, new_label: str, new_description: str = "") -> bool:
        """Rename an entry, or — if `new_label` collides with another
        entry of the same `(entity_id, org_id, primary_type)` — fold
        the source into the colliding entry via `merge(src, dst)`.

        The merge fallback is what makes the LLM's rename proposals
        safe: when the tagger says "rename X to Y" and Y already
        exists, that's effectively "X and Y are the same idea, treat
        them as one." Returns True if either rename or merge was
        applied; False only when `stance_id` doesn't exist.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT label, primary_type
                  FROM stance_entries
                 WHERE stance_id = %s AND entity_id = %s AND org_id = %s
                   AND retired_at IS NULL
                """,
                (stance_id, self.entity_id, self.org_id),
            )
            row = cur.fetchone()
            if row is None:
                return False
            old_label, primary_type = row[0], row[1]

            # Trivial no-op rename — only update description if provided.
            if old_label == new_label:
                if new_description:
                    cur.execute(
                        "UPDATE stance_entries SET description = %s "
                        "WHERE stance_id = %s",
                        (new_description, stance_id),
                    )
                return True

            # Collision against an *active* entry only — retired rows
            # may keep their old label (the partial unique index allows
            # it) and a rename onto that label is allowed.
            cur.execute(
                """
                SELECT stance_id FROM stance_entries
                 WHERE entity_id = %s AND org_id = %s
                   AND primary_type = %s AND label = %s
                   AND retired_at IS NULL
                """,
                (self.entity_id, self.org_id, primary_type, new_label),
            )
            collision = cur.fetchone()

        if collision is not None and collision[0] != stance_id:
            logger.info(
                "rename collision on label=%r — merging %s into existing %s",
                new_label, stance_id, collision[0],
            )
            self.merge(stance_id, collision[0])
            return True

        # No collision — straightforward rename.
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

        Order matters: UPDATE assignments → dst onto src's label/alias
        history → soft-retire the src entry (`retired_at = now()`).
        Both entries must be active at call time; cross-`(entity, org)`
        and cross-`primary_type` merges are refused.

        The src row is preserved in the table so historical readers can
        still resolve the old `stance_id`; the partial unique index
        clears its label slot once `retired_at` is non-NULL, leaving
        room for a future re-add of the same label.
        """
        if src_id == dst_id:
            return 0
        with self.conn.cursor() as cur:
            # Cross-scope guard — never merge across (entity, org) lines,
            # and refuse to operate on already-retired rows.
            cur.execute(
                """
                SELECT stance_id, label, entity_id, org_id, primary_type
                  FROM stance_entries
                 WHERE stance_id IN (%s, %s)
                   AND retired_at IS NULL
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
            cur.execute(
                "UPDATE stance_entries SET retired_at = %s WHERE stance_id = %s",
                (self.effective_now(), src_id),
            )
        return moved

    def retire(self, stance_id: str) -> bool:
        """Soft-retire: stamp `retired_at = now()` on the entry.

        The row stays in `stance_entries` and its existing
        `stance_assignments` are preserved as-is so historical reads
        can still resolve `stance_id` against the retired entry.
        Active-catalog reads (`iter_entries`, `snapshot`, `summary`,
        etc.) filter the row out via `retired_at IS NULL`.

        Idempotent — re-retiring an already-retired entry returns
        False, same shape as the old hard-delete which returned False
        when the row was already gone.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE stance_entries
                   SET retired_at = %s
                 WHERE stance_id = %s AND entity_id = %s AND org_id = %s
                   AND retired_at IS NULL
                """,
                (self.effective_now(), stance_id, self.entity_id, self.org_id),
            )
            return cur.rowcount > 0

    # Alias — `delete` is the historical name on the in-memory side;
    # under soft-retire it's the same operation.
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

    def route_nulls_to_entry(
        self,
        stance_type: StanceType,
        entry_id: str,
        source_item_ids: Iterable[str],
    ) -> int:
        """Re-target null-stance assignments at an existing entry.

        Scoped to one stance_type and a set of source_item_ids — the
        DB equivalent of the in-memory loop the consistency pass's
        Stage 2 uses to claim orphan rows for a newly minted entry.
        Preserves `assigned_at`. Returns the number of rows updated.
        """
        ids = list(source_item_ids)
        if not ids:
            return 0
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE stance_assignments
                   SET stance_id = %s
                 WHERE entity_id = %s AND org_id = %s
                   AND stance_type = %s
                   AND stance_id IS NULL
                   AND source_item_id = ANY(%s)
                """,
                (entry_id, self.entity_id, self.org_id, stance_type, ids),
            )
            return cur.rowcount

    # ── Queries ───────────────────────────────────────────────────────

    def iter_entries(
        self, types: Optional[Iterable[StanceType]] = None
    ) -> Iterable[StanceEntry]:
        """Active-catalog entries only. Retired rows are filtered here
        and surfaced via `retired_entries` instead, matching the in-
        memory `StanceCatalog`'s split between `entries` (active) and
        `retired_entries` (history)."""
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if types is None:
                cur.execute(
                    """
                    SELECT stance_id, label, description, primary_type, aliases, created_at
                      FROM stance_entries
                     WHERE entity_id = %s AND org_id = %s
                       AND retired_at IS NULL
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
                       AND retired_at IS NULL
                    """,
                    (self.entity_id, self.org_id, list(types)),
                )
            for row in cur.fetchall():
                yield _row_to_stance_entry(row)

    @property
    def entries(self) -> dict[str, StanceEntry]:
        """Convenience snapshot of the catalog's active entries.

        **Hot-path warning.** Each access issues `SELECT * FROM
        stance_entries WHERE entity_id=… AND org_id=…`. Safe for
        one-shot printouts (stats, loop helpers), but anything that
        loops over it — counts, lookups by id — should call the
        explicit SQL methods (`count_catalogued_assignments`,
        `get_entries_by_ids`, `iter_zero_assignment_entries`) instead.
        """
        return {e.id: e for e in self.iter_entries()}

    @property
    def retired_entries(self) -> dict[str, StanceEntry]:
        """Entries with `retired_at IS NOT NULL` — kept on disk for
        history. Mirrors the in-memory `StanceCatalog.retired_entries`
        dict; new tagging cannot reach these (see `_primary_type_of`)
        but historical readers can still resolve their `stance_id` to
        a label."""
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT stance_id, label, description, primary_type, aliases, created_at
                  FROM stance_entries
                 WHERE entity_id = %s AND org_id = %s
                   AND retired_at IS NOT NULL
                """,
                (self.entity_id, self.org_id),
            )
            return {row["stance_id"]: _row_to_stance_entry(row) for row in cur.fetchall()}

    @property
    def assignments(self) -> list[StanceAssignment]:
        """Convenience snapshot of every assignment for this `(entity,
        org)` — including rows whose `stance_id` now points at a
        retired entry, since assignment history is preserved.

        **Hot-path warning.** Each access issues an unbounded table
        scan. Aggregations and filtered scans inside loops should call
        `count_catalogued_assignments(...)`, `assignments_for(...)`,
        or `recent_bundle_assignments(...)` instead.
        """
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

    def count_catalogued_assignments(
        self,
        *,
        stance_type: Optional[StanceType] = None,
    ) -> dict[str, int]:
        """`{stance_id: count}` for catalogued (non-NULL `stance_id`)
        assignments. Optional `stance_type` filter. Single SQL
        aggregation — preferred over walking `assignments` in a Python
        loop."""
        clauses = ["entity_id = %s", "org_id = %s", "stance_id IS NOT NULL"]
        params: list = [self.entity_id, self.org_id]
        if stance_type is not None:
            clauses.append("stance_type = %s")
            params.append(stance_type)
        sql = f"""
            SELECT stance_id, COUNT(*) AS n
              FROM stance_assignments
             WHERE {' AND '.join(clauses)}
             GROUP BY stance_id
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return {sid: int(n) for (sid, n) in cur.fetchall()}

    def iter_zero_assignment_entries(self) -> list[StanceEntry]:
        """Active entries with no assignments at all. Stage 1 of the
        consistency pass calls this to pick retire candidates without
        a Python-side count + iterate. The TTL-based
        `retire_stale_entries` typically covers these already (zero
        rows ⇒ no row newer than TTL), so this is a no-op in practice
        when retention runs first."""
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT stance_id, label, description, primary_type, aliases, created_at
                  FROM stance_entries e
                 WHERE entity_id = %s AND org_id = %s
                   AND retired_at IS NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM stance_assignments
                        WHERE stance_id = e.stance_id
                   )
                """,
                (self.entity_id, self.org_id),
            )
            return [_row_to_stance_entry(r) for r in cur.fetchall()]

    def get_entries_by_ids(
        self, stance_ids: Iterable[str],
    ) -> dict[str, StanceEntry]:
        """Look up a fixed set of active entries in one query. Used by
        Stage 3 to prefetch entries referenced by merge proposals so we
        don't re-scan the whole catalog per id. Retired entries are
        filtered out — Stage 3 only proposes merges on the active
        snapshot the LLM was given."""
        ids = list({sid for sid in stance_ids if sid})
        if not ids:
            return {}
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT stance_id, label, description, primary_type, aliases, created_at
                  FROM stance_entries
                 WHERE entity_id = %s AND org_id = %s
                   AND stance_id = ANY(%s)
                   AND retired_at IS NULL
                """,
                (self.entity_id, self.org_id, ids),
            )
            return {row["stance_id"]: _row_to_stance_entry(row) for row in cur.fetchall()}

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
        max_age_days: Optional[int] = None,
    ) -> list[StanceAssignment]:
        """Window assignments to the K most-recent bundles (= unique
        post/article `source_item_id`s) for this `(entity, org)`.

        When `max_age_days` is set, also drop bundles whose most-recent
        assignment is older than that many days — the result is at most
        K bundles, possibly fewer.

        Mirrors the CTE in `readme_tags.md` § DB mapping.
        """
        if n_bundles <= 0:
            return []
        kind_list = list(kinds)
        stream_now = self.effective_now()
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                WITH recent AS (
                    SELECT source_item_id, MAX(assigned_at) AS last_at
                      FROM stance_assignments
                     WHERE entity_id = %s AND org_id = %s
                       AND source_kind = ANY(%s)
                     GROUP BY source_item_id
                    HAVING %s::int IS NULL
                        OR MAX(assigned_at) >= %s::timestamptz
                                              - (%s::text || ' days')::interval
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
                (self.entity_id, self.org_id, kind_list,
                 max_age_days, stream_now, max_age_days,
                 int(n_bundles),
                 self.entity_id, self.org_id),
            )
            return [_row_to_stance_assignment(r) for r in cur.fetchall()]

    # ── Retention ─────────────────────────────────────────────────────

    def retire_stale_entries(self, ttl_days: int) -> int:
        """Soft-retire entries with no recent assignment.

        An entry is "stale" when its most-recent `stance_assignments`
        row is older than `ttl_days` (and entries with zero assignments
        at all are stale by definition). The "now" used for the
        comparison and the `retired_at` stamp is the repo's
        `effective_now()` — i.e. the latest `created_at` from the most
        recently processed bundle, falling back to wall-clock only
        when no stream context has been seen. Replaying an old corpus
        therefore retires against the corpus's own timeline rather
        than today's date.

        Returns the number of rows transitioned to retired. Idempotent:
        already-retired entries are skipped.
        """
        stream_now = self.effective_now()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE stance_entries e
                   SET retired_at = %s
                 WHERE e.entity_id = %s AND e.org_id = %s
                   AND e.retired_at IS NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM stance_assignments a
                        WHERE a.stance_id = e.stance_id
                          AND a.assigned_at >= %s::timestamptz
                                             - (%s::text || ' days')::interval
                   )
                """,
                (stream_now, self.entity_id, self.org_id,
                 stream_now, int(ttl_days)),
            )
            return cur.rowcount

    # ── Internal ──────────────────────────────────────────────────────

    def _primary_type_of(self, stance_id: str) -> Optional[str]:
        """Return the entry's `primary_type` if it exists AND is still
        active. Retired entries are invisible here so a fresh `assign()`
        cannot resurrect them — under the soft-retire model the row is
        kept for history but cannot accept new assignments. Redeliveries
        targeting an already-retired stance fall through `assign()`'s
        validation and are dropped (the previous assignment row, if any,
        is preserved untouched)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT primary_type
                  FROM stance_entries
                 WHERE stance_id = %s AND entity_id = %s AND org_id = %s
                   AND retired_at IS NULL
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
        simulate_assigned_at_from_document: bool = False,
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
        # See `StanceCatalogRepo._simulate_assigned_at` — same backfill
        # knob, mirrored here so `claim_assignments.extracted_at`
        # tracks the simulated stream time too.
        self._simulate_assigned_at = simulate_assigned_at_from_document
        # Stream clock — see `StanceCatalogRepo._stream_now`. Forwarded
        # from `ClaimCatalogStoreRepo` (which itself receives it per
        # bundle) so the wall-clock fallback in `_assigned_at_for`
        # uses document time instead of `datetime.now()`.
        self._stream_now: Optional[datetime] = None

    def set_bundle_context(
        self,
        items_by_id: dict[str, SourceItem],
        query_id: Optional[int],
        stream_now: Optional[datetime] = None,
    ) -> None:
        self._bundle_items = items_by_id
        self._bundle_query_id = query_id
        self._stream_now = stream_now

    def effective_now(self) -> datetime:
        return self._stream_now or datetime.now(timezone.utc)

    def _ctx_for(self, claim: RawClaim) -> tuple[Optional[int], Optional[str], Optional[str]]:
        """Resolve `(query_id, parent_source_id, news_type)` from the
        current bundle context. Each tuple element is None if the
        context is unset or doesn't carry that field.

        Comment-level claims inherit `news_type` from the parent post
        (their own metadata only carries comment-level fields).
        """
        query_id = self._bundle_query_id
        parent_source_id: Optional[str] = None
        news_type: Optional[str] = None
        if self._bundle_items:
            item = self._bundle_items.get(claim.source_item_id)
            if item is not None:
                # See `StanceCatalogRepo._enrich_assignment_from_context` —
                # post-level URL: parent for comments, self for roots.
                parent_source_id = item.parent_source_id or item.id
                nt = (item.metadata or {}).get("news_type")
                if not isinstance(nt, str) and item.parent_source_id:
                    parent = self._bundle_items.get(item.parent_source_id)
                    if parent and parent.metadata:
                        nt = parent.metadata.get("news_type")
                if isinstance(nt, str):
                    news_type = nt
        return query_id, parent_source_id, news_type

    def _assigned_at_for(self, source_item_id: str) -> str:
        """Resolve the timestamp written to `claim_assignments.extracted_at`.

        Backfill mode (`_simulate_assigned_at=True`): the bundle item's
        `created_at` (article `date_created`); comments fall back to
        their parent post's `created_at`. Live / non-simulated mode:
        the repo's stream clock (`effective_now()`) — which is the
        bundle's latest `created_at` when one is set, and wall-clock
        UTC only as the final fallback. Never wall-clock when a
        document timeline is available.
        """
        if self._simulate_assigned_at and self._bundle_items:
            item = self._bundle_items.get(source_item_id)
            if item is not None:
                sim = item.created_at
                if not sim and item.parent_source_id:
                    parent = self._bundle_items.get(item.parent_source_id)
                    if parent:
                        sim = parent.created_at
                if sim:
                    return sim
        return self.effective_now().isoformat()

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
            assigned_at=self._assigned_at_for(claim.source_item_id),
            org_id=self.org_id,
            query_id=query_id,
            parent_source_id=parent_source_id,
            news_type=news_type,
            importance=int(claim.importance or 1),
            importance_reason=str(claim.importance_reason or ""),
        )
        verbatim_hash = _verbatim_hash(claim.verbatim)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO claim_assignments
                    (source_item_id, source_kind, parent_source_id, news_type,
                     entity_id, org_id, query_id, event_id, cluster_id,
                     verbatim, verbatim_hash, importance, importance_reason,
                     extracted_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_item_id, entity_id, org_id, event_id,
                             cluster_id, verbatim_hash)
                DO NOTHING
                """,
                (
                    a.source_item_id, a.source_kind, a.parent_source_id, a.news_type,
                    self.entity_id, self.org_id, a.query_id, self.event_id, cluster_id,
                    a.verbatim, verbatim_hash, a.importance, a.importance_reason,
                    a.assigned_at,
                ),
            )
        # Return the dataclass whether the insert fired or a redelivery
        # collided with an existing row — the claim *is* in the cluster
        # either way, so the streaming caller's success branch is correct.
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
        simulate_assigned_at_from_document: bool = False,
    ):
        self.conn = conn
        self.entity_id = entity_id
        self.org_id = org_id
        # Forwarded to every `ClaimCatalogRepo` this store hands out so
        # the bundle-context enrichment flows through to per-event
        # repos without an extra wiring step.
        self._bundle_items: dict[str, SourceItem] = {}
        self._bundle_query_id: Optional[int] = None
        self._simulate_assigned_at = simulate_assigned_at_from_document
        # Stream clock — see `StanceCatalogRepo._stream_now`. Forwarded
        # to every per-event `ClaimCatalogRepo` this store builds so
        # `claim_assignments.extracted_at` uses document time when no
        # other timestamp is available.
        self._stream_now: Optional[datetime] = None

    def set_bundle_context(
        self, bundle: ArticleBundle, query_id: Optional[int] = None,
    ) -> None:
        self._bundle_items = {item.id: item for item in bundle.all_items}
        self._bundle_query_id = query_id
        self._stream_now = _latest_created_at(bundle.all_items)

    def clear_bundle_context(self) -> None:
        self._bundle_items = {}
        self._bundle_query_id = None
        # See `StanceCatalogRepo.clear_bundle_context` — stream clock
        # is kept so callers that do consistency work between bundles
        # don't snap back to wall-clock.

    def _build_repo(self, event_id: str) -> ClaimCatalogRepo:
        repo = ClaimCatalogRepo(
            self.conn, entity_id=self.entity_id, org_id=self.org_id, event_id=event_id,
            simulate_assigned_at_from_document=self._simulate_assigned_at,
        )
        if self._bundle_items or self._bundle_query_id is not None or self._stream_now is not None:
            repo.set_bundle_context(
                self._bundle_items, self._bundle_query_id, self._stream_now,
            )
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
                       bundles_processed_total, bundles_processed_since_last_pass,
                       last_consistency_pass_at, last_consistency_pass_count,
                       bootstrap_completed_at,
                       assignment_ttl_days,
                       consistency_pass_threshold_bundles,
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
        n_bundles: int = 1,
    ) -> None:
        """Add `n_bundles` to the per-`(entity, org)` bundle counters."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tags_entity_state
                    (entity_id, org_id,
                     bundles_processed_total, bundles_processed_since_last_pass)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (entity_id, org_id) DO UPDATE SET
                    bundles_processed_total =
                        tags_entity_state.bundles_processed_total + EXCLUDED.bundles_processed_total,
                    bundles_processed_since_last_pass =
                        tags_entity_state.bundles_processed_since_last_pass + EXCLUDED.bundles_processed_since_last_pass
                """,
                (entity_id, org_id, int(n_bundles), int(n_bundles)),
            )

    def mark_bootstrap_complete(
        self,
        entity_id: int,
        org_id: int,
        *,
        stream_now: Optional[datetime] = None,
    ) -> None:
        """Stamp `bootstrap_completed_at` with the stream clock.

        Callers should pass `stance_repo.effective_now()` so backfill
        runs stamp the corpus's timeline rather than wall-clock. None
        falls back to UTC `now()` for callers that don't have a stream
        context yet.
        """
        ts = stream_now or datetime.now(timezone.utc)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tags_entity_state (entity_id, org_id, bootstrap_completed_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (entity_id, org_id) DO UPDATE
                   SET bootstrap_completed_at = EXCLUDED.bootstrap_completed_at
                """,
                (entity_id, org_id, ts),
            )

    def mark_consistency_pass(
        self,
        entity_id: int,
        org_id: int,
        *,
        stream_now: Optional[datetime] = None,
    ) -> None:
        """Stamp `last_consistency_pass_at` with the stream clock and
        reset `bundles_processed_since_last_pass`. Caller passes the
        stream clock so the audit row stays consistent with the
        document timeline the pass operated on.
        """
        ts = stream_now or datetime.now(timezone.utc)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tags_entity_state (entity_id, org_id, last_consistency_pass_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (entity_id, org_id) DO UPDATE SET
                    last_consistency_pass_at = EXCLUDED.last_consistency_pass_at,
                    last_consistency_pass_count = tags_entity_state.last_consistency_pass_count + 1,
                    bundles_processed_since_last_pass = 0
                """,
                (entity_id, org_id, ts),
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
        customer.bundles_processed_total = int(state["bundles_processed_total"])
        customer.bundles_processed_since_last_pass = int(state["bundles_processed_since_last_pass"])
        customer.last_consistency_pass_at = state["last_consistency_pass_at"]
        customer.last_consistency_pass_count = int(state["last_consistency_pass_count"])
        customer.consistency_pass_threshold_bundles = int(state["consistency_pass_threshold_bundles"])
        customer.consistency_pass_threshold_days = int(state["consistency_pass_threshold_days"])
        return customer
