"""Incremental streaming stats for the tagging pipeline.

Shapes the stdout printout the streaming runner emits per article and
whenever a new event is created.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from src.entities.tags.models.claim_catalog import ClaimCatalogRegistry
from src.entities.tags.models.source_item import SourceItem
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
    article_event_ids: list[str],
    *,
    top_n: int = 10,
) -> None:
    """Print the per-article metrics block. Caller is responsible for
    printing any header line (e.g. `[i/total] source_id`) above this."""
    a_created = sum(1 for e in article_event_ids if e and e.startswith("created:"))
    a_merged = sum(1 for e in article_event_ids if e and e.startswith("merged:"))
    new_clusters = stats.new_clusters_since_last_snapshot
    print(
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


# ── Final-summary printers ──────────────────────────────────────────


def _truncate(text: str, n: int = 200) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def _stances_by_item(catalog: StanceCatalog) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for a in catalog.assignments:
        entry = catalog.entries.get(a.stance_id)
        out[a.source_item_id].append(
            {
                "label": entry.label if entry else f"<missing:{a.stance_id}>",
                "event_id": a.event_id,
                "reason": a.reason,
            }
        )
    return out


def _claims_by_item(registry: ClaimCatalogRegistry) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for _, cat in registry.items():
        canonical_by_id = {cid: c.canonical for cid, c in cat.clusters.items()}
        importance_by_id_and_source: dict[tuple[str, str], int] = {}
        for c in cat.clusters.values():
            for m in c.members:
                importance_by_id_and_source[(c.id, m.source_id)] = m.importance
        for a in cat.assignments:
            out[a.source_item_id].append(
                {
                    "cluster_canonical": canonical_by_id.get(a.cluster_id, ""),
                    "cluster_id": a.cluster_id,
                    "event_id": a.event_id,
                    "verbatim": a.verbatim,
                    "importance": importance_by_id_and_source.get(
                        (a.cluster_id, a.source_item_id), 0
                    ),
                }
            )
    return out


def print_sample_source_items(
    catalog: StanceCatalog,
    registry: ClaimCatalogRegistry,
    items_seen: dict[str, SourceItem],
    *,
    n: int = 3,
) -> None:
    """Pick `n` source items that carry both a stance assignment AND at
    least one claim assignment, and print them with their tags.
    Falls back to items with stances or claims (in that order) when
    fewer than `n` carry both.
    """
    stance_map = _stances_by_item(catalog)
    claim_map = _claims_by_item(registry)

    both = [sid for sid in stance_map if sid in claim_map and sid in items_seen]
    only_stance = [
        sid for sid in stance_map if sid not in claim_map and sid in items_seen
    ]
    only_claim = [
        sid for sid in claim_map if sid not in stance_map and sid in items_seen
    ]
    picked: list[str] = (both + only_stance + only_claim)[:n]

    print()
    print("=" * 72)
    print(f"Sample source items ({len(picked)} of {len(stance_map | claim_map.keys())} tagged):")
    print("=" * 72)
    if not picked:
        print("  (no tagged source items captured this run)")
        return

    for sid in picked:
        item = items_seen[sid]
        print()
        print(f"[{item.kind}] {sid}")
        if item.author:
            print(f"  author: {item.author}")
        print(f"  text: {_truncate(item.text)}")
        for s in stance_map.get(sid, []):
            print(f"  stance: {s['label']!r}")
            if s.get("reason"):
                print(f"    reason: {_truncate(s['reason'], 160)}")
            if s.get("event_id"):
                print(f"    in event: {s['event_id']}")
        claims = claim_map.get(sid, [])
        if claims:
            print(f"  claims ({len(claims)}):")
            for c in claims:
                print(
                    f"    - cluster: {c['cluster_canonical']!r} "
                    f"(event {c['event_id']}, importance {c['importance']})"
                )
                print(f"      verbatim: {_truncate(c['verbatim'], 160)}")


def _event_score(
    event: dict,
    registry: ClaimCatalogRegistry,
    customer_id: int,
) -> tuple[int, int, int]:
    """Sort key: (n_clusters, n_claim_members, n_source_ids) — events with
    rich tag activity float to the top."""
    eid = event.get("id")
    cat = None
    for (cust, ev), c in registry.items():
        if cust == customer_id and ev == eid:
            cat = c
            break
    n_clusters = len(cat.clusters) if cat else 0
    n_members = sum(len(c.members) for c in cat.clusters.values()) if cat else 0
    n_sources = len(event.get("source_ids") or [])
    return (n_clusters, n_members, n_sources)


def print_top_events(
    events: list[dict],
    catalog: StanceCatalog,
    registry: ClaimCatalogRegistry,
    items_seen: dict[str, SourceItem],
    customer_id: int,
    *,
    n_events: int = 5,
    items_per_event: int = 3,
    cluster_top_n: int = 4,
    stance_top_n: int = 5,
) -> None:
    """Print the top-`n_events` linked events ranked by tagging activity,
    with a stance breakdown, a claim-cluster summary, and a sample of
    their source items (each annotated with stances + claims)."""
    if not events:
        print()
        print("(no events linked)")
        return

    ranked = sorted(events, key=lambda e: _event_score(e, registry, customer_id), reverse=True)
    picked = ranked[:n_events]

    stance_map = _stances_by_item(catalog)
    claim_map = _claims_by_item(registry)

    print()
    print("=" * 72)
    print(f"Top events ({len(picked)} of {len(events)}):")
    print("=" * 72)

    for ev in picked:
        eid = ev.get("id")
        source_ids = ev.get("source_ids") or []
        print()
        print(f"[{eid}] {ev.get('event_type')} — {(ev.get('name') or '').strip()!r}")
        if ev.get("description"):
            print(f"  description: {_truncate(ev['description'], 220)}")
        print(f"  source_ids: {len(source_ids)}")

        # ── Per-event stance aggregate (only assignments tied to this event)
        stance_counts: Counter[str] = Counter()
        for a in catalog.assignments:
            if a.event_id == eid:
                entry = catalog.entries.get(a.stance_id)
                stance_counts[entry.label if entry else f"<{a.stance_id}>"] += 1
        if stance_counts:
            print("  stance aggregate (this event):")
            for label, c in stance_counts.most_common(stance_top_n):
                print(f"    [{c}] {label}")
        else:
            print("  stance aggregate (this event): (none)")

        # ── Per-event claim cluster summary
        cat = None
        for (cust, evid), c in registry.items():
            if cust == customer_id and evid == eid:
                cat = c
                break
        if cat and cat.clusters:
            rows = cat.summary()[:cluster_top_n]
            print(f"  claim clusters ({len(cat.clusters)} total):")
            for canonical, n_members, importance_max, is_new in rows:
                tag = "NEW" if is_new else "old"
                print(
                    f"    [{tag} imp={importance_max} n={n_members}] {_truncate(canonical, 180)}"
                )
        else:
            print("  claim clusters: (none)")

        # ── Sample source items tied to this event with their tags
        sample_sids = [sid for sid in source_ids if sid in items_seen][:items_per_event]
        # also include any comment whose stance/claim points at this event
        comment_sids = [
            sid
            for sid in (set(stance_map.keys()) | set(claim_map.keys()))
            if sid in items_seen
            and any(s.get("event_id") == eid for s in stance_map.get(sid, []))
            or any(c.get("event_id") == eid for c in claim_map.get(sid, []))
        ]
        for sid in comment_sids:
            if sid not in sample_sids and len(sample_sids) < items_per_event * 2:
                sample_sids.append(sid)

        if sample_sids:
            print("  sample source items:")
            for sid in sample_sids:
                item = items_seen[sid]
                stances_here = [
                    s["label"]
                    for s in stance_map.get(sid, [])
                    if s.get("event_id") in (eid, None)
                ]
                claims_here = [
                    c
                    for c in claim_map.get(sid, [])
                    if c.get("event_id") == eid
                ]
                print(f"    [{item.kind}] {sid}")
                print(f"      text: {_truncate(item.text, 180)}")
                if stances_here:
                    print(f"      stances: {stances_here}")
                if claims_here:
                    print(
                        "      claims: "
                        + "; ".join(
                            f"{_truncate(c['verbatim'], 80)} → {_truncate(c['cluster_canonical'], 60)} (imp={c['importance']})"
                            for c in claims_here
                        )
                    )
                if not stances_here and not claims_here:
                    print("      stances: -   claims: -")
        else:
            print("  sample source items: (none captured)")
