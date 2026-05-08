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
    DEFAULT_STANCE_SENTIMENT,
    LinkedEvent,
    RawClaim,
    STANCE_BEARING_TYPES,
    STANCE_TYPES,
    STREAMING_GROWABLE_TYPES,
    TAG_ONLY_TYPES,
    Sentiment,
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
    stance_tagging_prompt,
    stance_update_prompt,
    type_triage_prompt,
)


CLAIM_SOURCE_KINDS = {"article", "user_post"}
CLAIM_SOURCE_KINDS_WITH_COMMENTS = {"article", "user_post", "user_comment"}


def claim_source_kinds(include_comments: bool = False) -> set[str]:
    """Active source kinds for claim extraction.

    Default (False) — claims come from articles + posts only; comments are
    excluded because they're typically opinion / anecdote / sarcasm, not
    factual reporting. Pass True to opt comments back in.
    """
    return CLAIM_SOURCE_KINDS_WITH_COMMENTS if include_comments else CLAIM_SOURCE_KINDS


class TypeTriageStep:
    def __init__(
        self,
        customer: Customer,
        llm: JsonLlm,
        *,
        model: Optional[str] = None,
        include_comments_for_claims: bool = False,
    ):
        self.customer = customer
        self.llm = llm
        self.model = model or os.environ.get(
            "OPENROUTER_TYPE_TRIAGE_MODEL",
            "google/gemini-2.5-flash-lite",
        )
        self.include_comments_for_claims = include_comments_for_claims

    @property
    def claim_source_kinds(self) -> set[str]:
        return claim_source_kinds(self.include_comments_for_claims)

    def triage(self, event: LinkedEvent, items: list[SourceItem]) -> TypeTriageResult:
        if not items:
            return TypeTriageResult(n_items_seen=0)
        payload = {
            "customer_id": self.customer.entity_id,
            "event_id": event.id,
            "include_comments_for_claims": self.include_comments_for_claims,
            "items": [{"id": item.id, "kind": item.kind, "text": item.short_text()} for item in items],
        }
        response = self.llm.complete_json(
            phase="type_triage",
            payload=payload,
            prompt=type_triage_prompt(
                self.customer,
                event,
                items,
                include_comments_for_claims=self.include_comments_for_claims,
            ),
            model=self.model,
        )
        return self._parse_response(event.id, items, response)

    def _parse_response(self, event_id: str, items: list[SourceItem], response: dict) -> TypeTriageResult:
        item_by_id = {item.id: item for item in items}
        grouped: dict[str, list[TypeTriageItem]] = {}
        dropped_invalid = 0
        for raw in response.get("triage") or []:
            source_item_id = str(raw.get("source_item_id") or "")
            item = item_by_id.get(source_item_id)
            stance_type = _stance_type(raw.get("stance_type"))
            if item is None or stance_type is None:
                dropped_invalid += 1
                continue
            grouped.setdefault(source_item_id, []).append(
                TypeTriageItem(
                    source_item_id=source_item_id,
                    source_kind=item.kind,
                    stance_type=stance_type,
                    brief_summary=str(raw.get("brief_summary") or "").strip()[:500],
                    sentiment=_sentiment(raw.get("sentiment"), stance_type=stance_type),
                    importance_hint=_consistency_relevance(raw.get("importance_hint")),
                )
            )

        triaged: list[TypeTriageItem] = []
        for source_item_id, entries in grouped.items():
            noise = [entry for entry in entries if entry.stance_type == "noise"]
            if noise:
                triaged.append(noise[0])
                continue
            triaged.extend(entries[:4])

        claims, dropped_claim_invalid, dropped_off_customer = _parse_claim_rows(
            customer_id=self.customer.entity_id,
            event_id=event_id,
            items=items,
            source_kinds=self.claim_source_kinds,
            rows=response.get("claims") or [],
        )
        return TypeTriageResult(
            triaged=triaged,
            claims=claims,
            n_items_seen=len(items),
            dropped_invalid=dropped_invalid + dropped_claim_invalid,
            dropped_claim_invalid=dropped_claim_invalid,
            dropped_off_customer=dropped_off_customer,
        )


