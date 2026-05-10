"""Triage, stance, and claim steps for tags_gpt."""

from __future__ import annotations

import os
from typing import Any

from src.entities.tags_gpt.catalogs import ClaimCatalogStore, StanceCatalog
from src.entities.tags_gpt.llm import JsonLlm
from src.entities.tags_gpt.models import (
    ArticleBundle,
    ClaimDecision,
    ClaimMutation,
    ClaimTagging,
    Customer,
    LinkedEventContext,
    RawClaim,
    STANCE_BEARING_TYPES,
    STANCE_TYPES,
    TAG_ONLY_TYPES,
    SourceItem,
    StanceAssignment,
    StanceDecision,
    StanceProposal,
    StanceTagging,
    StanceType,
    StepSummary,
    TypeTriageItem,
    TypeTriageResult,
)
from src.entities.tags_gpt.prompts import (
    claim_tagging_prompt,
    claim_update_prompt,
    compact_customer,
    compact_event,
    stance_tagging_prompt,
    type_triage_prompt,
)


def _local_items(items: list[SourceItem], *, limit: int = 1200) -> tuple[list[dict[str, Any]], dict[int, SourceItem]]:
    payload: list[dict[str, Any]] = []
    by_local: dict[int, SourceItem] = {}
    for index, item in enumerate(items, start=1):
        by_local[index] = item
        payload.append({"id": index, "kind": item.kind, "text": item.short_text(limit)})
    return payload, by_local


def _valid_stance_type(value: Any) -> bool:
    return value in STANCE_TYPES


class TypeTriageStep:
    def __init__(self, customer: Customer, llm: JsonLlm, *, model: str | None = None):
        self.customer = customer
        self.llm = llm
        self.model = model or os.environ.get("OPENROUTER_TYPE_TRIAGE_MODEL", "google/gemini-2.5-flash-lite")

    def triage(
        self,
        bundle: ArticleBundle,
        *,
        event: LinkedEventContext | None = None,
        batch_size: int = 15,
    ) -> TypeTriageResult:
        merged = TypeTriageResult(n_items_seen=len(bundle.items))
        for batch in self._batches(bundle, batch_size):
            batch_result = self._triage_batch(batch, event=event)
            merged.triaged.extend(batch_result.triaged)
            merged.dropped_invalid += batch_result.dropped_invalid
        merged.triaged, dropped = self._clean_rows(merged.triaged)
        merged.dropped_invalid += dropped
        return merged

    def _batches(self, bundle: ArticleBundle, batch_size: int) -> list[list[SourceItem]]:
        size = max(1, batch_size)
        batches = [[bundle.root]]
        for start in range(0, len(bundle.comments), size):
            batches.append([bundle.root, *bundle.comments[start:start + size]])
        return batches

    def _triage_batch(
        self,
        items: list[SourceItem],
        *,
        event: LinkedEventContext | None,
    ) -> TypeTriageResult:
        local_items, by_local = _local_items(items)
        payload = {
            "customer": compact_customer(self.customer),
            "event": compact_event(event),
            "items": local_items,
        }
        response = self.llm.complete_json(
            phase="type_triage",
            payload=payload,
            prompt=type_triage_prompt(payload),
            model=self.model,
        )
        result = TypeTriageResult(n_items_seen=len(items))
        for raw in response.get("triage") or []:
            if not isinstance(raw, dict):
                result.dropped_invalid += 1
                continue
            try:
                local_id = int(raw.get("source_item_id"))
            except (TypeError, ValueError):
                result.dropped_invalid += 1
                continue
            item = by_local.get(local_id)
            stance_type = raw.get("stance_type")
            if item is None or not _valid_stance_type(stance_type):
                result.dropped_invalid += 1
                continue
            result.triaged.append(
                TypeTriageItem(
                    source_item_id=item.id,
                    source_kind=item.kind,
                    stance_type=stance_type,
                    brief_summary=str(raw.get("brief_summary") or "").strip(),
                    importance_hint=raw.get("importance_hint") if raw.get("importance_hint") in {"low", "medium", "high"} else None,
                )
            )
        return result

    def _clean_rows(self, rows: list[TypeTriageItem]) -> tuple[list[TypeTriageItem], int]:
        by_item: dict[str, list[TypeTriageItem]] = {}
        for row in rows:
            by_item.setdefault(row.source_item_id, []).append(row)
        cleaned: list[TypeTriageItem] = []
        dropped = 0
        for item_rows in by_item.values():
            noise = [row for row in item_rows if row.stance_type == "noise"]
            if noise:
                cleaned.append(noise[0])
                dropped += len(item_rows) - 1
                continue
            seen: set[tuple[str, str]] = set()
            kept: list[TypeTriageItem] = []
            for row in item_rows:
                key = (row.source_item_id, row.stance_type)
                if key in seen:
                    dropped += 1
                    continue
                seen.add(key)
                kept.append(row)
            cleaned.extend(kept[:4])
            dropped += max(0, len(kept) - 4)
        return cleaned, dropped


