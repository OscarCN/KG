"""In-memory catalogs for tags_gpt."""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from src.entities.tags_gpt.models import (
    ClaimAssignment,
    ClaimCluster,
    RawClaim,
    STANCE_BEARING_TYPES,
    TAG_ONLY_TYPES,
    StanceAssignment,
    StanceEntry,
    StanceType,
    slugify,
    now_iso,
)


class StanceCatalog:
    def __init__(self, customer_id: int):
        self.customer_id = customer_id
        self.entries: dict[str, StanceEntry] = {}
        self.assignments: list[StanceAssignment] = []

    @classmethod
    def from_dict(cls, data: dict) -> "StanceCatalog":
        catalog = cls(int(data.get("customer_id") or data.get("customer_id", 0)))
        for raw in data.get("entries") or []:
            catalog.entries[raw["id"]] = StanceEntry.from_dict(raw)
        for raw in data.get("assignments") or []:
            catalog.assignments.append(StanceAssignment.from_dict(raw))
        return catalog

    def add_entry(self, entry: StanceEntry) -> StanceEntry:
        if entry.id in self.entries:
            return self.entries[entry.id]
        self.entries[entry.id] = entry
        return entry

    def add(
        self,
        label: str,
        description: str = "",
        *,
        primary_type: StanceType,
        entry_id: str | None = None,
        origin_event_id: str | None = None,
    ) -> StanceEntry:
        stance_id = entry_id or self._unique_id(label)
        return self.add_entry(
            StanceEntry.new(
                label,
                description,
                entry_id=stance_id,
                primary_type=primary_type,
                origin_event_id=origin_event_id,
            )
        )

    def assign(self, assignment: StanceAssignment) -> bool:
        if assignment.customer_id != self.customer_id:
            return False
        if assignment.stance_type in TAG_ONLY_TYPES:
            if assignment.stance_id is not None:
                return False
            self.assignments.append(assignment)
            return True
        if assignment.stance_type not in STANCE_BEARING_TYPES:
            return False
        if assignment.stance_id is None:
            self.assignments.append(assignment)
            return True
        entry = self.entries.get(assignment.stance_id)
        if entry is None or entry.primary_type != assignment.stance_type or entry.retired_at:
            return False
        self.assignments.append(assignment)
        return True

    def rename(self, stance_id: str, new_label: str, new_description: str = "") -> bool:
        entry = self.entries.get(stance_id)
        if not entry or entry.retired_at:
            return False
        if new_label and new_label != entry.label:
            entry.aliases.append(entry.label)
            entry.label = new_label
        if new_description:
            entry.description = new_description
        return True

    def merge(self, src_id: str, dst_id: str) -> bool:
        if src_id == dst_id:
            return True
        src = self.entries.get(src_id)
        dst = self.entries.get(dst_id)
        if not src or not dst or src.primary_type != dst.primary_type:
            return False
        dst.aliases.append(src.label)
        dst.aliases.extend(src.aliases)
        for assignment in self.assignments:
            if assignment.stance_id == src_id:
                assignment.stance_id = dst_id
        del self.entries[src_id]
        return True

    def retire(self, stance_id: str) -> bool:
        entry = self.entries.get(stance_id)
        if not entry:
            return False
        entry.retired_at = now_iso()
        return True

    def reroute(self, from_id: str, to_id: str) -> int:
        src = self.entries.get(from_id)
        dst = self.entries.get(to_id)
        if not src or not dst or src.primary_type != dst.primary_type:
            return 0
        count = 0
        for assignment in self.assignments:
            if assignment.stance_id == from_id:
                assignment.stance_id = to_id
                count += 1
        return count

    def iter_entries(self, types: set[StanceType] | None = None) -> Iterable[StanceEntry]:
        for entry in self.entries.values():
            if entry.retired_at:
                continue
            if types and entry.primary_type not in types:
                continue
            yield entry

    def summary(self, types: set[StanceType] | None = None) -> list[tuple[str, int]]:
        counts: Counter[str] = Counter()
        for assignment in self.assignments:
            if types and assignment.stance_type not in types:
                continue
            entry = self.entries.get(assignment.stance_id or "")
            label = entry.label if entry else f"<uncatalogued:{assignment.stance_type}>"
            counts[label] += 1
        return counts.most_common()

    def snapshot(self, types: set[StanceType] | None = None) -> list[dict]:
        return [entry.to_dict() for entry in self.iter_entries(types)]

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "entries": [entry.to_dict() for entry in self.entries.values()],
            "assignments": [assignment.to_dict() for assignment in self.assignments],
        }

    def _unique_id(self, label: str) -> str:
        base = slugify(label, fallback="stance")
        candidate = base
        suffix = 2
        while candidate in self.entries:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate


