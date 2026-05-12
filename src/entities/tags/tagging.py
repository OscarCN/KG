"""Stance tagging + claim tagging step classes (design §5.3-§5.6).

Four classes:

- `StanceTagger`     — per-type LLM call producing `StanceTagging`.
- `StanceUpdater`    — deterministic; applies assignments + add/rename
                       proposals. No LLM (design §5.4).
- `ClaimTagger`      — per-(root, event_id) LLM call producing `RawClaim`s.
- `ClaimUpdater`     — LLM-driven cluster routing (assign/create/drop) +
                       optional mutations (rename/merge).
"""

from __future__ import annotations

import logging
from typing import Optional

from src.entities.tags.catalogs import (
    ClaimCatalog,
    StanceCatalog,
    make_entry_id,
)
from src.entities.tags.llm import JsonLlm
from src.entities.tags.models import (
    ClaimAssignment,
    ClaimCluster,
    ClaimTagging,
    Customer,
    LinkedEventContext,
    RawClaim,
    SourceItem,
    SourceKind,
    StanceAssignment,
    StanceEntry,
    StanceProposal,
    StanceTagging,
    StanceType,
    StepSummary,
    TypeTriageItem,
    now_iso,
)
from src.entities.tags.prompts import (
    claim_extract_prompt,
    claim_group_prompt,
    tag_prompt_for_type,
)


logger = logging.getLogger(__name__)


# ── StanceTagger ────────────────────────────────────────────────────────


