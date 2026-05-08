"""In-memory stores for events, stances, and claims.

These stores are deliberately small and boring: they own mutations and
summary queries, while LLM-driven steps only produce decisions.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Optional

from src.entities.tags_gpt.models import (
    ClaimAssignment,
    ClaimCluster,
    LinkedEvent,
    RawClaim,
    STANCE_BEARING_TYPES,
    TAG_ONLY_TYPES,
    StanceAssignment,
    StanceEntry,
    StanceType,
    slugify,
)


class EventStore:
    def __init__(self):
        self.events: dict[str, LinkedEvent] = {}

    def add(self, event: LinkedEvent) -> LinkedEvent:
        self.events[event.id] = event
        return event

    def get(self, event_id: str) -> Optional[LinkedEvent]:
        return self.events.get(event_id)

    def values(self) -> list[LinkedEvent]:
        return list(self.events.values())

    def to_records(self) -> list[dict]:
        return [event.to_record() for event in self.events.values()]


class StanceCatalog:
    def __init__(self, customer_id: int):
        self.customer_id = customer_id
        self.entries: dict[str, StanceEntry] = {}
        self.retired_entries: dict[str, StanceEntry] = {}
        self.assignments: list[StanceAssignment] = []

    @classmethod
    def from_dict(cls, data: dict) -> "StanceCatalog":
        catalog = cls(int(data["customer_id"]))
        for raw in data.get("entries") or []:
            entry = StanceEntry.from_dict(raw)
            catalog.entries[entry.id] = entry
        for raw in data.get("retired_entries") or []:
            entry = StanceEntry.from_dict(raw)
            catalog.retired_entries[entry.id] = entry
        for raw in data.get("assignments") or []:
            catalog.assignments.append(StanceAssignment.from_dict(raw))
        return catalog

    def add(
        self,
        label: str,
        description: str = "",
        entry_id: Optional[str] = None,
        *,
        primary_type: StanceType = "entity_stance",
    ) -> StanceEntry:
        stance_id = entry_id or self._unique_id(label)
        if stance_id in self.entries:
            return self.entries[stance_id]
        entry = StanceEntry.new(label, description, entry_id=stance_id, primary_type=primary_type)
        self.entries[entry.id] = entry
        return entry

    def add_entry(self, entry: StanceEntry) -> StanceEntry:
        if entry.id in self.entries:
            return self.entries[entry.id]
        self.entries[entry.id] = entry
        return entry

    def assign(self, assignment: StanceAssignment) -> bool:
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
        if entry is None or entry.primary_type != assignment.stance_type:
            return False
        self.assignments.append(assignment)
        return True

    def rename(self, stance_id: str, label: str, description: str = "") -> bool:
        entry = self.entries.get(stance_id)
        if not entry:
            return False
        if entry.label != label:
            entry.aliases.append(entry.label)
        entry.label = label
        entry.description = description
        return True

    def merge(self, src_id: str, dst_id: str) -> bool:
        if src_id == dst_id:
            return True
        src = self.entries.get(src_id)
        dst = self.entries.get(dst_id)
        if not src or not dst:
            return False
        if src.primary_type != dst.primary_type:
            return False
        dst.aliases.append(src.label)
        dst.aliases.extend(src.aliases)
        for assignment in self.assignments:
            if assignment.stance_id == src_id:
                assignment.stance_id = dst_id
        del self.entries[src_id]
        return True

    def delete(self, stance_id: str, *, delete_assignments: bool = True) -> int:
        self.entries.pop(stance_id, None)
        if not delete_assignments:
            return 0
        before = len(self.assignments)
        self.assignments = [x for x in self.assignments if x.stance_id != stance_id]
        return before - len(self.assignments)

    def retire(self, stance_id: str) -> bool:
        entry = self.entries.pop(stance_id, None)
        if not entry:
            return False
        self.retired_entries[stance_id] = entry
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

    def summary(
        self,
        *,
        event_id: Optional[str] = None,
        top_n: Optional[int] = None,
        types: Optional[set[StanceType]] = None,
    ) -> list[tuple[str, int]]:
        counts: Counter[str] = Counter()
        for assignment in self.assignments:
            if event_id and assignment.event_id != event_id:
                continue
            if types and assignment.stance_type not in types:
                continue
            entry = self.entries.get(assignment.stance_id or "")
            retired = self.retired_entries.get(assignment.stance_id or "")
            if entry:
                label = entry.label
            elif retired:
                label = f"{retired.label} [retired]"
            else:
                label = f"<unmapped:{assignment.stance_type}>"
            counts[label] += 1
        rows = counts.most_common(top_n)
        if not rows and not event_id:
            rows = [(entry.label, 0) for entry in self.iter_entries(types=types)]
        return rows

    def iter_entries(self, *, types: Optional[set[StanceType]] = None) -> Iterable[StanceEntry]:
        for entry in self.entries.values():
            if types and entry.primary_type not in types:
                continue
            yield entry

    def snapshot(self, *, types: Optional[set[StanceType]] = None) -> list[dict]:
        return [
            {
                "id": entry.id,
                "label": entry.label,
                "description": entry.description,
                "primary_type": entry.primary_type,
            }
            for entry in self.iter_entries(types=types)
        ]

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "entries": [x.to_dict() for x in self.entries.values()],
            "retired_entries": [x.to_dict() for x in self.retired_entries.values()],
            "assignments": [x.to_dict() for x in self.assignments],
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

    def create(self, claim: RawClaim, canonical: str) -> ClaimCluster:
        cluster_id = self._unique_id(canonical)
        cluster = ClaimCluster(
            id=cluster_id,
            customer_id=self.customer_id,
            event_id=self.event_id,
            canonical=canonical,
            members=[claim],
        )
        self.clusters[cluster_id] = cluster
        self.assignments.append(self._assignment_for(claim, cluster_id))
        return cluster

    def assign(self, claim: RawClaim, cluster_id: str) -> bool:
        cluster = self.clusters.get(cluster_id)
        if not cluster:
            return False
        cluster.members.append(claim)
        self.assignments.append(self._assignment_for(claim, cluster_id))
        return True

    def rename(self, cluster_id: str, canonical: str) -> bool:
        cluster = self.clusters.get(cluster_id)
        if not cluster:
            return False
        if cluster.canonical != canonical:
            cluster.aliases.append(cluster.canonical)
        cluster.canonical = canonical
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
        for assignment in self.assignments:
            if assignment.cluster_id == src_id:
                assignment.cluster_id = dst_id
        del self.clusters[src_id]
        return True

    def summary(self, top_n: Optional[int] = None) -> list[tuple[str, int, int, bool]]:
        rows = [
            (cluster.canonical, len(cluster.members), cluster.importance_max, cluster.is_new)
            for cluster in self.clusters.values()
        ]
        rows.sort(key=lambda row: (row[3], row[2], row[1]), reverse=True)
        return rows[:top_n] if top_n else rows

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "event_id": self.event_id,
            "clusters": [x.to_dict() for x in self.clusters.values()],
            "assignments": [x.to_dict() for x in self.assignments],
        }

    def _assignment_for(self, claim: RawClaim, cluster_id: str) -> ClaimAssignment:
        return ClaimAssignment(
            source_item_id=claim.source_item_id,
            source_kind=claim.source_kind,
            customer_id=self.customer_id,
            event_id=self.event_id,
            cluster_id=cluster_id,
            verbatim=claim.verbatim,
        )

    def _unique_id(self, canonical: str) -> str:
        base = slugify(canonical, fallback="claim", max_len=48)
        candidate = base
        suffix = 2
        while candidate in self.clusters:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate


class ClaimCatalogStore:
    def __init__(self):
        self.catalogs: dict[tuple[int, str], ClaimCatalog] = {}

    def get_or_create(self, customer_id: int, event_id: str) -> ClaimCatalog:
        key = (customer_id, event_id)
        if key not in self.catalogs:
            self.catalogs[key] = ClaimCatalog(customer_id, event_id)
        return self.catalogs[key]

    def values(self) -> Iterable[ClaimCatalog]:
        return self.catalogs.values()

    def items(self):
        return self.catalogs.items()

    def to_dict(self) -> dict:
        return {
            f"{customer_id}|{event_id}": catalog.to_dict()
            for (customer_id, event_id), catalog in self.catalogs.items()
        }
