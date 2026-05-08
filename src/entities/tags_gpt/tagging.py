"""Separate stance and claim tagging/updating steps."""

from __future__ import annotations

import os
from typing import Optional

from src.entities.tags_gpt.catalogs import ClaimCatalog, ClaimCatalogStore, StanceCatalog
from src.entities.tags_gpt.llm import JsonLlm
from src.entities.tags_gpt.models import (
    ClaimDecision,
    ClaimMutation,
    ClaimTagging,
    Customer,
    LinkedEvent,
    RawClaim,
    SourceItem,
    StanceAssignment,
    StanceDecision,
    StanceProposal,
    StanceTagging,
    StepSummary,
)
from src.entities.tags_gpt.prompts import (
    claim_tagging_prompt,
    claim_update_prompt,
    stance_tagging_prompt,
    stance_update_prompt,
)


class StanceTagger:
    def __init__(self, customer: Customer, llm: JsonLlm, *, model: Optional[str] = None):
        self.customer = customer
        self.llm = llm
        self.model = model or os.environ.get("OPENROUTER_STANCE_TAGGER_MODEL", "openai/gpt-4o")

    def tag(self, event: LinkedEvent, items: list[SourceItem], catalog: StanceCatalog) -> StanceTagging:
        if not items:
            return StanceTagging()
        payload = {
            "customer_id": self.customer.entity_id,
            "event_id": event.id,
            "catalog": catalog.snapshot(),
            "items": [{"id": item.id, "kind": item.kind, "text": item.short_text()} for item in items],
        }
        response = self.llm.complete_json(
            phase="stance_tagging",
            payload=payload,
            prompt=stance_tagging_prompt(self.customer, event, items, catalog.snapshot()),
            model=self.model,
        )
        return self._parse_response(event.id, items, catalog, response)

    def _parse_response(
        self,
        event_id: str,
        items: list[SourceItem],
        catalog: StanceCatalog,
        response: dict,
    ) -> StanceTagging:
        item_kind = {item.id: item.kind for item in items}
        result = StanceTagging()
        for raw in response.get("assignments") or []:
            source_item_id = str(raw.get("source_item_id") or "")
            stance_id = str(raw.get("stance_id") or "")
            if source_item_id not in item_kind or stance_id not in catalog.entries:
                result.dropped_assignments += 1
                continue
            result.assignments.append(
                StanceAssignment(
                    source_item_id=source_item_id,
                    source_kind=item_kind[source_item_id],
                    customer_id=self.customer.entity_id,
                    stance_id=stance_id,
                    event_id=event_id,
                    reason=str(raw.get("reason") or "")[:500],
                )
            )

        for raw in response.get("proposals") or []:
            kind = raw.get("kind")
            label = str(raw.get("label") or "").strip()
            if kind not in ("add", "rename") or not label:
                continue
            result.proposals.append(
                StanceProposal(
                    kind=kind,
                    label=label,
                    description=str(raw.get("description") or "").strip(),
                    source_item_ids=[str(x) for x in raw.get("source_item_ids") or []],
                    src_stance_id=raw.get("src_stance_id"),
                )
            )
        return result