class StanceTagger:
    """Maps triaged ideas of one stance_type to catalog entries (or null)
    plus optional add/rename proposals.
    """

    def __init__(self, customer: Customer, llm: JsonLlm):
        self.customer = customer
        self.llm = llm

    def tag(
        self,
        catalog: StanceCatalog,
        *,
        stance_type: StanceType,
        items: list[SourceItem],
        triage_hints: list[TypeTriageItem],
        event: Optional[LinkedEventContext] = None,
    ) -> StanceTagging:
        result = StanceTagging()
        if not triage_hints:
            return result

        catalog_slice = catalog.snapshot(types=[stance_type])
        valid_ids = {entry["id"] for entry in catalog_slice}
        kind_by_id: dict[str, SourceKind] = {
            item.id: item.kind for item in items  # type: ignore[misc]
        }

        triage_id_map: dict[int, str] = {}
        stance_id_map: dict[str, str] = {}
        prompt = tag_prompt_for_type(
            self.customer,
            items,
            triage_hints,
            catalog_slice,
            stance_type,
            event=event,
            triage_id_map=triage_id_map,
            stance_id_map=stance_id_map,
        )
        response = self.llm.call(prompt)
        if not isinstance(response, dict):
            logger.warning("stance tag: malformed response (not an object)")
            return result

        def _resolve_stance_id(raw_stance_id) -> Optional[str]:
            """Map LLM stance_id back to canonical: short `st_N` →
            stance_id_map; falls back to canonical id (for cached
            pre-rename responses); unknown → None."""
            if raw_stance_id in (None, "", "null"):
                return None
            if not isinstance(raw_stance_id, str):
                return None
            canonical_short = stance_id_map.get(raw_stance_id)
            if canonical_short is not None:
                return canonical_short
            if raw_stance_id in valid_ids:
                return raw_stance_id  # backward-compat with canonical ids
            logger.debug("stance tag: unknown stance_id %s; null-routed", raw_stance_id)
            return None

        # Assignments
        for raw in response.get("assignments") or []:
            if not isinstance(raw, dict):
                continue
            # Short key at the LLM boundary (`id`); fall back to the long
            # key (`source_item_id`) for any cached pre-rename response.
            local_id = raw.get("id", raw.get("source_item_id"))
            try:
                local_id_int = int(local_id)
            except (TypeError, ValueError):
                result.dropped_assignments += 1
                continue
            canonical = triage_id_map.get(local_id_int)
            if canonical is None:
                result.dropped_assignments += 1
                continue
            stance_id = _resolve_stance_id(raw.get("stance_id"))
            assignment = StanceAssignment(
                source_item_id=canonical,
                source_kind=kind_by_id.get(canonical, "user_comment"),
                customer_id=self.customer.entity_id,
                stance_id=stance_id,
                stance_type=stance_type,
                event_id=event.id if event else None,
                reason=str(raw.get("reason") or ""),
                assigned_at=now_iso(),
            )
            result.assignments.append(assignment)
            result.n_assignments_by_type[stance_type] = (
                result.n_assignments_by_type.get(stance_type, 0) + 1
            )

        # Proposals
        for raw in response.get("proposals") or []:
            if not isinstance(raw, dict):
                continue
            kind = raw.get("kind")
            if kind not in {"add", "rename"}:
                continue
            label = str(raw.get("label") or "").strip()
            if not label:
                continue
            description = str(raw.get("description") or "")
            source_item_ids: list[str] = []
            for sid in raw.get("source_item_ids") or []:
                try:
                    sid_int = int(sid)
                except (TypeError, ValueError):
                    continue
                canonical = triage_id_map.get(sid_int)
                if canonical:
                    source_item_ids.append(canonical)
            src_id = _resolve_stance_id(raw.get("src_stance_id")) if kind == "rename" else None
            if kind == "rename" and src_id is None:
                continue
            # `add` requires ≥2 distinct evidence ids — the prompt says so,
            # but LLMs slip; we drop the proposal here rather than poison
            # the catalog with one-shot entries.
            if kind == "add" and len(set(source_item_ids)) < 2:
                logger.debug(
                    "stance tag: drop add-proposal %r (only %d distinct evidence ids)",
                    label,
                    len(set(source_item_ids)),
                )
                continue
            result.proposals.append(
                StanceProposal(
                    kind=kind,
                    label=label,
                    description=description,
                    stance_type=stance_type,
                    source_item_ids=source_item_ids,
                    src_stance_id=src_id,
                )
            )

        # The prompt instructs the LLM to OMIT assignments for items where
        # no catalog entry fits and the postura isn't worth proposing — to
        # save output tokens. We synthesize a null `StanceAssignment` for
        # each triage_hint missing from the LLM response so the catalog
        # still has a row of "we saw this item, classified it as <type>,
        # no entry fit". The hint's brief_summary becomes the reason.
        assigned_ids = {a.source_item_id for a in result.assignments}
        first_hint_by_id: dict[str, TypeTriageItem] = {}
        for hint in triage_hints:
            first_hint_by_id.setdefault(hint.source_item_id, hint)
        for sid, hint in first_hint_by_id.items():
            if sid in assigned_ids:
                continue
            result.assignments.append(
                StanceAssignment(
                    source_item_id=sid,
                    source_kind=kind_by_id.get(sid, hint.source_kind),
                    customer_id=self.customer.entity_id,
                    stance_id=None,
                    stance_type=stance_type,
                    event_id=event.id if event else None,
                    reason=hint.brief_summary,
                    assigned_at=now_iso(),
                )
            )
            result.n_assignments_by_type[stance_type] = (
                result.n_assignments_by_type.get(stance_type, 0) + 1
            )

        # Items with no entry — recompute now that synthesized rows are in.
        assigned_with_entry = {
            a.source_item_id for a in result.assignments if a.stance_id is not None
        }
        triaged_ids = {h.source_item_id for h in triage_hints}
        result.n_items_tagged_no_stance = len(triaged_ids - assigned_with_entry)
        return result


# ── StanceUpdater (deterministic — no LLM, design §5.4) ────────────────


