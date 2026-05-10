"""Claim catalog (per (customer, event)) + raw claims, clusters,
assignments, and a registry holding one catalog per event.

Each catalog is composed of `ClaimCluster` entries — a cluster carries
a canonical phrasing plus the raw claims folded into it. Renames and
merges are id-stable so retroactive propagation is automatic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Optional


def _slugify(text: str, *, max_len: int = 48) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower(), flags=re.UNICODE)
    s = re.sub(r"\s+", "_", s).strip("_")
    return s[:max_len] or "cluster"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


@dataclass
class RawClaim:
    event_id: str
    customer_id: int
    affected_entity_ids: list[int]
    verbatim: str
    source_id: str
    source_kind: str
    importance: int = 1
    importance_reason: str = ""
    extracted_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "customer_id": self.customer_id,
            "affected_entity_ids": list(self.affected_entity_ids),
            "verbatim": self.verbatim,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "importance": self.importance,
            "importance_reason": self.importance_reason,
            "extracted_at": self.extracted_at,
        }


@dataclass
class ClaimAssignment:
    source_item_id: str
    source_kind: str
    cluster_id: str
    event_id: str
    customer_id: int
    verbatim: str
    assigned_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "source_item_id": self.source_item_id,
            "source_kind": self.source_kind,
            "cluster_id": self.cluster_id,
            "event_id": self.event_id,
            "customer_id": self.customer_id,
            "verbatim": self.verbatim,
            "assigned_at": self.assigned_at,
        }


@dataclass
class ClaimCluster:
    """One claim entry inside an event's claim catalog. Aggregates many
    raw claims that express the same allegation."""

    id: str
    event_id: str
    customer_id: int
    canonical: str
    members: list[RawClaim] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    is_new: bool = True
    freshness_window_hours: int = 24
    aliases: list[str] = field(default_factory=list)

    @classmethod
    def new(
        cls,
        event_id: str,
        customer_id: int,
        canonical: str,
        freshness_window_hours: int = 24,
    ) -> "ClaimCluster":
        return cls(
            id=_slugify(canonical),
            event_id=event_id,
            customer_id=customer_id,
            canonical=canonical,
            freshness_window_hours=freshness_window_hours,
        )

    @property
    def importance_max(self) -> int:
        return max((m.importance for m in self.members), default=0)

    @property
    def importance_typical(self) -> int:
        if not self.members:
            return 0
        return int(median(m.importance for m in self.members))

    @property
    def importance_n_high(self) -> int:
        return sum(1 for m in self.members if m.importance >= 3)

    def add_member(self, claim: RawClaim) -> None:
        self.members.append(claim)

    def rename(self, new_canonical: str) -> None:
        if self.canonical == new_canonical:
            return
        self.aliases.append(self.canonical)
        self.canonical = new_canonical

    def freshness_expired(self, now: Optional[datetime] = None) -> bool:
        now = now or _now()
        try:
            created = datetime.fromisoformat(self.created_at)
        except ValueError:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (now - created) > timedelta(hours=self.freshness_window_hours)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "customer_id": self.customer_id,
            "canonical": self.canonical,
            "members": [m.to_dict() for m in self.members],
            "created_at": self.created_at,
            "is_new": self.is_new,
            "freshness_window_hours": self.freshness_window_hours,
            "aliases": list(self.aliases),
            "importance_max": self.importance_max,
            "importance_typical": self.importance_typical,
            "importance_n_high": self.importance_n_high,
        }


class ClaimCatalog:
    """Open-ended catalog scoped to one `(customer, event)` pair."""

    def __init__(self, customer_id: int, event_id: str):
        self.customer_id = customer_id
        self.event_id = event_id
        self.clusters: dict[str, ClaimCluster] = {}
        self.assignments: list[ClaimAssignment] = []

    # ── mutations ──────────────────────────────────────────────────

    def create_new(
        self,
        claim: RawClaim,
        canonical: str,
        freshness_window_hours: int = 24,
    ) -> ClaimCluster:
        cluster_id = _slugify(canonical)
        suffix = 1
        while cluster_id in self.clusters:
            suffix += 1
            cluster_id = f"{_slugify(canonical)}_{suffix}"
        cluster = ClaimCluster(
            id=cluster_id,
            event_id=self.event_id,
            customer_id=self.customer_id,
            canonical=canonical,
            freshness_window_hours=freshness_window_hours,
        )
        cluster.add_member(claim)
        self.clusters[cluster_id] = cluster
        self.assignments.append(self._make_assignment(claim, cluster_id))
        return cluster

    def assign_to_existing(self, claim: RawClaim, cluster_id: str) -> ClaimCluster:
        cluster = self.clusters[cluster_id]
        cluster.add_member(claim)
        self.assignments.append(self._make_assignment(claim, cluster_id))
        return cluster

    def rename(self, cluster_id: str, new_canonical: str) -> ClaimCluster:
        cluster = self.clusters[cluster_id]
        cluster.rename(new_canonical)
        return cluster

    def merge(self, src_id: str, dst_id: str) -> ClaimCluster:
        if src_id == dst_id or src_id not in self.clusters:
            return self.clusters[dst_id]
        src = self.clusters.pop(src_id)
        dst = self.clusters[dst_id]
        dst.aliases.append(src.canonical)
        dst.aliases.extend(src.aliases)
        dst.members.extend(src.members)
        for a in self.assignments:
            if a.cluster_id == src_id:
                a.cluster_id = dst_id
        return dst

    def expire_freshness(self, now: Optional[datetime] = None) -> int:
        n = 0
        for cluster in self.clusters.values():
            if cluster.is_new and cluster.freshness_expired(now):
                cluster.is_new = False
                n += 1
        return n

    # ── reads ──────────────────────────────────────────────────────

    def summary(self) -> list[tuple[str, int, int, bool]]:
        rows = [
            (c.canonical, len(c.members), c.importance_max, c.is_new)
            for c in self.clusters.values()
        ]
        rows.sort(key=lambda r: (r[3], r[2], r[1]), reverse=True)
        return rows

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "event_id": self.event_id,
            "clusters": [c.to_dict() for c in self.clusters.values()],
            "assignments": [a.to_dict() for a in self.assignments],
        }

    # ── helpers ────────────────────────────────────────────────────

    def _make_assignment(self, claim: RawClaim, cluster_id: str) -> ClaimAssignment:
        return ClaimAssignment(
            source_item_id=claim.source_id,
            source_kind=claim.source_kind,
            cluster_id=cluster_id,
            event_id=self.event_id,
            customer_id=self.customer_id,
            verbatim=claim.verbatim,
        )


class ClaimCatalogRegistry:
    """One `ClaimCatalog` per `(customer_id, event_id)`."""

    def __init__(self):
        self._catalogs: dict[tuple[int, str], ClaimCatalog] = {}

    def get_or_create(self, customer_id: int, event_id: str) -> ClaimCatalog:
        key = (customer_id, event_id)
        if key not in self._catalogs:
            self._catalogs[key] = ClaimCatalog(customer_id, event_id)
        return self._catalogs[key]

    def __iter__(self):
        return iter(self._catalogs.values())

    def items(self):
        return self._catalogs.items()

    def to_dict(self) -> dict:
        return {
            f"{cust}|{ev}": cat.to_dict()
            for (cust, ev), cat in self._catalogs.items()
        }