class StanceUpdater:
    def __init__(self, customer: Customer, llm: Optional[JsonLlm] = None, *, model: Optional[str] = None):
        self.customer = customer
        self.llm = llm
        self.model = model or os.environ.get("OPENROUTER_STANCE_UPDATER_MODEL", "openai/gpt-4o")

    def update(
        self,
        catalog: StanceCatalog,
        tagging: StanceTagging,
        *,
        sample_items: list[SourceItem],
    ) -> StepSummary:
        summary = StepSummary("stance_update")
        for assignment in tagging.assignments:
            if catalog.assign(assignment):
                summary.inc("assignments")
            else:
                summary.inc("assignments_dropped")

        decisions = self._decide(catalog, tagging.proposals, sample_items)
        for decision in decisions:
            self._apply_decision(catalog, tagging.proposals, decision, summary)
        summary.inc("proposals", len(tagging.proposals))
        return summary

    def _decide(
        self,
        catalog: StanceCatalog,
        proposals: list[StanceProposal],
        sample_items: list[SourceItem],
    ) -> list[StanceDecision]:
        if not proposals:
            return []
        if self.llm is None:
            return [StanceDecision(i, "accept") for i, _ in enumerate(proposals)]
        payload = {
            "customer_id": self.customer.entity_id,
            "catalog": catalog.snapshot(),
            "proposals": [proposal.__dict__ for proposal in proposals],
        }
        response = self.llm.complete_json(
            phase="stance_update",
            payload=payload,
            prompt=stance_update_prompt(
                self.customer,
                catalog.snapshot(),
                [proposal.__dict__ for proposal in proposals],
                sample_items,
            ),
            model=self.model,
        )
        decisions: list[StanceDecision] = []
        valid_ids = set(catalog.entries)
        for raw in response.get("decisions") or []:
            try:
                index = int(raw.get("proposal_index"))
            except (TypeError, ValueError):
                continue
            if not (0 <= index < len(proposals)):
                continue
            action = raw.get("action")
            if action not in ("accept", "reject", "rename", "generalise"):
                continue
            existing_id = raw.get("existing_id")
            if action in ("rename", "generalise") and existing_id not in valid_ids:
                action = "reject"
                existing_id = None
            decisions.append(
                StanceDecision(
                    proposal_index=index,
                    action=action,
                    existing_id=existing_id,
                    new_label=raw.get("new_label"),
                    new_description=raw.get("new_description"),
                    reason=str(raw.get("reason") or "")[:500],
                )
            )
        return decisions

    def _apply_decision(
        self,
        catalog: StanceCatalog,
        proposals: list[StanceProposal],
        decision: StanceDecision,
        summary: StepSummary,
    ) -> None:
        proposal = proposals[decision.proposal_index]
        if decision.action == "accept":
            catalog.add(proposal.label, proposal.description)
            summary.inc("accepted")
        elif decision.action == "reject":
            summary.inc("rejected")
        elif decision.action == "rename" and decision.existing_id:
            catalog.rename(
                decision.existing_id,
                decision.new_label or proposal.label,
                decision.new_description or proposal.description,
            )
            summary.inc("renamed")
        elif decision.action == "generalise" and decision.existing_id:
            summary.inc("generalised")


class ClaimTagger:
    def __init__(self, customer: Customer, llm: JsonLlm, *, model: Optional[str] = None):
        self.customer = customer
        self.llm = llm
        self.model = model or os.environ.get("OPENROUTER_CLAIM_TAGGER_MODEL", "openai/gpt-4o")

    def tag(self, event: LinkedEvent, items: list[SourceItem]) -> ClaimTagging:
        if not items:
            return ClaimTagging()
        payload = {
            "customer_id": self.customer.entity_id,
            "event_id": event.id,
            "items": [{"id": item.id, "kind": item.kind, "text": item.short_text()} for item in items],
        }
        response = self.llm.complete_json(
            phase="claim_tagging",
            payload=payload,
            prompt=claim_tagging_prompt(self.customer, event, items),
            model=self.model,
        )
        return self._parse_response(event.id, items, response)

    def _parse_response(self, event_id: str, items: list[SourceItem], response: dict) -> ClaimTagging:
        item_kind = {item.id: item.kind for item in items}
        result = ClaimTagging()
        for raw in response.get("claims") or []:
            source_item_id = str(raw.get("source_item_id") or "")
            verbatim = str(raw.get("verbatim") or "").strip()
            if source_item_id not in item_kind or not verbatim:
                result.dropped_invalid += 1
                continue
            affected = _int_list(raw.get("affected_entity_ids") or [])
            if self.customer.entity_id not in affected:
                result.dropped_off_customer += 1
                continue
            result.claims.append(
                RawClaim(
                    event_id=event_id,
                    customer_id=self.customer.entity_id,
                    affected_entity_ids=affected,
                    verbatim=verbatim,
                    source_item_id=source_item_id,
                    source_kind=item_kind[source_item_id],
                    importance=_importance(raw.get("importance")),
                    importance_reason=str(raw.get("importance_reason") or "")[:500],
                )
            )
        return result


