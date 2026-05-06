"""Stance catalog (per customer) + stance assignments.

Catalog mutations (add / rename / merge) are id-stable: assignments
reference entries by `stance_id`, so renames and merges propagate
retroactively across every event/theme without rewriting the assignment
rows' label fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _slugify(label: str) -> str:
    s = re.sub(r"[^\w\s-]", "", label.lower(), flags=re.UNICODE)
    s = re.sub(r"\s+", "_", s).strip("_")
    return s[:64] or "stance"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StanceEntry:
    id: str
    label: str
    description: str
    created_at: str = field(default_factory=_now)
    n_assignments: int = 0
    aliases: list[str] = field(default_factory=list)

    @classmethod
    def new(cls, label: str, description: str, id: Optional[str] = None) -> "StanceEntry":
        return cls(
            id=id or _slugify(label),
            label=label,
            description=description,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "created_at": self.created_at,
            "n_assignments": self.n_assignments,
            "aliases": list(self.aliases),
        }


@dataclass
class StanceAssignment:
    source_item_id: str
    source_kind: str
    customer_id: int
    stance_id: str
    event_id: Optional[str] = None
    theme_id: Optional[str] = None
    assigned_at: str = field(default_factory=_now)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "source_item_id": self.source_item_id,
            "source_kind": self.source_kind,
            "customer_id": self.customer_id,
            "stance_id": self.stance_id,
            "event_id": self.event_id,
            "theme_id": self.theme_id,
            "assigned_at": self.assigned_at,
            "reason": self.reason,
        }


class StanceCatalog:
    """Per-customer stance catalog.

    Entries are addressed by id; labels are display-only and may change
    over time via `rename`. `merge(src, dst)` re-points every assignment
    pointing at `src` to `dst` and removes `src`.
    """

    def __init__(self, customer_id: int):
        self.customer_id = customer_id
        self.entries: dict[str, StanceEntry] = {}
        self.assignments: list[StanceAssignment] = []

    # ── catalog mutations ──────────────────────────────────────────

    def add(self, entry: StanceEntry) -> StanceEntry:
        if entry.id in self.entries:
            return self.entries[entry.id]
        self.entries[entry.id] = entry
        return entry

    def rename(self, stance_id: str, new_label: str, new_description: str) -> StanceEntry:
        entry = self.entries[stance_id]
        if entry.label != new_label:
            entry.aliases.append(entry.label)
        entry.label = new_label
        entry.description = new_description
        return entry

    def merge(self, src_id: str, dst_id: str) -> StanceEntry:
        if src_id == dst_id or src_id not in self.entries:
            return self.entries[dst_id]
        src = self.entries.pop(src_id)
        dst = self.entries[dst_id]
        dst.aliases.append(src.label)
        dst.aliases.extend(src.aliases)
        for a in self.assignments:
            if a.stance_id == src_id:
                a.stance_id = dst_id
        dst.n_assignments += src.n_assignments
        return dst

    # ── assignments ────────────────────────────────────────────────

    def assign(self, assignment: StanceAssignment) -> None:
        if assignment.stance_id not in self.entries:
            raise KeyError(f"unknown stance_id: {assignment.stance_id}")
        self.assignments.append(assignment)
        self.entries[assignment.stance_id].n_assignments += 1

    def reroute_assignments(self, from_stance_id: str, to_stance_id: str) -> int:
        """Used by Phase 5 when adjudicator returns `generalise` — the
        proposed addition is folded into an existing entry without
        creating a new one. Returns count of assignments rerouted."""
        if to_stance_id not in self.entries:
            raise KeyError(f"unknown stance_id: {to_stance_id}")
        n = 0
        for a in self.assignments:
            if a.stance_id == from_stance_id:
                a.stance_id = to_stance_id
                n += 1
        if from_stance_id in self.entries:
            entry = self.entries[from_stance_id]
            self.entries[to_stance_id].n_assignments += entry.n_assignments
            del self.entries[from_stance_id]
        return n

    def drop_assignments_for(self, stance_id: str) -> int:
        """Used by Phase 5 when adjudicator rejects a proposed addition
        — assignments produced under the rejected label are discarded."""
        before = len(self.assignments)
        self.assignments = [a for a in self.assignments if a.stance_id != stance_id]
        if stance_id in self.entries:
            del self.entries[stance_id]
        return before - len(self.assignments)

    # ── reads ──────────────────────────────────────────────────────

    def summary(self) -> list[tuple[str, int]]:
        rows = [(e.label, e.n_assignments) for e in self.entries.values()]
        rows.sort(key=lambda r: r[1], reverse=True)
        return rows

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "entries": [e.to_dict() for e in self.entries.values()],
            "assignments": [a.to_dict() for a in self.assignments],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StanceCatalog":
        cat = cls(int(d["customer_id"]))
        for e in d.get("entries", []):
            cat.entries[e["id"]] = StanceEntry(
                id=e["id"],
                label=e["label"],
                description=e["description"],
                created_at=e.get("created_at", _now()),
                n_assignments=int(e.get("n_assignments", 0)),
                aliases=list(e.get("aliases", [])),
            )
        for a in d.get("assignments", []):
            cat.assignments.append(
                StanceAssignment(
                    source_item_id=a["source_item_id"],
                    source_kind=a["source_kind"],
                    customer_id=int(a["customer_id"]),
                    stance_id=a["stance_id"],
                    event_id=a.get("event_id"),
                    theme_id=a.get("theme_id"),
                    assigned_at=a.get("assigned_at", _now()),
                    reason=a.get("reason", ""),
                )
            )
        return cat
