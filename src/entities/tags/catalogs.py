"""In-memory catalogs for stances and claims.

Both stores are pure data: no LLM calls, no I/O. They are mutated by
`StanceUpdater` / `ClaimUpdater` (see `tagging.py`) and by
`ConsistencyPassStep` (see `consistency.py`).

`StanceCatalog` keeps a single flat `entries: dict[str, StanceEntry]`;
type-scoped queries filter on `primary_type` (see `tags_design.md` §5.4).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from typing import Iterable, Optional

from src.entities.tags.models import (
    ClaimAssignment,
    ClaimCluster,
    RawClaim,
    StanceAssignment,
    StanceEntry,
    StanceType,
    now_iso,
)


logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────


def _slugify(text: str, *, max_len: int = 64) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "-", text)
    text = text[:max_len].strip("-")
    return text or "entry"


def make_entry_id(label: str, primary_type: str) -> str:
    suffix = uuid.uuid4().hex[:6]
    return f"{primary_type}__{_slugify(label)}__{suffix}"


def make_cluster_id(canonical: str, event_id: str) -> str:
    suffix = uuid.uuid4().hex[:6]
    return f"{event_id}__{_slugify(canonical)}__{suffix}"


# ── Stance catalog ──────────────────────────────────────────────────────


class StanceCatalog:
    """Per-customer typed stance catalog (flat entries dict)."""

    def __init__(self, customer_id: int):
        self.customer_id = customer_id
        self.entries: dict[str, StanceEntry] = {}
        self.retired_entries: dict[str, StanceEntry] = {}
        self.assignments: list[StanceAssignment] = []

    # ── Mutations ──────────────────────────────────────────────────────

    def add_entry(self, entry: StanceEntry) -> StanceEntry:
        if entry.id in self.entries:
            logger.warning("stance entry %s already present; skip add", entry.id)
            return self.entries[entry.id]
        self.entries[entry.id] = entry
        return entry

    def add(
        self,
        label: str,
        description: str = "",
        *,
        primary_type: StanceType = "entity_stance",
        entry_id: Optional[str] = None,
        origin_event_id: Optional[str] = None,
    ) -> StanceEntry:
        entry = StanceEntry(
            id=entry_id or make_entry_id(label, primary_type),
            label=label,
            description=description,
            primary_type=primary_type,
            origin_event_id=origin_event_id,
        )
        return self.add_entry(entry)

    def assign(self, assignment: StanceAssignment) -> bool:
        """Append the assignment after validation. Returns True on success."""
        if assignment.stance_id is not None:
            entry = self.entries.get(assignment.stance_id)
            if entry is None:
                logger.debug(
                    "drop assignment: unknown stance_id=%s", assignment.stance_id
                )
                return False
            if entry.primary_type != assignment.stance_type:
                logger.debug(
                    "drop assignment: type mismatch stance_type=%s entry.primary_type=%s",
                    assignment.stance_type,
                    entry.primary_type,
                )
                return False
        self.assignments.append(assignment)
        return True

    def rename(self, stance_id: str, new_label: str, new_description: str = "") -> bool:
        entry = self.entries.get(stance_id)
        if entry is None:
            return False
        if entry.label and entry.label != new_label:
            entry.aliases.append(entry.label)
        entry.label = new_label
        if new_description:
            entry.description = new_description
        return True

    def merge(self, src_id: str, dst_id: str) -> int:
        if src_id == dst_id:
            return 0
        if dst_id not in self.entries or src_id not in self.entries:
            return 0
        moved = self.reroute(src_id, dst_id)
        # Append the source label to dst aliases for history.
        src = self.entries.pop(src_id, None)
        if src is not None:
            self.entries[dst_id].aliases.append(src.label)
        return moved

    def retire(self, stance_id: str) -> bool:
        entry = self.entries.pop(stance_id, None)
        if entry is None:
            return False
        self.retired_entries[stance_id] = entry
        return True

    def reroute(self, from_id: str, to_id: str) -> int:
        if from_id == to_id:
            return 0
        if to_id not in self.entries and to_id not in self.retired_entries:
            return 0
        n = 0
        for a in self.assignments:
            if a.stance_id == from_id:
                a.stance_id = to_id
                n += 1
        return n

    # ── Queries ────────────────────────────────────────────────────────

    def iter_entries(
        self, types: Optional[Iterable[StanceType]] = None
    ) -> Iterable[StanceEntry]:
        if types is None:
            return iter(self.entries.values())
        wanted = set(types)
        return (e for e in self.entries.values() if e.primary_type in wanted)

    def summary(
        self,
        *,
        types: Optional[Iterable[StanceType]] = None,
        event_id: Optional[str] = None,
        top_n: Optional[int] = None,
    ) -> list[tuple[str, int]]:
        """Return [(label, count), ...] sorted by count desc.

        Filters: by stance type (entry's `primary_type`) and/or by `event_id`
        (assignment's filter dimension).
        """
        wanted_types = set(types) if types is not None else None
        counter: Counter[str] = Counter()
        for a in self.assignments:
            if a.stance_id is None:
                continue
            entry = self.entries.get(a.stance_id) or self.retired_entries.get(a.stance_id)
            if entry is None:
                continue
            if wanted_types is not None and entry.primary_type not in wanted_types:
                continue
            if event_id is not None and a.event_id != event_id:
                continue
            counter[entry.label] += 1
        items = counter.most_common(top_n) if top_n else counter.most_common()
        return items

    def recent_bundle_assignments(
        self,
        *,
        n_bundles: int,
        kinds: Iterable[str] = ("article", "user_post"),
    ) -> list[StanceAssignment]:
        """Window the assignments to the K most-recent bundles.

        A bundle is identified by a unique `source_item_id` among
        assignments whose `source_kind` is in `kinds` (default: posts
        and articles, excluding comments). We rank those source ids by
        `max(assigned_at)` descending, take the top `n_bundles`, and
        return EVERY assignment (any kind, any stance_id including
        null) belonging to that source-id set.

        Maps cleanly to SQL later:
            WITH recent AS (
                SELECT source_item_id, MAX(assigned_at) AS last_at
                FROM stance_assignments
                WHERE source_kind IN (:kinds)
                GROUP BY source_item_id
                ORDER BY last_at DESC
                LIMIT :n_bundles
            )
            SELECT a.* FROM stance_assignments a
            JOIN recent USING (source_item_id);
        """
        if n_bundles <= 0:
            return []
        wanted_kinds = set(kinds)
        latest_by_sid: dict[str, str] = {}
        for a in self.assignments:
            if a.source_kind not in wanted_kinds:
                continue
            prev = latest_by_sid.get(a.source_item_id)
            if prev is None or a.assigned_at > prev:
                latest_by_sid[a.source_item_id] = a.assigned_at
        if not latest_by_sid:
            return []
        ranked = sorted(latest_by_sid.items(), key=lambda kv: kv[1], reverse=True)
        keep_ids = {sid for sid, _ in ranked[:n_bundles]}
        return [a for a in self.assignments if a.source_item_id in keep_ids]

    def assignments_for(
        self,
        *,
        types: Optional[Iterable[StanceType]] = None,
        stance_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> Iterable[StanceAssignment]:
        wanted_types = set(types) if types is not None else None
        for a in self.assignments:
            if wanted_types is not None and a.stance_type not in wanted_types:
                continue
            if stance_id is not None and a.stance_id != stance_id:
                continue
            if event_id is not None and a.event_id != event_id:
                continue
            yield a

    def snapshot(
        self, *, types: Optional[Iterable[StanceType]] = None
    ) -> list[dict]:
        """Compact catalog payload for prompts (id, label, description, primary_type)."""
        out = []
        for entry in self.iter_entries(types):
            out.append(
                {
                    "id": entry.id,
                    "label": entry.label,
                    "description": entry.description,
                    "primary_type": entry.primary_type,
                }
            )
        return out

    # ── Persistence ────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "entries": [e.to_dict() for e in self.entries.values()],
            "retired_entries": [e.to_dict() for e in self.retired_entries.values()],
            "assignments": [a.to_dict() for a in self.assignments],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "StanceCatalog":
        cat = cls(customer_id=int(payload["customer_id"]))
        for raw in payload.get("entries") or []:
            entry = StanceEntry.from_dict(raw)
            cat.entries[entry.id] = entry
        for raw in payload.get("retired_entries") or []:
            entry = StanceEntry.from_dict(raw)
            cat.retired_entries[entry.id] = entry
        for raw in payload.get("assignments") or []:
            cat.assignments.append(StanceAssignment.from_dict(raw))
        return cat


# ── Claim catalog ───────────────────────────────────────────────────────


class ClaimCatalog:
    """Per-(customer_id, event_id) claim cluster catalog."""

    def __init__(self, customer_id: int, event_id: str):
        self.customer_id = customer_id
        self.event_id = event_id
        self.clusters: dict[str, ClaimCluster] = {}
        self.assignments: list[ClaimAssignment] = []

    def assign(self, claim: RawClaim, cluster_id: str) -> Optional[ClaimAssignment]:
        cluster = self.clusters.get(cluster_id)
        if cluster is None:
            logger.debug("drop claim assign: unknown cluster_id=%s", cluster_id)
            return None
        cluster.add_member(claim)
        a = ClaimAssignment(
            source_item_id=claim.source_item_id,
            source_kind=claim.source_kind,
            cluster_id=cluster.id,
            event_id=cluster.event_id,
            customer_id=cluster.customer_id,
            verbatim=claim.verbatim,
            assigned_at=now_iso(),
        )
        self.assignments.append(a)
        return a

    def create(self, claim: RawClaim, canonical: str) -> ClaimCluster:
        cluster = ClaimCluster(
            id=make_cluster_id(canonical, self.event_id),
            customer_id=self.customer_id,
            event_id=self.event_id,
            canonical=canonical,
        )
        self.clusters[cluster.id] = cluster
        self.assign(claim, cluster.id)
        return cluster

    def rename(self, cluster_id: str, new_canonical: str) -> bool:
        cluster = self.clusters.get(cluster_id)
        if cluster is None:
            return False
        cluster.rename(new_canonical)
        return True

    def merge(self, src_id: str, dst_id: str) -> int:
        if src_id == dst_id:
            return 0
        src = self.clusters.get(src_id)
        dst = self.clusters.get(dst_id)
        if src is None or dst is None:
            return 0
        moved = 0
        for member in src.members:
            dst.add_member(member)
            moved += 1
        for a in self.assignments:
            if a.cluster_id == src_id:
                a.cluster_id = dst_id
        if src.canonical and src.canonical != dst.canonical:
            dst.aliases.append(src.canonical)
        del self.clusters[src_id]
        return moved

    def summary(self) -> list[tuple[str, int, int, bool]]:
        """[(canonical, n_members, importance_max, is_new), ...] by n_members desc."""
        rows = [
            (c.canonical, len(c.members), c.importance_max, c.is_new)
            for c in self.clusters.values()
        ]
        rows.sort(key=lambda r: (r[1], r[2]), reverse=True)
        return rows

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "event_id": self.event_id,
            "clusters": [c.to_dict() for c in self.clusters.values()],
            "assignments": [a.to_dict() for a in self.assignments],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ClaimCatalog":
        cat = cls(
            customer_id=int(payload["customer_id"]),
            event_id=str(payload["event_id"]),
        )
        for raw in payload.get("clusters") or []:
            cluster = ClaimCluster.from_dict(raw)
            cat.clusters[cluster.id] = cluster
        for raw in payload.get("assignments") or []:
            cat.assignments.append(ClaimAssignment.from_dict(raw))
        return cat


class ClaimCatalogStore:
    """Registry of `ClaimCatalog` keyed by (customer_id, event_id)."""

    def __init__(self):
        self.catalogs: dict[tuple[int, str], ClaimCatalog] = {}

    def get_or_create(self, customer_id: int, event_id: str) -> ClaimCatalog:
        key = (customer_id, event_id)
        cat = self.catalogs.get(key)
        if cat is None:
            cat = ClaimCatalog(customer_id=customer_id, event_id=event_id)
            self.catalogs[key] = cat
        return cat

    def get(self, customer_id: int, event_id: str) -> Optional[ClaimCatalog]:
        return self.catalogs.get((customer_id, event_id))

    def iter_for_event(self, event_id: str) -> Iterable[ClaimCatalog]:
        for (_cust, ev), cat in self.catalogs.items():
            if ev == event_id:
                yield cat

    def to_dict(self) -> dict:
        return {f"{cust}|{ev}": cat.to_dict() for (cust, ev), cat in self.catalogs.items()}

    @classmethod
    def from_dict(cls, payload: dict) -> "ClaimCatalogStore":
        store = cls()
        for key, raw in (payload or {}).items():
            cat = ClaimCatalog.from_dict(raw)
            store.catalogs[(cat.customer_id, cat.event_id)] = cat
        return store
