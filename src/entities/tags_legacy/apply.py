"""Phase 5 — apply Phase 2/3/4 results into the catalogs.

Kept separate from the orchestrators so the streaming runner can
- run Phase 2 (tagging),
- pre-create placeholder stance entries for proposals (so Phase 2's
  assignments under those proposals can be threaded through),
- run Phase 3 (adjudicator) on the proposals,
- run Phase 4 (clusterer) on the claims,
- apply everything via this module.

Renames are id-stable: assignments reference entries by id, so renames
and merges propagate retroactively.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.entities.tags.models.claim_catalog import (
    ClaimCatalog,
    ClaimCatalogRegistry,
    RawClaim,
)
from src.entities.tags.claim_clusterer import (
    ClusteringDecision,
    ClusteringMutation,
    ClusteringResult,
)
from src.entities.tags.stance_adjudicator import AdjudicationDecision
from src.entities.tags.models.stance_catalog import (
    StanceAssignment,
    StanceCatalog,
    StanceEntry,
)
from src.entities.tags.tagging import StanceProposal, TaggingResult

logger = logging.getLogger(__name__)


# ── Stance side ──────────────────────────────────────────────────────


def stage_proposals_as_placeholders(
    catalog: StanceCatalog, proposals: list[StanceProposal]
) -> dict[str, str]:
    """Add a placeholder entry per `add` proposal so Phase 2's stance
    assignments referencing the *new* label can be staged before Phase 3
    decides. Returns a `proposal_id → stance_id` map; for `rename`
    proposals the mapping points at `src_stance_id` (no placeholder).
    """
    out: dict[str, str] = {}
    for p in proposals:
        if not p.proposal_id:
            continue
        if p.kind == "add":
            ph = StanceEntry.new(p.label, p.description, id=p.proposal_id)
            catalog.add(ph)
            out[p.proposal_id] = ph.id
        elif p.kind == "rename" and p.src_stance_id in catalog.entries:
            out[p.proposal_id] = p.src_stance_id
    return out


def apply_stance_phase(
    catalog: StanceCatalog,
    customer_id: int,
    event_id: Optional[str],
    tagging: TaggingResult,
    adjudications: list[AdjudicationDecision],
) -> dict:
    """Apply tagging + adjudication into the catalog.

    Order:
      1. Stage `add` proposals as placeholder entries (so Phase 2's
         stance assignments under them have somewhere to live).
      2. Apply Phase 2 stance assignments — pointed at either an
         existing entry or a placeholder.
      3. Apply adjudicator decisions on the placeholders:
         - accept → keep placeholder as-is (already in catalog).
         - reject → drop placeholder + its assignments.
         - rename → rewrite an existing entry's label/description; the
           placeholder's assignments are re-pointed at the renamed
           existing entry, then placeholder removed.
         - generalise → re-point placeholder's assignments at an
           existing entry, drop placeholder.
    """
    proposal_to_id = stage_proposals_as_placeholders(catalog, tagging.stance_proposals)

    # Step 2 — assignments. For each Phase-2 assignment, the orchestrator
    # already pointed `stance_id` at an existing catalog entry. But if the
    # tagging LLM had emitted an assignment under a *proposed* label, the
    # orchestrator would have dropped it (because the stance_id wasn't in
    # `valid_stance_ids` at filter time). Phase 2 keeps assignments on
    # existing entries only — proposals introduce new labels but don't
    # carry assignments themselves. This is fine for v1.
    for sa in tagging.stance_assignments:
        if sa["stance_id"] not in catalog.entries:
            continue
        catalog.assign(
            StanceAssignment(
                source_item_id=sa["source_item_id"],
                source_kind=sa["source_kind"],
                customer_id=customer_id,
                stance_id=sa["stance_id"],
                event_id=event_id,
                reason=sa.get("reason", ""),
            )
        )

    # Step 3 — adjudications.
    n_accept = n_reject = n_rename = n_generalise = 0
    proposals = tagging.stance_proposals
    for d in adjudications:
        if not (0 <= d.proposal_index < len(proposals)):
            continue
        p = proposals[d.proposal_index]
        placeholder_id = proposal_to_id.get(p.proposal_id) if p.proposal_id else None
        if d.decision == "accept":
            n_accept += 1
            # keep placeholder as the canonical entry; nothing to do.
        elif d.decision == "reject":
            n_reject += 1
            if placeholder_id and p.kind == "add":
                catalog.drop_assignments_for(placeholder_id)
        elif d.decision == "rename":
            n_rename += 1
            if d.existing_id and d.new_label:
                catalog.rename(
                    d.existing_id,
                    d.new_label,
                    d.new_description or "",
                )
            if placeholder_id and p.kind == "add":
                catalog.reroute_assignments(placeholder_id, d.existing_id or placeholder_id)
        elif d.decision == "generalise":
            n_generalise += 1
            if placeholder_id and p.kind == "add" and d.existing_id:
                catalog.reroute_assignments(placeholder_id, d.existing_id)

    return {
        "n_assignments_applied": len(tagging.stance_assignments),
        "n_proposals": len(proposals),
        "n_accept": n_accept,
        "n_reject": n_reject,
        "n_rename": n_rename,
        "n_generalise": n_generalise,
    }


# ── Claim side ───────────────────────────────────────────────────────


def apply_claim_phase(
    registry: ClaimCatalogRegistry,
    customer_id: int,
    event_id: str,
    raw_claims: list[RawClaim],
    clustering: ClusteringResult,
    freshness_window_hours: int = 24,
) -> dict:
    catalog = registry.get_or_create(customer_id, event_id)
    n_create = n_assign = n_drop = 0

    for d in clustering.decisions:
        if not (0 <= d.claim_index < len(raw_claims)):
            continue
        rc = raw_claims[d.claim_index]
        if d.decision == "assign" and d.cluster_id in catalog.clusters:
            catalog.assign_to_existing(rc, d.cluster_id)
            n_assign += 1
        elif d.decision == "create" and d.canonical:
            catalog.create_new(rc, d.canonical, freshness_window_hours)
            n_create += 1
        elif d.decision == "drop":
            n_drop += 1

    n_renames = n_merges = 0
    for m in clustering.mutations:
        if m.kind == "rename" and m.cluster_id and m.new_canonical:
            catalog.rename(m.cluster_id, m.new_canonical)
            n_renames += 1
        elif m.kind == "merge" and m.src_id and m.dst_id:
            catalog.merge(m.src_id, m.dst_id)
            n_merges += 1

    return {
        "n_create": n_create,
        "n_assign": n_assign,
        "n_drop": n_drop,
        "n_renames": n_renames,
        "n_merges": n_merges,
    }
