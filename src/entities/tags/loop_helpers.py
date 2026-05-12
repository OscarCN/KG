"""Helpers for `run_tags.py` — keeps the IPython driver script lean.

Pre-loop accounting, per-bundle diff prints, mid-stream consistency
trigger. Distinct from `stats.py` (which holds cumulative-summary
printers used by the post-streaming summary block too).
"""

from __future__ import annotations

from src.entities.tags.catalogs import StanceCatalog
from src.entities.tags.consistency import ConsistencyPassStep
from src.entities.tags.models import (
    ArticleBundle,
    ArticleProcessResult,
    ConsistencyPassResult,
)
from src.entities.tags.stats import (
    print_catalog_overview,
    print_event_created_snapshot,
)
from src.entities.tags.streaming import StreamingState


# Same canonical type order used by `stats.print_catalog_overview`.
_TYPE_ORDER: tuple[str, ...] = (
    "entity_stance",
    "complaint",
    "denuncia",
    "suggestion",
    "request",
    "gratefulness",
    "endorsement",
    "question",
)


def _truncate(text: str, limit: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _quantile(sorted_vals: list[int], p: int) -> int:
    """0..100 percentile on a pre-sorted ascending list. P0=min, P100=max."""
    if not sorted_vals:
        return 0
    if p >= 100:
        return sorted_vals[-1]
    if p <= 0:
        return sorted_vals[0]
    k = int(round((p / 100.0) * (len(sorted_vals) - 1)))
    return sorted_vals[k]


def _distribution_line(counts: list[int], *, singleton_label: str = "zero") -> str:
    """Compact quantile summary: max, P80, P60, P40, P20, and a count of
    `zero` (or `singleton`) entries.

    Convention: percentiles run bottom→top, so `max` is the highest value,
    `P80` is the value such that 80% of entries are ≤ it, etc.
    """
    if not counts:
        return "(no entries)"
    s = sorted(counts)
    threshold = 0 if singleton_label == "zero" else 1
    n_small = sum(1 for c in counts if c <= threshold)
    return (
        f"max={_quantile(s, 100)}  P80={_quantile(s, 80)}  "
        f"P60={_quantile(s, 60)}  P40={_quantile(s, 40)}  P20={_quantile(s, 20)}  "
        f"{singleton_label}={n_small}/{len(counts)}"
    )


# ── Catalog / assignment shape helpers ──────────────────────────────────


def per_entry_counts(catalog: StanceCatalog) -> dict[str, int]:
    """{stance_id: n_assignments} — catalogued rows only."""
    out: dict[str, int] = {}
    for a in catalog.assignments:
        if a.stance_id:
            out[a.stance_id] = out.get(a.stance_id, 0) + 1
    return out


def tally_assignments(catalog: StanceCatalog) -> tuple[dict[str, int], int, int]:
    """Return ({stance_type: n_assignments}, n_catalogued, n_uncatalogued)."""
    by_type: dict[str, int] = {}
    n_cat = n_null = 0
    for a in catalog.assignments:
        by_type[a.stance_type] = by_type.get(a.stance_type, 0) + 1
        if a.stance_id is None:
            n_null += 1
        else:
            n_cat += 1
    return by_type, n_cat, n_null


# ── Pre-loop printing ──────────────────────────────────────────────────


def print_corpus_accounting(
    bundles: list[ArticleBundle],
    *,
    bootstrapped_now: bool,
    bootstrap_bundle_limit: int,
) -> None:
    total_items = sum(len(b.all_items) for b in bundles)
    if bootstrapped_now:
        n_b = min(len(bundles), bootstrap_bundle_limit)
        bs_items = sum(len(b.all_items) for b in bundles[:n_b])
        note = f"(this run; first {n_b} bundles)"
    else:
        n_b = 0
        bs_items = 0
        note = "(loaded from disk or skipped)"
    print()
    print("Corpus accounting:")
    print(f"  bundles to stream:        {len(bundles)}")
    print(f"  total items (root+coms):  {total_items}")
    print(f"  bundles in bootstrap:     {n_b}")
    print(f"  items used for bootstrap: {bs_items}  {note}")


# ── Per-bundle printing ────────────────────────────────────────────────


def print_bundle_progress(
    state: StreamingState,
    bundle: ArticleBundle,
    result: ArticleProcessResult,
    *,
    index: int,
    total: int,
    before_counts: dict[str, int],
    snapshot_top_n: int,
) -> None:
    """Per-bundle output: header, per-entry diff, type-tally, per-event
    cluster snapshots.
    """
    after_counts = per_entry_counts(state.stance_catalog)
    by_type, n_cat, n_null = tally_assignments(state.stance_catalog)
    total_assigns = n_cat + n_null

    print(f"[{index}/{total}] {bundle.root.id}")

    changed = [
        eid for eid in set(before_counts) | set(after_counts)
        if before_counts.get(eid, 0) != after_counts.get(eid, 0)
    ]
    if changed:
        changed.sort(
            key=lambda eid: after_counts.get(eid, 0) - before_counts.get(eid, 0),
            reverse=True,
        )
        print("  catálogo (entradas que cambiaron en este bundle):")
        for eid in changed:
            before = before_counts.get(eid, 0)
            after = after_counts.get(eid, 0)
            entry = (
                state.stance_catalog.entries.get(eid)
                or state.stance_catalog.retired_entries.get(eid)
            )
            label = entry.label if entry else eid
            print(f"    {before:>3} → {after:<3}  {label}")

    print(f"  asignaciones acumuladas: {total_assigns}  "
          f"catalogadas: {n_cat}  sin catálogo: {n_null}")
    if by_type:
        tally = "  ".join(
            f"{t}={n}" for t, n in sorted(by_type.items(), key=lambda kv: -kv[1])
        )
        print(f"  por stance_type: {tally}")

    for etr in result.event_tag_results:
        if etr.event_id == "__bundle__":
            continue
        print_event_created_snapshot(
            state.stance_catalog,
            state.claim_catalogs,
            state.customer.entity_id,
            etr.event_id,
            top_n=snapshot_top_n,
        )


# ── Mid-stream consistency-pass trigger ────────────────────────────────


def run_consistency_pass_at_bundle(
    state: StreamingState,
    consistency_step: ConsistencyPassStep,
    *,
    index: int,
) -> ConsistencyPassResult:
    """Run consistency pass mid-stream and print before/after deltas."""
    print()
    print(f"=== Consistency pass @ bundle {index} ===")
    entries_before = len(state.stance_catalog.entries)
    asgn_before = len(state.stance_catalog.assignments)
    result = consistency_step.run(
        state.stance_catalog,
        state.items_seen,
    )
    entries_after = len(state.stance_catalog.entries)
    asgn_after = len(state.stance_catalog.assignments)
    print(f"  entradas:     {entries_before} → {entries_after}")
    print(f"  asignaciones: {asgn_before} → {asgn_after}  "
          f"(reroute puede mover, no crear)")
    print(f"  proposals: {len(result.proposals)}  "
          f"merges: {len(result.merge_pairs)}  "
          f"retires: {len(result.retire_ids)}  "
          f"reroutes: {len(result.reroute_pairs)}")
    if result.summary:
        print(f"  counters: {dict(result.summary.counters)}")
    print()
    print_catalog_overview(state.stance_catalog)
    print()
    return result


# ── End-of-run / on-demand catalog summary ─────────────────────────────


def print_catalogs_summary(
    state: StreamingState,
    *,
    top_k_entries_per_type: int = 5,
    sample_items_per_entry: int = 3,
    sample_clusters: int = 8,
    sample_claims_per_cluster: int = 3,
    text_limit: int = 120,
) -> None:
    """Compact dual-catalog summary.

    Stance catalog (block per `stance_type`):
        - top K entries by # of catalogued assignments
        - each entry: n_assignments + a sample of items tagged with it
          (item text snippet, plus the assignment's `reason`)

    Claim clusters (across all events):
        - top N clusters by member count
        - each cluster: canonical + sample of individual claim verbatims
    """
    sc = state.stance_catalog
    items = state.items_seen

    # Index assignments by stance_id and capture reason for each.
    by_entry: dict[str, list] = {}  # stance_id -> list[StanceAssignment]
    for a in sc.assignments:
        if a.stance_id:
            by_entry.setdefault(a.stance_id, []).append(a)

    # Group entries by primary_type.
    by_type: dict[str, list] = {}
    for e in sc.entries.values():
        by_type.setdefault(e.primary_type, []).append(e)

    print("Stance catalog summary:")
    if not sc.entries:
        print("  (empty)")
    else:
        ordered = list(_TYPE_ORDER) + [t for t in by_type if t not in _TYPE_ORDER]
        for t in ordered:
            entries = by_type.get(t) or []
            if not entries:
                continue
            ranked = sorted(
                entries,
                key=lambda e: (len(by_entry.get(e.id, [])), e.label.lower()),
                reverse=True,
            )[:top_k_entries_per_type]
            type_total = sum(len(by_entry.get(e.id, [])) for e in entries)
            counts_per_entry = [len(by_entry.get(e.id, [])) for e in entries]
            print(f"  {t}  ({len(entries)} entries, {type_total} catalogued assignments)")
            print(f"      items-per-entry: {_distribution_line(counts_per_entry)}")
            for e in ranked:
                assigns = by_entry.get(e.id, [])
                print(f"    • [{len(assigns):3d}] {e.label}")
                for a in assigns[:sample_items_per_entry]:
                    item = items.get(a.source_item_id)
                    snippet = _truncate(item.text if item else "", text_limit)
                    reason = _truncate(a.reason, 60)
                    print(f"        ↳ {a.source_kind:<13} {snippet}")
                    if reason:
                        print(f"          (reason: {reason})")

    # Claim clusters — flatten across events, rank by member count.
    print()
    print("Claim cluster summary:")
    all_clusters: list = []
    for cat in state.claim_catalogs.catalogs.values():
        for c in cat.clusters.values():
            all_clusters.append(c)
    if not all_clusters:
        print("  (empty)")
        return

    all_clusters.sort(key=lambda c: (len(c.members), c.importance_max), reverse=True)
    n_total = len(all_clusters)
    n_events = len({c.event_id for c in all_clusters})
    members_per_cluster = [len(c.members) for c in all_clusters]
    print(f"  {n_total} clusters across {n_events} event(s); showing top {min(sample_clusters, n_total)}")
    print(f"      members-per-cluster: {_distribution_line(members_per_cluster, singleton_label='singleton')}")
    for cluster in all_clusters[:sample_clusters]:
        print(f"  • [{len(cluster.members):3d}] {cluster.canonical}")
        print(f"      event={cluster.event_id}  importance_max={cluster.importance_max}")
        for claim in cluster.members[:sample_claims_per_cluster]:
            verbatim = _truncate(claim.verbatim, text_limit)
            print(f"        ↳ imp={claim.importance} {claim.source_kind:<13} {verbatim}")
    if n_total > sample_clusters:
        print(f"  … and {n_total - sample_clusters} more clusters")