class StanceTagger:
    def __init__(self, customer: Customer, llm: JsonLlm, *, model: str | None = None):
        self.customer = customer
        self.llm = llm
        self.model = model or os.environ.get("OPENROUTER_STANCE_TAGGER_MODEL", "google/gemini-2.5-flash-lite")

    def tag(
        self,
        *,
        event: LinkedEventContext | None,
        items: list[SourceItem],
        catalog: StanceCatalog,
        stance_type: StanceType,
        triage_hints: list[TypeTriageItem],
    ) -> StanceTagging:
        if stance_type not in STANCE_BEARING_TYPES or not triage_hints:
            return StanceTagging()
        candidate_ids = {hint.source_item_id for hint in triage_hints}
        candidate_items = [item for item in items if item.id in candidate_ids]
        local_items, by_local = _local_items(candidate_items)
        payload = {
            "customer": compact_customer(self.customer),
            "event": compact_event(event),
            "stance_type": stance_type,
            "items": local_items,
            "triage_hints": [
                {
                    "source_item_id": index,
                    "stance_type": stance_type,
                    "brief_summary": hint.brief_summary,
                    "importance_hint": hint.importance_hint,
                }
                for index, item in by_local.items()
                for hint in triage_hints
                if hint.source_item_id == item.id
            ],
            "catalog": [
                {"id": entry.id, "label": entry.label, "description": entry.description}
                for entry in catalog.iter_entries({stance_type})
            ],
        }
        response = self.llm.complete_json(
            phase="stance_tagging",
            payload=payload,
            prompt=stance_tagging_prompt(payload, stance_type),
            model=self.model,
        )
        result = StanceTagging()
        for raw in response.get("assignments") or []:
            assignment = self._parse_assignment(raw, by_local, event, stance_type)
            if assignment is None:
                result.dropped_assignments += 1
                continue
            result.assignments.append(assignment)
            result.n_assignments_by_type[assignment.stance_type] = result.n_assignments_by_type.get(assignment.stance_type, 0) + 1
        for raw in response.get("proposals") or []:
            proposal = self._parse_proposal(raw, by_local, stance_type)
            if proposal:
                result.proposals.append(proposal)
        result.n_items_tagged_no_stance = len({a.source_item_id for a in result.assignments if a.stance_id is None})
        return result

    def _parse_assignment(
        self,
        raw: dict[str, Any],
        by_local: dict[int, SourceItem],
        event: LinkedEventContext | None,
        stance_type: StanceType,
    ) -> StanceAssignment | None:
        try:
            item = by_local[int(raw.get("source_item_id"))]
        except (KeyError, TypeError, ValueError):
            return None
        if raw.get("stance_type", stance_type) != stance_type:
            return None
        return StanceAssignment(
            source_item_id=item.id,
            source_kind=item.kind,
            customer_id=self.customer.entity_id,
            stance_id=raw.get("stance_id"),
            stance_type=stance_type,
            event_id=event.id if event else None,
            reason=str(raw.get("reason") or raw.get("brief_summary") or ""),
        )

    def _parse_proposal(
        self,
        raw: dict[str, Any],
        by_local: dict[int, SourceItem],
        stance_type: StanceType,
    ) -> StanceProposal | None:
        if raw.get("kind") not in {"add", "rename"}:
            return None
        if raw.get("stance_type", stance_type) != stance_type:
            return None
        label = str(raw.get("label") or "").strip()
        if not label:
            return None
        source_ids: list[str] = []
        for local_id in raw.get("source_item_ids") or []:
            try:
                item = by_local[int(local_id)]
            except (KeyError, TypeError, ValueError):
                continue
            source_ids.append(item.id)
        return StanceProposal(
            kind=raw["kind"],
            label=label,
            description=str(raw.get("description") or "").strip(),
            stance_type=stance_type,
            source_item_ids=source_ids,
            src_stance_id=raw.get("src_stance_id") or raw.get("existing_id"),
        )


