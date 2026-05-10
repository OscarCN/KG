"""Incremental snapshot printers for the tags pipeline.

Adapted from `tags_legacy/stats.py`. Pure stdout — no I/O.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from src.entities.tags.catalogs import ClaimCatalogStore, StanceCatalog
from src.entities.tags.models import (
    ArticleProcessResult,
    SourceItem,
    StanceType,
)


class StreamingStats:
    """Aggregate counters across a streaming run."""

    def __init__(self):
        self.bundles = 0
        self.events_with_claims = 0
        self.assignments_total = 0
        self.proposals_total = 0
        self.proposals_accepted = 0
        self.claims_extracted = 0
        self.clusters_created = 0
        self.dropped_by_llm = 0

    def absorb(self, result: ArticleProcessResult) -> None:
        self.bundles += 1
        for etr in result.event_tag_results:
            if etr.stance_tagging:
                self.assignments_total += len(etr.stance_tagging.assignments)
                self.proposals_total += len(etr.stance_tagging.proposals)
            if etr.stance_update:
                self.proposals_accepted += etr.stance_update.counters.get(
                    "proposal_add_accepted", 0
                ) + etr.stance_update.counters.get("proposal_rename_accepted", 0)
            if etr.claim_tagging:
                self.claims_extracted += len(etr.claim_tagging.claims)
                if etr.claim_tagging.claims:
                    self.events_with_claims += 1
            if etr.claim_update:
                self.clusters_created += etr.claim_update.counters.get("created", 0)
                self.dropped_by_llm += etr.claim_update.counters.get("dropped_by_llm", 0)


def print_article_snapshot(
    stats: StreamingStats,
    stance_catalog: StanceCatalog,
    claim_catalogs: ClaimCatalogStore,
    *,
    label: str,
    top_n: int = 10,
) -> None:
    print(f"      events: claims={stats.events_with_claims}  "
          f"assignments={stats.assignments_total}  "
          f"proposals={stats.proposals_accepted}/{stats.proposals_total}")
    top = stance_catalog.summary(top_n=top_n)
    if top:
        rendered = "; ".join(f"{lab} ({n})" for lab, n in top)
        print(f"      top stances: {rendered}")
    n_clusters = sum(len(c.clusters) for c in claim_catalogs.catalogs.values())
    print(f"      claim clusters total: {n_clusters}  ({label})")


def print_event_created_snapshot(
    stance_catalog: StanceCatalog,
    claim_catalogs: ClaimCatalogStore,
    customer_id: int,
    event_id: str,
    *,
    top_n: int = 10,
) -> None:
    print(f"      ↳ EVENT {event_id}")
    cat = claim_catalogs.get(customer_id, event_id)
    if cat is None:
        return
    rows = cat.summary()[:top_n]
    for canonical, n, importance_max, is_new in rows:
        tag = "NEW" if is_new else "old"
        print(f"        [{tag} imp={importance_max} n={n}] {canonical}")


def print_top_stances_by_type(
    stance_catalog: StanceCatalog,
    *,
    types: Iterable[StanceType] = (
        "entity_stance",
        "complaint",
        "gratefulness",
        "suggestion",
        "request",
        "denuncia",
        "question",
        "endorsement",
    ),
    top_n: int = 5,
) -> None:
    print("Stance catalog — top per type:")
    for t in types:
        rows = stance_catalog.summary(types=[t], top_n=top_n)
        if not rows:
            continue
        print(f"  {t}:")
        for label, n in rows:
            print(f"    {n:3d}  {label}")


def print_sample_source_items(
    stance_catalog: StanceCatalog,
    claim_catalogs: ClaimCatalogStore,
    items_seen: dict[str, SourceItem],
    *,
    n: int = 3,
) -> None:
    """Pick a few items that received both a stance and a claim, print them."""
    stances_by_item: dict[str, list] = {}
    for a in stance_catalog.assignments:
        stances_by_item.setdefault(a.source_item_id, []).append(a)
    claims_by_item: dict[str, list] = {}
    for cat in claim_catalogs.catalogs.values():
        for a in cat.assignments:
            claims_by_item.setdefault(a.source_item_id, []).append(a)

    candidates = sorted(set(stances_by_item) & set(claims_by_item))
    if not candidates:
        candidates = sorted(set(stances_by_item))[:n]
    print(f"\nSample source items ({len(candidates[:n])} of {len(candidates)} candidates):")
    for sid in candidates[:n]:
        item = items_seen.get(sid)
        text = (item.text if item else "")[:160]
        print(f"  • {sid}  ({item.kind if item else '?'})")
        print(f"    text: {text}")
        for a in stances_by_item.get(sid, []):
            entry = stance_catalog.entries.get(a.stance_id) if a.stance_id else None
            label = entry.label if entry else f"<null:{a.stance_type}>"
            print(f"    stance: [{a.stance_type}] {label}  reason={a.reason[:80]}")
        for a in claims_by_item.get(sid, []):
            print(f"    claim: cluster={a.cluster_id}  verbatim={a.verbatim[:80]}")


def print_top_events(
    stance_catalog: StanceCatalog,
    claim_catalogs: ClaimCatalogStore,
    *,
    n_events: int = 5,
) -> None:
    """Rank events by claim activity; print summaries."""
    rows = []
    for (cust, ev_id), cat in claim_catalogs.catalogs.items():
        n_clusters = len(cat.clusters)
        n_members = sum(len(c.members) for c in cat.clusters.values())
        rows.append((ev_id, n_clusters, n_members))
    rows.sort(key=lambda r: (r[1], r[2]), reverse=True)
    print(f"\nTop events (claim activity):")
    for ev_id, n_clusters, n_members in rows[:n_events]:
        print(f"  • {ev_id}  clusters={n_clusters}  raw_claims={n_members}")
        cat = next(iter(claim_catalogs.iter_for_event(ev_id)), None)
        if cat:
            for canonical, n, importance_max, is_new in cat.summary()[:3]:
                tag = "NEW" if is_new else "old"
                print(f"      [{tag} imp={importance_max} n={n}] {canonical}")