class ClaimCatalog:
    def __init__(self, customer_id: int, event_id: str):
        self.customer_id = customer_id
        self.event_id = event_id
        self.clusters: dict[str, ClaimCluster] = {}
        self.assignments: list[ClaimAssignment] = []

    @classmethod
    def from_dict(cls, data: dict) -> "ClaimCatalog":
        catalog = cls(int(data["customer_id"]), str(data["event_id"]))
        for raw in data.get("clusters") or []:
            cluster = ClaimCluster.from_dict(raw)
            catalog.clusters[cluster.id] = cluster
        for raw in data.get("assignments") or []:
            catalog.assignments.append(ClaimAssignment.from_dict(raw))
        return catalog

    def create(self, claim: RawClaim, canonical: str) -> ClaimCluster:
        cluster_id = self._unique_id(canonical)
        cluster = ClaimCluster(
            id=cluster_id,
            customer_id=self.customer_id,
            event_id=self.event_id,
            canonical=canonical,
            members=[claim],
        )
        cluster.recompute_importance()
        self.clusters[cluster_id] = cluster
        self.assignments.append(self._assignment_for(claim, cluster_id))
        return cluster

    def assign(self, claim: RawClaim, cluster_id: str) -> bool:
        cluster = self.clusters.get(cluster_id)
        if not cluster:
            return False
        cluster.members.append(claim)
        cluster.recompute_importance()
        self.assignments.append(self._assignment_for(claim, cluster_id))
        return True

    def rename(self, cluster_id: str, new_canonical: str) -> bool:
        cluster = self.clusters.get(cluster_id)
        if not cluster:
            return False
        if new_canonical and new_canonical != cluster.canonical:
            cluster.aliases.append(cluster.canonical)
            cluster.canonical = new_canonical
        return True

    def merge(self, src_id: str, dst_id: str) -> bool:
        if src_id == dst_id:
            return True
        src = self.clusters.get(src_id)
        dst = self.clusters.get(dst_id)
        if not src or not dst:
            return False
        dst.aliases.append(src.canonical)
        dst.aliases.extend(src.aliases)
        dst.members.extend(src.members)
        dst.recompute_importance()
        for assignment in self.assignments:
            if assignment.cluster_id == src_id:
                assignment.cluster_id = dst_id
        del self.clusters[src_id]
        return True

    def summary(self) -> list[dict]:
        return [
            {"id": cluster.id, "canonical": cluster.canonical, "n_members": len(cluster.members)}
            for cluster in self.clusters.values()
        ]

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "event_id": self.event_id,
            "clusters": [cluster.to_dict() for cluster in self.clusters.values()],
            "assignments": [assignment.to_dict() for assignment in self.assignments],
        }

    def _assignment_for(self, claim: RawClaim, cluster_id: str) -> ClaimAssignment:
        return ClaimAssignment(
            source_item_id=claim.source_item_id,
            source_kind=claim.source_kind,
            cluster_id=cluster_id,
            event_id=self.event_id,
            customer_id=self.customer_id,
            verbatim=claim.verbatim,
        )

    def _unique_id(self, canonical: str) -> str:
        base = slugify(canonical, fallback="claim")
        candidate = base
        suffix = 2
        while candidate in self.clusters:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate


class ClaimCatalogStore:
    def __init__(self):
        self.catalogs: dict[tuple[int, str], ClaimCatalog] = {}

    def get(self, customer_id: int, event_id: str) -> ClaimCatalog:
        key = (int(customer_id), str(event_id))
        if key not in self.catalogs:
            self.catalogs[key] = ClaimCatalog(*key)
        return self.catalogs[key]

    @classmethod
    def from_dict(cls, data: dict) -> "ClaimCatalogStore":
        store = cls()
        for key, raw in (data or {}).items():
            if "customer_id" not in raw or "event_id" not in raw:
                customer_id, event_id = key.split("|", 1)
                raw = {**raw, "customer_id": int(customer_id), "event_id": event_id}
            catalog = ClaimCatalog.from_dict(raw)
            store.catalogs[(catalog.customer_id, catalog.event_id)] = catalog
        return store

    def to_dict(self) -> dict:
        return {
            f"{customer_id}|{event_id}": catalog.to_dict()
            for (customer_id, event_id), catalog in self.catalogs.items()
        }