class StanceUpdater:
    def __init__(self, customer: Customer):
        self.customer = customer

    def update(self, catalog: StanceCatalog, tagging: StanceTagging) -> StepSummary:
        summary = StepSummary("stance_update")
        accepted_proposals: dict[int, str] = {}
        for assignment in tagging.assignments:
            if catalog.assign(assignment):
                summary.inc("assign")
            else:
                summary.inc("reject_assignment")
        for index, proposal in enumerate(tagging.proposals):
            if proposal.kind == "add":
                entry_id = self._apply_add(catalog, proposal, summary)
                if entry_id:
                    accepted_proposals[index] = entry_id
            elif proposal.kind == "rename":
                if catalog.rename(proposal.src_stance_id or "", proposal.label, proposal.description):
                    summary.inc("rename")
                else:
                    summary.inc("reject_proposal")
        return summary

    def _apply_add(self, catalog: StanceCatalog, proposal: StanceProposal, summary: StepSummary) -> str | None:
        if proposal.stance_type not in STANCE_BEARING_TYPES or not proposal.label:
            summary.inc("reject_proposal")
            return None
        entry = catalog.add(proposal.label, proposal.description, primary_type=proposal.stance_type)
        for assignment in catalog.assignments:
            if assignment.source_item_id in set(proposal.source_item_ids) and assignment.stance_type == proposal.stance_type:
                assignment.stance_id = entry.id
        summary.inc("add")
        return entry.id


class ClaimTagger:
    def __init__(
        self,
        customer: Customer,
        llm: JsonLlm,
        *,
        include_comments: bool = False,
        model: str | None = None,
    ):
        self.customer = customer
        self.llm = llm
        self.include_comments = include_comments
        self.model = model or os.environ.get("OPENROUTER_CLAIM_TAGGER_MODEL", "google/gemini-2.5-flash-lite")

    def tag(
        self,
        *,
        bundle: ArticleBundle,
        event: LinkedEventContext,
        claim_catalogs: ClaimCatalogStore,
    ) -> ClaimTagging:
        items = [bundle.root, *bundle.comments] if self.include_comments else [bundle.root]
        local_items, by_local = _local_items(items)
        existing = claim_catalogs.get(self.customer.entity_id, event.id).summary()
        payload = {
            "customer": compact_customer(self.customer, include_id=True),
            "event": compact_event(event),
            "existing_clusters": [{"canonical": row["canonical"]} for row in existing],
            "items": local_items,
        }
        response = self.llm.complete_json(
            phase="claim_tagging",
            payload=payload,
            prompt=claim_tagging_prompt(payload),
            model=self.model,
        )
        result = ClaimTagging()
        for raw in response.get("claims") or []:
            try:
                item = by_local[int(raw.get("source_item_id"))]
            except (KeyError, TypeError, ValueError):
                result.dropped_invalid += 1
                continue
            verbatim = str(raw.get("verbatim") or "").strip()
            if not verbatim:
                result.dropped_invalid += 1
                continue
            importance = raw.get("importance", 1)
            try:
                importance = max(1, min(3, int(importance)))
            except (TypeError, ValueError):
                importance = 1
            result.claims.append(
                RawClaim(
                    event_id=event.id,
                    customer_id=self.customer.entity_id,
                    verbatim=verbatim,
                    source_item_id=item.id,
                    source_kind=item.kind,
                    importance=importance,
                    importance_reason=str(raw.get("importance_reason") or ""),
                )
            )
        return result