class StanceUpdater:
    """Applies assignments + add/rename proposals to a `StanceCatalog`.

    Deterministic: validation rules decide accept/reject. No adjudicator
    LLM (design §5.4 — drift is the consistency-pass's job).
    """

    def update(self, catalog: StanceCatalog, tagging: StanceTagging) -> StepSummary:
        summary = StepSummary(name="stance_update")

        # 1. Apply proposals first so that subsequent assignments can route
        #    to newly-added entries (when the LLM emits a `null` assignment
        #    next to an `add` proposal, or vice versa).
        new_entry_id_by_label: dict[tuple[str, str], str] = {}
        for proposal in tagging.proposals:
            if proposal.kind == "add":
                if proposal.stance_type in {"noise"}:
                    summary.inc("rejected_proposal_tag_only")
                    continue
                # De-dup against existing entry of same primary_type with
                # exact label match (case-insensitive).
                norm = proposal.label.strip().lower()
                existing = next(
                    (
                        e
                        for e in catalog.iter_entries(types=[proposal.stance_type])
                        if e.label.strip().lower() == norm
                    ),
                    None,
                )
                if existing is not None:
                    summary.inc("proposal_add_already_exists")
                    new_entry_id_by_label[(proposal.stance_type, norm)] = existing.id
                    continue
                entry = catalog.add(
                    label=proposal.label,
                    description=proposal.description,
                    primary_type=proposal.stance_type,
                )
                new_entry_id_by_label[(proposal.stance_type, norm)] = entry.id
                summary.inc("proposal_add_accepted")
            elif proposal.kind == "rename":
                if not proposal.src_stance_id:
                    summary.inc("rejected_proposal_rename_no_src")
                    continue
                ok = catalog.rename(
                    proposal.src_stance_id, proposal.label, proposal.description
                )
                if ok:
                    summary.inc("proposal_rename_accepted")
                else:
                    summary.inc("rejected_proposal_rename_unknown_src")

        # 2. Apply assignments.
        for assignment in tagging.assignments:
            if assignment.stance_id is None:
                # Try to route to a newly-added entry of matching type+label.
                # The model can hint at this by emitting an assignment with
                # null stance_id alongside an `add` proposal.
                # (We leave this minimal; the consistency pass owns
                # null-row consolidation.)
                pass
            if catalog.assign(assignment):
                summary.inc("assignment_accepted")
                if assignment.stance_id is None:
                    summary.inc("assignment_uncatalogued")
            else:
                summary.inc("assignment_rejected")

        summary.inc("dropped_assignments", tagging.dropped_assignments)
        return summary


# ── ClaimTagger ─────────────────────────────────────────────────────────


class ClaimTagger:
    """Extract raw factual claims from items linked to one event.

    Existing clusters are passed for de-duplication awareness only — this
    step does NOT route claims into clusters (that's `ClaimUpdater`).
    """

    def __init__(self, customer: Customer, llm: JsonLlm, *, include_comments: bool = False):
        self.customer = customer
        self.llm = llm
        self.include_comments = include_comments

    def tag(
        self,
        event: LinkedEventContext,
        root: SourceItem,
        comments: list[SourceItem],
        existing_clusters: list[ClaimCluster],
    ) -> ClaimTagging:
        result = ClaimTagging()
        items: list[SourceItem] = [root]
        if self.include_comments:
            items.extend(comments)
        if not items:
            return result

        id_map: dict[int, str] = {}
        kind_by_id: dict[str, SourceKind] = {
            item.id: item.kind for item in items  # type: ignore[misc]
        }
        prompt = claim_extract_prompt(
            self.customer,
            event,
            items,
            existing_clusters,
            id_map=id_map,
        )
        response = self.llm.call(prompt)
        if not isinstance(response, dict):
            logger.warning("claim extract: malformed response (not an object)")
            return result

        for raw in response.get("claims") or []:
            if not isinstance(raw, dict):
                continue
            # Short key at the LLM boundary (`id`); fall back to the long
            # key (`source_item_id`) for any cached pre-rename response.
            local_id = raw.get("id", raw.get("source_item_id"))
            try:
                local_id_int = int(local_id)
            except (TypeError, ValueError):
                result.dropped_invalid += 1
                continue
            canonical = id_map.get(local_id_int)
            if canonical is None:
                result.dropped_invalid += 1
                continue
            verbatim = str(raw.get("verbatim") or "").strip()
            if not verbatim:
                result.dropped_invalid += 1
                continue
            try:
                importance = int(raw.get("importance") or 1)
            except (TypeError, ValueError):
                importance = 1
            importance = max(1, min(3, importance))
            result.claims.append(
                RawClaim(
                    event_id=event.id,
                    customer_id=self.customer.entity_id,
                    verbatim=verbatim,
                    source_item_id=canonical,
                    source_kind=kind_by_id.get(canonical, "article"),
                    importance=importance,
                    importance_reason=str(raw.get("importance_reason") or ""),
                    extracted_at=now_iso(),
                )
            )
        return result


