"""Incremental streaming stats for the tagging pipeline.

Shapes the stdout printout the streaming runner emits per article and
whenever a new event is created.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from src.entities.tags.models.claim_catalog import ClaimCatalogRegistry
from src.entities.tags.models.stance_catalog import StanceCatalog


@dataclass
class StreamingStats:
    n_articles: int = 0
    n_events_created: int = 0
    n_events_merged: int = 0
    n_events_skipped: int = 0
    n_events_dropped: int = 0
    n_stance_assignments: int = 0
    n_stance_proposals_accepted: int = 0
    n_stance_proposals_rejected: int = 0
    n_stance_proposals_renamed: int = 0
    n_stance_proposals_generalised: int = 0
    n_claims_created: int = 0
    n_claims_assigned: int = 0
    n_claims_dropped_phase2: int = 0
    n_claims_dropped_phase4: int = 0
    n_claims_renames: int = 0
    n_claims_merges: int = 0
    new_clusters_since_last_snapshot: int = 0

    def on_article(self) -> None:
        self.n_articles += 1

    def on_link_result(self, status: str) -> None:
        if status == "created":
            self.n_events_created += 1
        elif status == "merged":
            self.n_events_merged += 1
        elif status == "skipped":
            self.n_events_skipped += 1
        elif status == "dropped":
            self.n_events_dropped += 1

    def absorb_stance_apply(self, summary: dict) -> None:
        self.n_stance_assignments += summary.get("n_assignments_applied", 0)
        self.n_stance_proposals_accepted += summary.get("n_accept", 0)
        self.n_stance_proposals_rejected += summary.get("n_reject", 0)
        self.n_stance_proposals_renamed += summary.get("n_rename", 0)
        self.n_stance_proposals_generalised += summary.get("n_generalise", 0)

    def absorb_claim_apply(self, summary: dict, dropped_phase2: int = 0) -> None:
        self.n_claims_created += summary.get("n_create", 0)
        self.n_claims_assigned += summary.get("n_assign", 0)
        self.n_claims_dropped_phase4 += summary.get("n_drop", 0)
        self.n_claims_renames += summary.get("n_renames", 0)
        self.n_claims_merges += summary.get("n_merges", 0)
        self.n_claims_dropped_phase2 += dropped_phase2
        self.new_clusters_since_last_snapshot += summary.get("n_create", 0)


def format_top_stances(catalog: StanceCatalog, top_n: int = 10) -> str:
    rows = catalog.summary()[:top_n]
    if not rows:
        return "(catálogo vacío)"
    parts = [f"{label} ({n})" for label, n in rows]
    return "; ".join(parts)


def format_event_clusters(
    registry: ClaimCatalogRegistry, event_id: str, top_n: int = 5
) -> str:
    for (cust, ev), cat in registry.items():
        if ev != event_id:
            continue
        rows = cat.summary()[:top_n]
        if not rows:
            return "(sin claim clusters)"
        parts = []
        for canonical, n_members, importance_max, is_new in rows:
            tag = "NEW" if is_new else "old"
            parts.append(f"[{tag} imp={importance_max} n={n_members}] {canonical}")
        return "\n      ".join(parts)
    return "(evento sin catálogo)"


def print_article_snapshot(
    stats: StreamingStats,
    catalog: StanceCatalog,
    registry: ClaimCatalogRegistry,
    article_idx: int,
    article_total: int,
    source_id: str,
    article_event_ids: list[str],
    *,
    top_n: int = 10,
) -> None:
    a_created = sum(1 for e in article_event_ids if e and e.startswith("created:"))
    a_merged = sum(1 for e in article_event_ids if e and e.startswith("merged:"))
    new_clusters = stats.new_clusters_since_last_snapshot
    print(
        f"[{article_idx}/{article_total}] {source_id}\n"
        f"      events: created={a_created} merged={a_merged}\n"
        f"      top stances: {format_top_stances(catalog, top_n)}\n"
        f"      new claim clusters this article: {new_clusters}"
    )
    stats.new_clusters_since_last_snapshot = 0


def print_event_created_snapshot(
    catalog: StanceCatalog,
    registry: ClaimCatalogRegistry,
    event_id: str,
    *,
    top_n: int = 10,
    cluster_top_n: int = 5,
) -> None:
    print(
        f"      ↳ EVENT CREATED {event_id}\n"
        f"        top stances: {format_top_stances(catalog, top_n)}\n"
        f"        clusters: {format_event_clusters(registry, event_id, cluster_top_n)}"
    )