class ClaimUpdater:
    def __init__(self, customer: Customer, llm: JsonLlm, *, model: str | None = None):
        self.customer = customer
        self.llm = llm
        self.model = model or os.environ.get("OPENROUTER_CLAIM_UPDATER_MODEL", "google/gemini-2.5-flash-lite")

    def update(
        self,
        *,
        claim_catalogs: ClaimCatalogStore,
        event: LinkedEventContext,
        tagging: ClaimTagging,
    ) -> StepSummary:
        summary = StepSummary("claim_update")
        if not tagging.claims:
            return summary
        catalog = claim_catalogs.get(self.customer.entity_id, event.id)
        payload = {
            "event": compact_event(event),
            "clusters": catalog.summary(),
            "claims": [
                {"claim_index": index, "verbatim": claim.verbatim, "importance": claim.importance}
                for index, claim in enumerate(tagging.claims, start=1)
            ],
        }
        response = self.llm.complete_json(
            phase="claim_update",
            payload=payload,
            prompt=claim_update_prompt(payload),
            model=self.model,
        )
        decisions = self._parse_decisions(response.get("decisions") or [])
        mutations = self._parse_mutations(response.get("mutations") or [])
        for mutation in mutations:
            if mutation.kind == "rename" and catalog.rename(mutation.cluster_id or "", mutation.new_canonical or ""):
                summary.inc("rename")
            elif mutation.kind == "merge" and catalog.merge(mutation.src_id or "", mutation.dst_id or ""):
                summary.inc("merge")
        claims_by_index = {index: claim for index, claim in enumerate(tagging.claims, start=1)}
        for decision in decisions:
            claim = claims_by_index.get(decision.claim_index)
            if not claim:
                summary.inc("drop")
                continue
            if decision.action == "assign" and decision.cluster_id and catalog.assign(claim, decision.cluster_id):
                summary.inc("assign")
            elif decision.action == "create" and decision.canonical:
                catalog.create(claim, decision.canonical)
                summary.inc("create")
            else:
                summary.inc("drop")
        return summary

    def _parse_decisions(self, rows: list[Any]) -> list[ClaimDecision]:
        out: list[ClaimDecision] = []
        for raw in rows:
            if not isinstance(raw, dict) or raw.get("action") not in {"assign", "create", "drop"}:
                continue
            try:
                claim_index = int(raw.get("claim_index"))
            except (TypeError, ValueError):
                continue
            out.append(
                ClaimDecision(
                    claim_index=claim_index,
                    action=raw["action"],
                    cluster_id=raw.get("cluster_id"),
                    canonical=raw.get("canonical"),
                    reason=str(raw.get("reason") or ""),
                )
            )
        return out

    def _parse_mutations(self, rows: list[Any]) -> list[ClaimMutation]:
        out: list[ClaimMutation] = []
        for raw in rows:
            if not isinstance(raw, dict) or raw.get("kind") not in {"rename", "merge"}:
                continue
            out.append(
                ClaimMutation(
                    kind=raw["kind"],
                    cluster_id=raw.get("cluster_id"),
                    new_canonical=raw.get("new_canonical"),
                    src_id=raw.get("src_id"),
                    dst_id=raw.get("dst_id"),
                )
            )
        return out