# ── ClaimUpdater ────────────────────────────────────────────────────────


class ClaimUpdater:
    """Routes raw claims into per-event clusters via one LLM call."""

    def __init__(self, customer: Customer, llm: JsonLlm):
        self.customer = customer
        self.llm = llm

    def update(
        self,
        catalog: ClaimCatalog,
        event: LinkedEventContext,
        raw_claims: list[RawClaim],
    ) -> StepSummary:
        summary = StepSummary(name="claim_update")
        if not raw_claims:
            return summary

        existing_clusters = list(catalog.clusters.values())
        existing_ids = {c.id for c in existing_clusters}

        id_map: dict[int, int] = {}
        cluster_id_map: dict[str, str] = {}
        prompt = claim_group_prompt(
            self.customer,
            event,
            raw_claims,
            existing_clusters,
            id_map=id_map,
            cluster_id_map=cluster_id_map,
        )
        response = self.llm.call(prompt)
        if not isinstance(response, dict):
            logger.warning("claim group: malformed response (not an object)")
            return summary

        def _resolve_cluster_id(raw_cid) -> Optional[str]:
            """Map LLM cluster_id back to canonical: `cl_N` →
            cluster_id_map; fall back to canonical (cached pre-rename
            responses); unknown → None."""
            if raw_cid in (None, "", "null"):
                return None
            if not isinstance(raw_cid, str):
                return None
            canonical_short = cluster_id_map.get(raw_cid)
            if canonical_short is not None:
                return canonical_short
            if raw_cid in existing_ids:
                return raw_cid
            return None

        # Decisions
        seen_indices: set[int] = set()
        for raw in response.get("decisions") or []:
            if not isinstance(raw, dict):
                continue
            # Short key at the LLM boundary (`idx`); fall back to the long
            # key (`claim_index`) for any cached pre-rename response.
            raw_idx = raw.get("idx", raw.get("claim_index"))
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                summary.inc("decision_dropped_bad_index")
                continue
            if idx < 0 or idx >= len(raw_claims) or idx in seen_indices:
                summary.inc("decision_dropped_bad_index")
                continue
            seen_indices.add(idx)
            action = raw.get("action")
            claim = raw_claims[idx]
            if action == "assign":
                cid = _resolve_cluster_id(raw.get("cluster_id"))
                if cid is None:
                    summary.inc("decision_dropped_unknown_cluster")
                    continue
                if catalog.assign(claim, cid):
                    summary.inc("assigned")
                else:
                    summary.inc("decision_dropped_assign_failed")
            elif action == "create":
                canonical = str(raw.get("canonical") or "").strip()
                if not canonical:
                    summary.inc("decision_dropped_empty_canonical")
                    continue
                cluster = catalog.create(claim, canonical)
                existing_ids.add(cluster.id)
                summary.inc("created")
            elif action == "drop":
                summary.inc("dropped_by_llm")
            else:
                summary.inc("decision_dropped_unknown_action")

        # Mutations (rename / merge)
        for raw in response.get("mutations") or []:
            if not isinstance(raw, dict):
                continue
            kind = raw.get("kind")
            if kind == "rename":
                cid = _resolve_cluster_id(raw.get("cluster_id"))
                new_canonical = str(raw.get("new_canonical") or "").strip()
                if cid is None or not new_canonical:
                    summary.inc("mutation_rename_invalid")
                    continue
                if catalog.rename(cid, new_canonical):
                    summary.inc("mutation_renamed")
            elif kind == "merge":
                src_id = _resolve_cluster_id(raw.get("src_id"))
                dst_id = _resolve_cluster_id(raw.get("dst_id"))
                if src_id is None or dst_id is None or src_id == dst_id:
                    summary.inc("mutation_merge_invalid")
                    continue
                moved = catalog.merge(src_id, dst_id)
                if moved:
                    summary.inc("mutation_merged")
                    existing_ids.discard(src_id)

        return summary