class StanceTagger:
    def __init__(self, customer: Customer, llm: JsonLlm, *, model: Optional[str] = None):
        self.customer = customer
        self.llm = llm
        self.model = model or os.environ.get("OPENROUTER_STANCE_TAGGER_MODEL", "openai/gpt-4o")

    def tag(
        self,
        event: LinkedEvent,
        items: list[SourceItem],
        catalog: StanceCatalog,
        *,
        stance_type: Optional[StanceType] = None,
        triage_hints: Optional[list[TypeTriageItem]] = None,
    ) -> StanceTagging:
        if triage_hints is not None:
            hint_item_ids = {hint.source_item_id for hint in triage_hints}
            items = [item for item in items if item.id in hint_item_ids]
        if not items:
            return StanceTagging()
        catalog_snapshot = (
            catalog.snapshot(types={stance_type})
            if stance_type in STANCE_BEARING_TYPES
            else catalog.snapshot()
        )
        allow_add_proposals = stance_type in STREAMING_GROWABLE_TYPES if stance_type else True
        payload = {
            "customer_id": self.customer.entity_id,
            "event_id": event.id,
            "stance_type": stance_type,
            "catalog": catalog_snapshot,
            "triage_hints": [hint.to_dict() for hint in triage_hints or []],
            "allow_add_proposals": allow_add_proposals,
            "items": [{"id": item.id, "kind": item.kind, "text": item.short_text()} for item in items],
        }
        response = self.llm.complete_json(
            phase="stance_tagging",
            payload=payload,
            prompt=stance_tagging_prompt(
                self.customer,
                event,
                items,
                catalog_snapshot,
                stance_type=stance_type,
                triage_hints=triage_hints,
                allow_add_proposals=allow_add_proposals,
            ),
            model=self.model,
        )
        return self._parse_response(event.id, items, catalog, response, stance_type=stance_type)

    def _parse_response(
        self,
        event_id: str,
        items: list[SourceItem],
        catalog: StanceCatalog,
        response: dict,
        *,
        stance_type: Optional[StanceType] = None,
    ) -> StanceTagging:
        item_kind = {item.id: item.kind for item in items}
        result = StanceTagging()
        for raw in response.get("assignments") or []:
            source_item_id = str(raw.get("source_item_id") or "")
            raw_stance_type = _stance_type(raw.get("stance_type")) or stance_type or "entity_stance"
            if source_item_id not in item_kind:
                result.dropped_assignments += 1
                continue
            if stance_type and raw_stance_type != stance_type:
                result.dropped_assignments += 1
                continue
            stance_id = _optional_id(raw.get("stance_id"))
            if raw_stance_type in TAG_ONLY_TYPES and stance_id is not None:
                result.dropped_assignments += 1
                continue
            if stance_id is not None:
                entry = catalog.entries.get(stance_id)
                if entry is None or entry.primary_type != raw_stance_type:
                    result.dropped_assignments += 1
                    continue
            result.assignments.append(
                StanceAssignment(
                    source_item_id=source_item_id,
                    source_kind=item_kind[source_item_id],
                    customer_id=self.customer.entity_id,
                    stance_id=stance_id,
                    stance_type=raw_stance_type,
                    sentiment=_sentiment(raw.get("sentiment"), stance_type=raw_stance_type),
                    consistency_relevance=_consistency_relevance(
                        raw.get("consistency_relevance") or raw.get("importance_hint")
                    ),
                    event_id=event_id,
                    reason=str(raw.get("reason") or "")[:500],
                )
            )
            result.n_assignments_by_type[raw_stance_type] = (
                result.n_assignments_by_type.get(raw_stance_type, 0) + 1
            )

        for raw in response.get("proposals") or []:
            kind = raw.get("kind")
            label = str(raw.get("label") or "").strip()
            proposal_type = _stance_type(raw.get("stance_type")) or stance_type or "entity_stance"
            if stance_type and proposal_type != stance_type:
                continue
            if kind not in ("add", "rename") or not label:
                continue
            result.proposals.append(
                StanceProposal(
                    kind=kind,
                    label=label,
                    description=str(raw.get("description") or "").strip(),
                    stance_type=proposal_type,
                    source_item_ids=[str(x) for x in raw.get("source_item_ids") or []],
                    src_stance_id=raw.get("src_stance_id"),
                )
            )
        assigned_item_ids = {
            assignment.source_item_id
            for assignment in result.assignments
            if assignment.stance_id is not None
        }
        result.n_items_tagged_no_stance = len(set(item_kind) - assigned_item_ids)
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
        allow_all_growth: bool = False,
    ) -> StepSummary:
        summary = StepSummary("stance_update")
        for assignment in tagging.assignments:
            if catalog.assign(assignment):
                summary.inc("assignments")
            else:
                summary.inc("assignments_dropped")

        decisions = self._decide(catalog, tagging.proposals, sample_items)
        for decision in decisions:
            self._apply_decision(
                catalog,
                tagging.proposals,
                decision,
                summary,
                allow_all_growth=allow_all_growth,
            )
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
        *,
        allow_all_growth: bool = False,
    ) -> None:
        proposal = proposals[decision.proposal_index]
        if decision.action == "accept":
            if proposal.kind == "rename":
                if proposal.src_stance_id and catalog.rename(
                    proposal.src_stance_id,
                    proposal.label,
                    proposal.description,
                ):
                    summary.inc("renamed")
                else:
                    summary.inc("rejected")
                return
            if not allow_all_growth and proposal.stance_type not in STREAMING_GROWABLE_TYPES:
                summary.inc("rejected_streaming_growth_blocked")
                return
            catalog.add(
                proposal.label,
                proposal.description,
                primary_type=proposal.stance_type,
            )
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
    def __init__(
        self,
        customer: Customer,
        llm: JsonLlm,
        *,
        model: Optional[str] = None,
        include_comments: bool = False,
    ):
        """Per-event claim extractor.

        `include_comments` controls whether `user_comment` items can be
        sources for claims. Default (False) drops them — comments are
        opinion-heavy and dilute the factual signal of the claim catalog.
        """
        self.customer = customer
        self.llm = llm
        self.model = model or os.environ.get("OPENROUTER_CLAIM_TAGGER_MODEL", "openai/gpt-4o")
        self.include_comments = include_comments

    @property
    def source_kinds(self) -> set[str]:
        return claim_source_kinds(self.include_comments)

    def tag(self, event: LinkedEvent, items: list[SourceItem]) -> ClaimTagging:
        items = [item for item in items if item.kind in self.source_kinds]
        if not items:
            return ClaimTagging()
        payload = {
            "customer_id": self.customer.entity_id,
            "event_id": event.id,
            "include_comments": self.include_comments,
            "items": [{"id": item.id, "kind": item.kind, "text": item.short_text()} for item in items],
        }
        response = self.llm.complete_json(
            phase="claim_tagging",
            payload=payload,
            prompt=claim_tagging_prompt(
                self.customer, event, items, include_comments=self.include_comments,
            ),
            model=self.model,
        )
        return self._parse_response(event.id, items, response)

    def _parse_response(self, event_id: str, items: list[SourceItem], response: dict) -> ClaimTagging:
        claims, dropped_invalid, dropped_off_customer = _parse_claim_rows(
            customer_id=self.customer.entity_id,
            event_id=event_id,
            items=items,
            source_kinds=self.source_kinds,
            rows=response.get("claims") or [],
        )
        return ClaimTagging(
            claims=claims,
            dropped_invalid=dropped_invalid,
            dropped_off_customer=dropped_off_customer,
        )


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