class ClaimUpdater:
    def __init__(self, customer: Customer, llm: Optional[JsonLlm] = None, *, model: Optional[str] = None):
        self.customer = customer
        self.llm = llm
        self.model = model or os.environ.get("OPENROUTER_CLAIM_UPDATER_MODEL", "google/gemini-2.5-flash-lite")

    def update(
        self,
        store: ClaimCatalogStore,
        event: LinkedEvent,
        tagging: ClaimTagging,
    ) -> StepSummary:
        catalog = store.get_or_create(self.customer.entity_id, event.id)
        summary = StepSummary("claim_update")
        decisions, mutations = self._decide(event, catalog, tagging.claims)
        handled_claim_indexes: set[int] = set()

        for decision in decisions:
            if not (0 <= decision.claim_index < len(tagging.claims)):
                continue
            handled_claim_indexes.add(decision.claim_index)
            claim = tagging.claims[decision.claim_index]
            if decision.action == "assign" and decision.cluster_id and catalog.assign(claim, decision.cluster_id):
                summary.inc("assigned")
            elif decision.action == "create" and decision.canonical:
                catalog.create(claim, decision.canonical)
                summary.inc("created")
            else:
                summary.inc("dropped")
        summary.inc("dropped", len(tagging.claims) - len(handled_claim_indexes))

        for mutation in mutations:
            if mutation.kind == "rename" and mutation.cluster_id and mutation.new_canonical:
                if catalog.rename(mutation.cluster_id, mutation.new_canonical):
                    summary.inc("renamed")
            elif mutation.kind == "merge" and mutation.src_id and mutation.dst_id:
                if catalog.merge(mutation.src_id, mutation.dst_id):
                    summary.inc("merged")

        summary.inc("dropped_off_customer", tagging.dropped_off_customer)
        summary.inc("dropped_invalid", tagging.dropped_invalid)
        return summary

    def _decide(
        self,
        event: LinkedEvent,
        catalog: ClaimCatalog,
        claims: list[RawClaim],
    ) -> tuple[list[ClaimDecision], list[ClaimMutation]]:
        if not claims:
            return [], []
        if self.llm is None:
            return [
                ClaimDecision(i, "create", canonical=claim.verbatim)
                for i, claim in enumerate(claims)
            ], []

        clusters = [
            {
                "id": cluster.id,
                "canonical": cluster.canonical,
                "n_members": len(cluster.members),
                "samples": [member.verbatim for member in cluster.members[:3]],
            }
            for cluster in catalog.clusters.values()
        ]
        claims_payload = [claim.to_dict() for claim in claims]
        response = self.llm.complete_json(
            phase="claim_update",
            payload={
                "customer_id": self.customer.entity_id,
                "event_id": event.id,
                "clusters": clusters,
                "claims": claims_payload,
            },
            prompt=claim_update_prompt(self.customer, event, claims_payload, clusters),
            model=self.model,
        )
        return self._parse_decisions(response, len(claims), set(catalog.clusters))

    @staticmethod
    def _parse_decisions(
        response: dict,
        n_claims: int,
        valid_cluster_ids: set[str],
    ) -> tuple[list[ClaimDecision], list[ClaimMutation]]:
        decisions: list[ClaimDecision] = []
        for raw in response.get("decisions") or []:
            try:
                index = int(raw.get("claim_index"))
            except (TypeError, ValueError):
                continue
            if not (0 <= index < n_claims):
                continue
            action = raw.get("action")
            if action not in ("assign", "create", "drop"):
                continue
            cluster_id = raw.get("cluster_id")
            if action == "assign" and cluster_id not in valid_cluster_ids:
                action = "drop"
                cluster_id = None
            canonical = str(raw.get("canonical") or "").strip() or None
            decisions.append(
                ClaimDecision(
                    claim_index=index,
                    action=action,
                    cluster_id=cluster_id,
                    canonical=canonical,
                    reason=str(raw.get("reason") or "")[:500],
                )
            )

        mutations: list[ClaimMutation] = []
        for raw in response.get("mutations") or []:
            kind = raw.get("kind")
            if kind == "rename":
                cluster_id = raw.get("cluster_id")
                new_canonical = str(raw.get("new_canonical") or "").strip()
                if cluster_id in valid_cluster_ids and new_canonical:
                    mutations.append(
                        ClaimMutation(kind="rename", cluster_id=cluster_id, new_canonical=new_canonical)
                    )
            elif kind == "merge":
                src_id = raw.get("src_id")
                dst_id = raw.get("dst_id")
                if src_id in valid_cluster_ids and dst_id in valid_cluster_ids and src_id != dst_id:
                    mutations.append(ClaimMutation(kind="merge", src_id=src_id, dst_id=dst_id))
        return decisions, mutations


def _int_list(values: list) -> list[int]:
    out: list[int] = []
    for value in values:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def _importance(value) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 1
    return max(1, min(3, parsed))
