"""Separate stance and claim tagging/updating steps."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

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


def _local_item_payload(
    items: list[SourceItem],
    *,
    text_limit: int = 1200,
) -> tuple[list[dict[str, Any]], dict[str, SourceItem], dict[str, str]]:
    local_items: list[dict[str, Any]] = []
    local_item_by_id: dict[str, SourceItem] = {}
    local_id_by_source_id: dict[str, str] = {}
    for index, item in enumerate(items, start=1):
        local_id = str(index)
        local_item_by_id[local_id] = item
        local_id_by_source_id[item.id] = local_id
        local_items.append(
            {
                "id": index,
                "kind": item.kind,
                "text": item.short_text(text_limit),
            }
        )
    return local_items, local_item_by_id, local_id_by_source_id


def _triage_item_batches(
    items: list[SourceItem],
    batch_size: int,
) -> list[tuple[list[SourceItem], set[str]]]:
    chunk_size = max(1, batch_size)
    context = [item for item in items if item.kind in ("article", "user_post")]
    comments = [item for item in items if item.kind == "user_comment"]

    batches: list[tuple[list[SourceItem], set[str]]] = []
    for chunk in _chunks(context, chunk_size):
        batches.append((chunk, {item.id for item in chunk}))

    context_by_id = {item.id: item for item in context}
    comments_by_parent: dict[str, list[SourceItem]] = {}
    comments_without_context: list[SourceItem] = []
    for comment in comments:
        parent = context_by_id.get(comment.parent_source_id or "")
        if parent is None and len(context) == 1:
            parent = context[0]
        if parent is None:
            comments_without_context.append(comment)
        else:
            comments_by_parent.setdefault(parent.id, []).append(comment)

    for parent_id, grouped_comments in comments_by_parent.items():
        parent = context_by_id[parent_id]
        for chunk in _chunks(grouped_comments, chunk_size):
            batches.append(([parent, *chunk], {item.id for item in chunk}))

    for chunk in _chunks(comments_without_context, chunk_size):
        batches.append((chunk, {item.id for item in chunk}))

    return [(batch_items, candidate_ids) for batch_items, candidate_ids in batches if candidate_ids]


def _chunks(items: list[SourceItem], size: int) -> list[list[SourceItem]]:
    chunk_size = max(1, size)
    return [items[index : index + chunk_size] for index in range(0, len(items), chunk_size)]


def _local_triage_hints(
    hints: list[TypeTriageItem],
    local_id_by_source_id: dict[str, str],
) -> list[dict[str, Any]]:
    local_hints: list[dict[str, Any]] = []
    for hint in hints:
        local_id = local_id_by_source_id.get(hint.source_item_id)
        if local_id is None:
            continue
        local_hints.append(
            {
                "source_item_id": int(local_id),
                "stance_type": hint.stance_type,
                "importance_hint": hint.importance_hint,
            }
        )
    return local_hints


def _local_stance_proposals(
    proposals: list[StanceProposal],
    local_id_by_source_id: dict[str, str],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for proposal in proposals:
        item = {
            "kind": proposal.kind,
            "label": proposal.label,
            "description": proposal.description,
            "stance_type": proposal.stance_type,
            "source_item_ids": [
                int(local_id)
                for source_id in proposal.source_item_ids
                if (local_id := local_id_by_source_id.get(source_id)) is not None
            ],
        }
        if proposal.src_stance_id:
            item["src_stance_id"] = proposal.src_stance_id
        payload.append(item)
    return payload


@dataclass
class TypeTriageDebugCall:
    batch_index: int
    candidate_source_item_ids: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    response: dict[str, Any] = field(default_factory=dict)
    result: TypeTriageResult = field(default_factory=TypeTriageResult)


@dataclass
class TypeTriageDebugResult:
    result: TypeTriageResult
    prompt: str
    payload: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    calls: list[TypeTriageDebugCall] = field(default_factory=list)


class TypeTriageStep:
    def __init__(
        self,
        customer: Customer,
        llm: JsonLlm,
        *,
        model: Optional[str] = None,
    ):
        self.customer = customer
        self.llm = llm
        self.model = model or os.environ.get(
            "OPENROUTER_TYPE_TRIAGE_MODEL",
            "google/gemini-2.5-flash-lite",
        )

    def triage(
        self,
        event: LinkedEvent,
        items: list[SourceItem],
        *,
        batch_size: int = 12,
    ) -> TypeTriageResult:
        return self.triage_with_debug(event, items, batch_size=batch_size).result

    def triage_with_debug(
        self,
        event: LinkedEvent,
        items: list[SourceItem],
        *,
        batch_size: int = 12,
    ) -> TypeTriageDebugResult:
        if not items:
            return TypeTriageDebugResult(
                result=TypeTriageResult(n_items_seen=0),
                prompt="",
            )

        calls = [
            self._triage_call_with_debug(
                event,
                batch_items,
                candidate_source_item_ids=candidate_ids,
                batch_index=index,
            )
            for index, (batch_items, candidate_ids) in enumerate(
                _triage_item_batches(items, batch_size),
                start=1,
            )
        ]
        if not calls:
            return TypeTriageDebugResult(
                result=TypeTriageResult(n_items_seen=0),
                prompt="",
            )
        merged = self._merge_debug_calls(calls)
        return TypeTriageDebugResult(
            result=merged,
            payload=calls[0].payload,
            prompt=calls[0].prompt,
            response=calls[0].response,
            calls=calls,
        )

    def _triage_call_with_debug(
        self,
        event: LinkedEvent,
        items: list[SourceItem],
        *,
        candidate_source_item_ids: set[str],
        batch_index: int,
    ) -> TypeTriageDebugCall:
        local_items, local_item_by_id, _ = _local_item_payload(items)
        candidate_local_item_by_id = {
            local_id: item
            for local_id, item in local_item_by_id.items()
            if item.id in candidate_source_item_ids
        }
        payload = {
            "customer": {
                "name": self.customer.name,
                "description": self.customer.description,
            },
            "event": {
                "description": event.description,
            },
            "items": local_items,
        }
        prompt = type_triage_prompt(
            self.customer,
            event,
            local_items,
        )
        response = self.llm.complete_json(
            phase="type_triage",
            payload=payload,
            prompt=prompt,
            model=self.model,
        )
        result = self._parse_response(candidate_local_item_by_id, response)
        result.n_items_seen = len(candidate_source_item_ids)
        return TypeTriageDebugCall(
            batch_index=batch_index,
            candidate_source_item_ids=sorted(candidate_source_item_ids),
            payload=payload,
            prompt=prompt,
            response=response,
            result=result,
        )

    @staticmethod
    def _merge_debug_calls(calls: list[TypeTriageDebugCall]) -> TypeTriageResult:
        merged = TypeTriageResult()
        seen: set[tuple[str, StanceType]] = set()
        seen_items: set[str] = set()
        for call in calls:
            merged.dropped_invalid += call.result.dropped_invalid
            seen_items.update(call.candidate_source_item_ids)
            for hint in call.result.triaged:
                key = (hint.source_item_id, hint.stance_type)
                if key in seen:
                    continue
                seen.add(key)
                merged.triaged.append(hint)
        merged.n_items_seen = len(seen_items)
        return merged

    def _parse_response(
        self,
        local_item_by_id: dict[str, SourceItem],
        response: dict,
    ) -> TypeTriageResult:
        grouped: dict[str, list[TypeTriageItem]] = {}
        dropped_invalid = 0
        for raw in response.get("triage") or []:
            if not isinstance(raw, dict):
                dropped_invalid += 1
                continue
            item = local_item_by_id.get(_local_id(raw.get("source_item_id")))
            stance_type = _stance_type(raw.get("stance_type"))
            if item is None or stance_type is None:
                dropped_invalid += 1
                continue
            grouped.setdefault(item.id, []).append(
                TypeTriageItem(
                    source_item_id=item.id,
                    source_kind=item.kind,
                    stance_type=stance_type,
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

        return TypeTriageResult(
            triaged=triaged,
            n_items_seen=len({item.id for item in local_item_by_id.values()}),
            dropped_invalid=dropped_invalid,
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
        if not items:
            return StanceTagging()
        if triage_hints is not None:
            candidate_item_ids = {hint.source_item_id for hint in triage_hints}
        else:
            candidate_item_ids = {item.id for item in items}
        if not candidate_item_ids:
            return StanceTagging()
        local_items, local_item_by_id, local_id_by_source_id = _local_item_payload(items)
        candidate_local_item_by_id = {
            local_id: item
            for local_id, item in local_item_by_id.items()
            if item.id in candidate_item_ids
        }
        if not candidate_local_item_by_id:
            return StanceTagging()
        local_hints = _local_triage_hints(triage_hints or [], local_id_by_source_id)
        catalog_snapshot = (
            catalog.snapshot(types={stance_type})
            if stance_type in STANCE_BEARING_TYPES
            else catalog.snapshot()
        )
        allow_add_proposals = stance_type in STREAMING_GROWABLE_TYPES if stance_type else True
        payload = {
            "customer": {
                "name": self.customer.name,
                "description": self.customer.description,
            },
            "event": {
                "description": event.description,
            },
            "stance_type": stance_type,
            "catalog": catalog_snapshot,
            "triage_hints": local_hints,
            "allow_add_proposals": allow_add_proposals,
            "items": local_items,
        }
        response = self.llm.complete_json(
            phase="stance_tagging",
            payload=payload,
            prompt=stance_tagging_prompt(
                self.customer,
                event,
                local_items,
                catalog_snapshot,
                stance_type=stance_type,
                triage_hints=local_hints if triage_hints is not None else None,
                allow_add_proposals=allow_add_proposals,
            ),
            model=self.model,
        )
        return self._parse_response(
            event.id,
            candidate_local_item_by_id,
            local_item_by_id,
            catalog,
            response,
            stance_type=stance_type,
        )

    def _parse_response(
        self,
        event_id: str,
        candidate_local_item_by_id: dict[str, SourceItem],
        local_item_by_id: dict[str, SourceItem],
        catalog: StanceCatalog,
        response: dict,
        *,
        stance_type: Optional[StanceType] = None,
    ) -> StanceTagging:
        result = StanceTagging()
        for raw in response.get("assignments") or []:
            item = candidate_local_item_by_id.get(_local_id(raw.get("source_item_id")))
            raw_stance_type = _stance_type(raw.get("stance_type")) or stance_type or "entity_stance"
            if item is None:
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
                    source_item_id=item.id,
                    source_kind=item.kind,
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
                    source_item_ids=[
                        item.id
                        for value in raw.get("source_item_ids") or []
                        if (item := local_item_by_id.get(_local_id(value))) is not None
                    ],
                    src_stance_id=raw.get("src_stance_id"),
                )
            )
        assigned_item_ids = {
            assignment.source_item_id
            for assignment in result.assignments
            if assignment.stance_id is not None
        }
        candidate_item_ids = {item.id for item in candidate_local_item_by_id.values()}
        result.n_items_tagged_no_stance = len(candidate_item_ids - assigned_item_ids)
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
        local_sample_items, _, local_id_by_source_id = _local_item_payload(
            sample_items,
            text_limit=700,
        )
        proposals_payload = _local_stance_proposals(proposals, local_id_by_source_id)
        payload = {
            "customer": {
                "name": self.customer.name,
                "description": self.customer.description,
            },
            "catalog": catalog.snapshot(),
            "proposals": proposals_payload,
            "sample_items": local_sample_items,
        }
        response = self.llm.complete_json(
            phase="stance_update",
            payload=payload,
            prompt=stance_update_prompt(
                self.customer,
                catalog.snapshot(),
                proposals_payload,
                local_sample_items,
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
        local_items, local_item_by_id, _ = _local_item_payload(items)
        payload = {
            "customer": {
                "entity_id": self.customer.entity_id,
                "name": self.customer.name,
                "description": self.customer.description,
            },
            "event": {
                "description": event.description,
            },
            "include_comments": self.include_comments,
            "items": local_items,
        }
        response = self.llm.complete_json(
            phase="claim_tagging",
            payload=payload,
            prompt=claim_tagging_prompt(
                self.customer, event, local_items, include_comments=self.include_comments,
            ),
            model=self.model,
        )
        return self._parse_response(event.id, local_item_by_id, response)

    def _parse_response(
        self,
        event_id: str,
        local_item_by_id: dict[str, SourceItem],
        response: dict,
    ) -> ClaimTagging:
        claims, dropped_invalid, dropped_off_customer = _parse_claim_rows(
            customer_id=self.customer.entity_id,
            event_id=event_id,
            local_item_by_id=local_item_by_id,
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
        local_id_by_source_id: dict[str, int] = {}
        for claim in claims:
            if claim.source_item_id not in local_id_by_source_id:
                local_id_by_source_id[claim.source_item_id] = len(local_id_by_source_id) + 1
        claims_payload = [
            {
                "source_item_id": local_id_by_source_id[claim.source_item_id],
                "source_kind": claim.source_kind,
                "affected_entity_ids": list(claim.affected_entity_ids),
                "verbatim": claim.verbatim,
                "importance": claim.importance,
                "importance_reason": claim.importance_reason,
            }
            for claim in claims
        ]
        response = self.llm.complete_json(
            phase="claim_update",
            payload={
                "customer": {
                    "name": self.customer.name,
                    "description": self.customer.description,
                },
                "event": {
                    "description": event.description,
                },
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


def _local_id(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_claim_rows(
    *,
    customer_id: int,
    event_id: str,
    local_item_by_id: dict[str, SourceItem],
    source_kinds: set[str],
    rows: list,
) -> tuple[list[RawClaim], int, int]:
    claims: list[RawClaim] = []
    dropped_invalid = 0
    dropped_off_customer = 0
    for raw in rows:
        item = local_item_by_id.get(_local_id(raw.get("source_item_id")))
        verbatim = str(raw.get("verbatim") or "").strip()
        if item is None or item.kind not in source_kinds or not verbatim:
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
                source_item_id=item.id,
                source_kind=item.kind,
                importance=_importance(raw.get("importance")),
                importance_reason=str(raw.get("importance_reason") or "")[:500],
            )
        )
    return claims, dropped_invalid, dropped_off_customer