def _stance_type(value) -> Optional[StanceType]:
    if value in STANCE_TYPES:
        return value
    return None


def _sentiment(value, *, stance_type: Optional[StanceType] = None) -> Optional[Sentiment]:
    if value in ("positive", "negative", "neutral"):
        return value
    if stance_type:
        return DEFAULT_STANCE_SENTIMENT.get(stance_type)
    return None


def _consistency_relevance(value):
    if value in ("low", "medium", "high"):
        return value
    return None


def _optional_id(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    return text


def _parse_claim_rows(
    *,
    customer_id: int,
    event_id: str,
    items: list[SourceItem],
    source_kinds: set[str],
    rows: list,
) -> tuple[list[RawClaim], int, int]:
    item_kind = {item.id: item.kind for item in items if item.kind in source_kinds}
    claims: list[RawClaim] = []
    dropped_invalid = 0
    dropped_off_customer = 0
    for raw in rows:
        source_item_id = str(raw.get("source_item_id") or "")
        verbatim = str(raw.get("verbatim") or "").strip()
        if source_item_id not in item_kind or not verbatim:
            dropped_invalid += 1
            continue
        affected = _int_list(raw.get("affected_entity_ids") or [])
        if customer_id not in affected:
            dropped_off_customer += 1
            continue
        claims.append(
            RawClaim(
                event_id=event_id,
                customer_id=customer_id,
                affected_entity_ids=affected,
                verbatim=verbatim,
                source_item_id=source_item_id,
                source_kind=item_kind[source_item_id],
                importance=_importance(raw.get("importance")),
                importance_reason=str(raw.get("importance_reason") or "")[:500],
            )
        )
    return claims, dropped_invalid, dropped_off_customer
